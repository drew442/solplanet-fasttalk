"""Local JSON and server-sent-event API."""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        plans=None,
        plugins=None,
    ) -> None:
        self.config = config
        self.state = state
        self.history = history
        self.tariff = tariff
        self.forecast = forecast
        self.plans = plans
        self.plugins = plugins
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
            server_version = "solplanet-fasttalk/0.2"

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                try:
                    if parsed.path == "/":
                        self._json(
                            {
                                "service": "solplanet-fasttalk",
                                "api_version": "v1",
                                "control_available": False,
                                "mode": "shadow",
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
                        self._json(
                            {
                                "measurements": api.history.measurements(
                                    name,
                                    since=self._one(query, "since"),
                                    until=self._one(query, "until"),
                                    limit=self._limit(query, 1000),
                                    resolution=self._one(query, "resolution")
                                    or "raw",
                                )
                            }
                        )
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
                    elif parsed.path == "/v1/forecasts/pv" and api.forecast:
                        payload = api.forecast.snapshot()
                        if self._one(query, "since") or self._one(query, "until"):
                            payload["historical_comparison"] = (
                                api.history.forecast_comparison(
                                    since=self._one(query, "since"),
                                    until=self._one(query, "until"),
                                    limit=self._limit(query, 1000),
                                )
                            )
                        self._json(payload)
                    elif parsed.path == "/v1/plans/current" and api.plans:
                        self._json(api.plans.snapshot())
                    elif parsed.path == "/metrics":
                        self._metrics()
                    else:
                        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                except (ValueError, OSError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

            def do_POST(self) -> None:
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
            "history": ["raw", "hourly", "daily"],
            "streaming": ["server_sent_events"],
            "tariff": self.tariff is not None,
            "forecast": self.forecast is not None,
            "optimisation": self.plans is not None,
            "plugins": (
                self.plugins.descriptors() if self.plugins is not None else []
            ),
        }
        return {"capabilities": capabilities}
