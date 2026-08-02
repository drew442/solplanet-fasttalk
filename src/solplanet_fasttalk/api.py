"""Local JSON and server-sent-event API."""

from __future__ import annotations

import datetime as dt
import hmac
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import DaemonConfig
from .model import PlantState
from .storage import HistoryReader


class API:
    def __init__(
        self,
        config: DaemonConfig,
        state: PlantState,
        history: HistoryReader,
        *,
        tariff=None,
        forecast=None,
        weather=None,
        plans=None,
        plugins=None,
    ) -> None:
        self.config = config
        self.state = state
        self.history = history
        self.tariff = tariff
        self.forecast = forecast
        self.weather = weather
        self.plans = plans
        self.plugins = plugins
        self.auth_token = (
            self._read_auth_token(config.api.auth_token_file)
            if config.api.auth_token_file
            else None
        )
        self.server = ThreadingHTTPServer(
            (config.api.host, config.api.port),
            self._handler(),
        )
        self.server.daemon_threads = True

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def serve(self) -> None:
        self.server.serve_forever(poll_interval=0.25)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "solplanet-fasttalk/0.7.4"

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                try:
                    if parsed.path == "/diagnostics":
                        self._redirect("/diagnostics/")
                        return
                    if parsed.path == "/diagnostics/":
                        self._static("index.html", "text/html; charset=utf-8")
                        return
                    if parsed.path == "/diagnostics/app.css":
                        self._static("app.css", "text/css; charset=utf-8")
                        return
                    if parsed.path == "/diagnostics/app.js":
                        self._static(
                            "app.js", "text/javascript; charset=utf-8"
                        )
                        return
                    if parsed.path == "/diagnostics/config.json":
                        self._json(
                            {
                                "authentication_required": (
                                    api.auth_token is not None
                                ),
                                "api_base": "/v1",
                                "poll_seconds": 5,
                            }
                        )
                        return
                    if not self._authorized():
                        self._unauthorized()
                        return
                    if parsed.path == "/":
                        self._json(
                            {
                                "service": "solplanet-fasttalk",
                                "api_version": "v1",
                                "control_available": False,
                                "mode": "shadow",
                                "diagnostics_ui": "/diagnostics/",
                            }
                        )
                    elif parsed.path == "/v1/plant":
                        self._json(api.state.plant())
                    elif parsed.path == "/v1/measurements/current":
                        self._json({"measurements": api.state.current()})
                    elif parsed.path == "/v1/measurements/history":
                        name = self._one(query, "name")
                        if not name:
                            self._json(
                                {"error": "the name query parameter is required"},
                                HTTPStatus.BAD_REQUEST,
                            )
                            return
                        bucket = self._one(query, "bucket_seconds")
                        measurements = (
                            api.history.series(
                                name,
                                since=self._one(query, "since"),
                                until=self._one(query, "until"),
                                bucket_seconds=int(bucket),
                                limit=self._limit(query, 1000),
                            )
                            if bucket is not None
                            else api.history.measurements(
                                name,
                                since=self._one(query, "since"),
                                until=self._one(query, "until"),
                                limit=self._limit(query, 1000),
                                resolution=self._one(query, "resolution")
                                or "raw",
                            )
                        )
                        self._json({"measurements": measurements})
                    elif parsed.path == "/v1/devices":
                        self._json({"devices": api._devices()})
                    elif parsed.path == "/v1/capabilities":
                        self._json(api._capabilities())
                    elif parsed.path == "/v1/health":
                        self._json(api.state.health())
                    elif parsed.path == "/v1/events":
                        self._json(
                            {"events": api.history.events(self._limit(query, 200))}
                        )
                    elif parsed.path == "/v1/stream":
                        self._stream()
                    elif parsed.path == "/v1/tariffs/current" and api.tariff:
                        self._json(api.tariff.current())
                    elif parsed.path == "/v1/tariffs/forecast" and api.tariff:
                        hours = max(
                            1,
                            min(168, int(self._one(query, "hours") or 48)),
                        )
                        step = max(
                            5,
                            min(60, int(self._one(query, "step_minutes") or 15)),
                        )
                        start = dt.datetime.now(dt.timezone.utc).replace(
                            second=0,
                            microsecond=0,
                        )
                        self._json(
                            {
                                "generated_at": start.isoformat(),
                                "points": [
                                    api.tariff.quote(
                                        start + dt.timedelta(minutes=offset)
                                    ).as_dict()
                                    for offset in range(
                                        0,
                                        hours * 60 + 1,
                                        step,
                                    )
                                ],
                            }
                        )
                    elif parsed.path == "/v1/forecasts/pv" and api.forecast:
                        payload = api.forecast.snapshot()
                        if self._one(query, "since") or self._one(query, "until"):
                            payload["historical_comparison"] = (
                                api.history.forecast_comparison(
                                    provider="fasttalk.corrected",
                                    since=self._one(query, "since"),
                                    until=self._one(query, "until"),
                                    limit=self._limit(query, 1000),
                                )
                            )
                            payload["historical_base_comparison"] = (
                                api.history.forecast_comparison(
                                    provider="forecast.solar",
                                    since=self._one(query, "since"),
                                    until=self._one(query, "until"),
                                    limit=self._limit(query, 1000),
                                )
                            )
                        self._json(payload)
                    elif parsed.path == "/v1/weather" and api.weather:
                        self._json(api.weather.snapshot())
                    elif parsed.path == "/v1/predictions/history":
                        signal = self._one(query, "signal")
                        scenario = self._one(query, "scenario")
                        if not signal or not scenario:
                            self._json(
                                {
                                    "error": (
                                        "signal and scenario query parameters "
                                        "are required"
                                    )
                                },
                                HTTPStatus.BAD_REQUEST,
                            )
                            return
                        self._json(
                            {
                                "predictions": api.history.prediction_samples(
                                    signal=signal,
                                    scenario=scenario,
                                    since=self._one(query, "since"),
                                    until=self._one(query, "until"),
                                    limit=self._limit(query, 1000),
                                )
                            }
                        )
                    elif parsed.path == "/v1/predictions/quality":
                        signal = self._one(query, "signal")
                        scenario = self._one(query, "scenario")
                        if not signal or not scenario:
                            self._json(
                                {
                                    "error": (
                                        "signal and scenario query parameters "
                                        "are required"
                                    )
                                },
                                HTTPStatus.BAD_REQUEST,
                            )
                            return
                        self._json(
                            api.history.prediction_quality(
                                signal=signal,
                                scenario=scenario,
                                since=self._one(query, "since"),
                                until=self._one(query, "until"),
                            )
                        )
                    elif parsed.path == "/v1/training/coverage":
                        self._json(api.history.training_coverage())
                    elif parsed.path == "/v1/plans/current" and api.plans:
                        self._json(api.plans.snapshot())
                    elif parsed.path == "/v1/plans/history":
                        self._json(
                            {
                                "plans": api.history.plans(
                                    since=self._one(query, "since"),
                                    until=self._one(query, "until"),
                                    limit=self._limit(query, 200),
                                    include_plan=(
                                        self._one(query, "include_plan") == "true"
                                    ),
                                )
                            }
                        )
                    elif parsed.path == "/v1/financials/history":
                        self._json(
                            {
                                "financials": api.history.financial_history(
                                    since=self._one(query, "since"),
                                    until=self._one(query, "until"),
                                    bucket_seconds=int(
                                        self._one(query, "bucket_seconds")
                                        or 3600
                                    ),
                                    limit=self._limit(query, 1000),
                                )
                            }
                        )
                    elif parsed.path == "/v1/financials/summary":
                        self._json(
                            api.history.financial_summary(
                                since=self._one(query, "since"),
                                until=self._one(query, "until"),
                            )
                        )
                    elif parsed.path == "/v1/diagnostics":
                        self._json(api._diagnostics())
                    elif parsed.path == "/metrics":
                        self._metrics()
                    else:
                        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                except (ValueError, OSError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

            def do_POST(self) -> None:
                if not self._authorized():
                    self._unauthorized()
                    return
                self._json(
                    {
                        "error": (
                            "the daemon is in shadow mode; API control and "
                            "Modbus writes are unavailable"
                        )
                    },
                    HTTPStatus.METHOD_NOT_ALLOWED,
                )

            def _json(
                self,
                value: Any,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                payload = json.dumps(value, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def _static(self, name: str, content_type: str) -> None:
                try:
                    payload = (
                        resources.files("solplanet_fasttalk.webui")
                        .joinpath(name)
                        .read_bytes()
                    )
                except (FileNotFoundError, ModuleNotFoundError):
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header(
                    "Cache-Control",
                    "no-cache" if name == "index.html" else "public, max-age=300",
                )
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    (
                        "default-src 'self'; connect-src 'self'; "
                        "img-src 'self' data:; style-src 'self'; "
                        "script-src 'self'; frame-ancestors 'none'; "
                        "base-uri 'none'; form-action 'self'"
                    ),
                )
                self.end_headers()
                self.wfile.write(payload)

            def _redirect(self, location: str) -> None:
                self.send_response(HTTPStatus.PERMANENT_REDIRECT)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def _authorized(self) -> bool:
                if api.auth_token is None:
                    return True
                scheme, _, token = self.headers.get(
                    "Authorization", ""
                ).partition(" ")
                return (
                    scheme.lower() == "bearer"
                    and hmac.compare_digest(token, api.auth_token)
                )

            def _unauthorized(self) -> None:
                payload = b'{"error":"authentication required"}'
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)

            def _metrics(self) -> None:
                health = api.state.health()
                qualities = health["measurement_quality"]
                lines = [
                    "# HELP solplanet_fasttalk_up Daemon health is not failed.",
                    "# TYPE solplanet_fasttalk_up gauge",
                    (
                        "solplanet_fasttalk_up "
                        + ("0" if health["status"] == "failed" else "1")
                    ),
                    "# TYPE solplanet_fasttalk_measurements gauge",
                ]
                for quality, count in sorted(qualities.items()):
                    lines.append(
                        'solplanet_fasttalk_measurements{quality="'
                        + quality.replace('"', "")
                        + f'"}} {count}'
                    )
                lines.extend(
                    (
                        "# TYPE solplanet_fasttalk_control_available gauge",
                        "solplanet_fasttalk_control_available 0",
                    )
                )
                payload = ("\n".join(lines) + "\n").encode()
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type", "text/plain; version=0.0.4; charset=utf-8"
                )
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def _stream(self) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                sequence = -1
                try:
                    while True:
                        next_sequence = api.state.wait_for_change(sequence, 15.0)
                        if next_sequence == sequence:
                            self.wfile.write(b": keepalive\n\n")
                        else:
                            data = json.dumps(
                                {
                                    "sequence": next_sequence,
                                    "plant": api.state.plant(),
                                },
                                separators=(",", ":"),
                            ).encode()
                            self.wfile.write(b"event: plant\n")
                            self.wfile.write(b"data: " + data + b"\n\n")
                            sequence = next_sequence
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            @staticmethod
            def _one(query: dict[str, list[str]], name: str) -> str | None:
                values = query.get(name)
                return values[0] if values else None

            def _limit(self, query: dict[str, list[str]], default: int) -> int:
                raw = self._one(query, "limit")
                return default if raw is None else int(raw)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    @staticmethod
    def _read_auth_token(path: str) -> str:
        return Path(path).read_text(encoding="utf-8").strip()

    def _devices(self) -> list[dict[str, Any]]:
        health = self.state.health()["components"]
        devices: list[dict[str, Any]] = []
        if self.config.eastron.enabled:
            devices.append(
                {
                    "id": "eastron-terminal8",
                    "type": "meter",
                    "model": "SEM3-M-2L-CT",
                    "access_mode": "passive_bus",
                    "authoritative_for": [
                        "grid",
                        "external_pv_ac",
                    ],
                    "capabilities": [
                        "phase_measurements",
                        "active_reactive_apparent_power",
                        "frequency",
                        "cumulative_energy",
                    ],
                    "control": False,
                    "health": health.get("eastron", {}),
                }
            )
        if self.config.asw.enabled:
            devices.append(
                {
                    "id": "asw",
                    "type": "hybrid_inverter_ess",
                    "access_mode": "direct_wired_modbus",
                    "authoritative_for": ["asw", "battery"],
                    "capabilities": [
                        "inverter_state",
                        "inverter_power",
                        "aggregate_battery",
                        "bms_limits",
                        "faults",
                    ],
                    "control": False,
                    "health": health.get("asw", {}),
                }
            )
        if self.config.solis.enabled:
            devices.append(
                {
                    "id": "solis-10k",
                    "type": "pv_inverter",
                    "access_mode": "direct_wired_modbus",
                    "authoritative_for": [],
                    "supplements": ["external_pv_ac"],
                    "measured_by": "eastron-terminal8:slave-2",
                    "capabilities": [
                        "dc_inputs",
                        "temperature",
                        "operating_state",
                        "diagnostics",
                    ],
                    "control": False,
                    "health": health.get("solis", {}),
                }
            )
        return devices

    def _capabilities(self) -> dict[str, Any]:
        capabilities = {
            "accounting": {
                "grid": "eastron-terminal8:slave-1",
                "external_pv_ac": "eastron-terminal8:slave-2",
                "source_resolution": "authoritative_meter_wins",
            },
            "control": {
                "available": False,
                "mode": "shadow",
                "modbus_writes": False,
            },
            "history": [
                "raw",
                "hourly",
                "daily",
                "financial",
                "forecast_accuracy",
                "load_and_soc_prediction_accuracy",
                "model_training_coverage",
                "plan_decisions",
            ],
            "streaming": ["server_sent_events"],
            "tariff": self.tariff is not None,
            "forecast": self.forecast is not None,
            "weather": self.weather is not None,
            "optimisation": self.plans is not None,
            "plugins": (
                self.plugins.descriptors() if self.plugins is not None else []
            ),
        }
        return {"capabilities": capabilities}

    def _diagnostics(self) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc)
        if self.tariff:
            local = now.astimezone(self.tariff.timezone)
            today = local.replace(hour=0, minute=0, second=0, microsecond=0)
            month = today.replace(day=1)
            financials = {
                "today": self.history.financial_summary(
                    since=today.astimezone(dt.timezone.utc).isoformat(),
                    until=now.isoformat(),
                ),
                "month": self.history.financial_summary(
                    since=month.astimezone(dt.timezone.utc).isoformat(),
                    until=now.isoformat(),
                ),
            }
        else:
            financials = None
        return {
            "plant": self.state.plant(),
            "measurements": self.state.current(),
            "health": self.state.health(),
            "devices": self._devices(),
            "capabilities": self._capabilities()["capabilities"],
            "tariff": self.tariff.current() if self.tariff else None,
            "forecast": self.forecast.snapshot() if self.forecast else None,
            "weather": self.weather.snapshot() if self.weather else None,
            "plan": self.plans.snapshot() if self.plans else None,
            "plan_history": self.history.plans(limit=12),
            "financials": financials,
            "events": self.history.events(20),
        }
