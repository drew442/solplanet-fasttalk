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
    ) -> None:
        self.config = config
        self.state = state
        self.history = history
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
            server_version = "solplanet-fasttalk/0.1"

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
                                )
                            }
                        )
                    elif parsed.path == "/v1/devices":
                        self._json({"devices": api._devices()})
                    elif parsed.path == "/v1/health":
                        self._json(api.state.health())
                    elif parsed.path == "/v1/events":
                        self._json(
                            {"events": api.history.events(self._limit(query, 200))}
                        )
                    elif parsed.path == "/v1/stream":
                        self._stream()
                    else:
                        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                except (ValueError, OSError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

            def do_POST(self) -> None:
                self._json(
                    {"error": "this milestone is read-only"},
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
        return devices

