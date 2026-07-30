"""Forecast.Solar adapter with secret-file, cache, and offline handling."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import ForecastSolarConfig
from .model import PlantState, utc_now


def _read_location(path: str) -> tuple[float, float]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    data = payload.get("data", payload)
    latitude = float(data["latitude"])
    longitude = float(data["longitude"])
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("forecast location is outside valid bounds")
    return latitude, longitude


def _read_key(path: str) -> str:
    key = Path(path).read_text(encoding="utf-8").strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("Forecast.Solar API key file is empty or malformed")
    return key


def _endpoint(
    config: ForecastSolarConfig,
    *,
    key: str,
    latitude: float,
    longitude: float,
) -> str:
    plane_parts = []
    for plane in config.planes:
        plane_parts.extend(
            (
                f"{plane.declination:g}",
                f"{plane.azimuth:g}",
                f"{plane.peak_power_kw:g}",
            )
        )
    return (
        "https://api.forecast.solar/"
        + "/".join(
            (
                key,
                "estimate",
                f"{latitude:.6f}",
                f"{longitude:.6f}",
                *plane_parts,
            )
        )
    )


class ForecastStore:
    def __init__(self, config: ForecastSolarConfig, timezone: str) -> None:
        self.config = config
        self.timezone = ZoneInfo(timezone)
        self._lock = threading.RLock()
        self._payload: dict[str, Any] | None = None
        self._actual: dict[str, Any] | None = None
        self._error: str | None = None

    def replace(self, payload: dict[str, Any], *, cached: bool) -> None:
        with self._lock:
            self._payload = payload
            self._payload["cached"] = cached
            self._error = None

    def fail(self, error: str) -> None:
        with self._lock:
            self._error = error

    def update_actual(self, state: PlantState) -> None:
        measurement = state.current().get("external_pv.active_power")
        if not measurement or measurement["quality"] != "good":
            return
        with self._lock:
            if not self._payload:
                return
            points = self._payload.get("points", [])
            if not points:
                return
            now = dt.datetime.now(dt.timezone.utc)
            closest = min(
                points,
                key=lambda point: abs(
                    dt.datetime.fromisoformat(point["timestamp"]).timestamp()
                    - now.timestamp()
                ),
            )
            actual = float(measurement["value"])
            forecast = float(closest["power_w"])
            self._actual = {
                "observed_at": measurement["observed_at"],
                "actual_power_w": actual,
                "forecast_power_w": forecast,
                "error_w": actual - forecast,
                "actual_source": measurement["source"],
                "actual_authority": measurement["authority"],
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._payload or {})
            fetched = payload.get("fetched_at")
            age = None
            if fetched:
                age = max(
                    0.0,
                    (
                        dt.datetime.now(dt.timezone.utc)
                        - dt.datetime.fromisoformat(fetched)
                    ).total_seconds(),
                )
            return {
                "provider": "forecast.solar",
                "status": (
                    "unavailable"
                    if not payload
                    else "stale"
                    if age is not None
                    and age > self.config.max_cache_age_seconds
                    else "ok"
                ),
                "fetched_at": fetched,
                "age_seconds": round(age, 3) if age is not None else None,
                "cached": payload.get("cached"),
                "planes": [
                    {
                        "name": plane.name,
                        "declination": plane.declination,
                        "azimuth": plane.azimuth,
                        "peak_power_kw": plane.peak_power_kw,
                    }
                    for plane in self.config.planes
                ],
                "points": payload.get("points", []),
                "daily_energy_wh": payload.get("daily_energy_wh", {}),
                "comparison_scope": (
                    "authoritative aggregate external-PV AC power against "
                    "the combined configured planes"
                ),
                "actual_comparison": dict(self._actual) if self._actual else None,
                "last_error": self._error,
            }


class ForecastSolarWorker:
    def __init__(
        self,
        config: ForecastSolarConfig,
        timezone: str,
        store: ForecastStore,
        state: PlantState,
        history=None,
    ) -> None:
        self.config = config
        self.timezone = ZoneInfo(timezone)
        self.store = store
        self.state = state
        self.history = history
        self.requests = 0
        self.failures = 0
        self.persistence_failures = 0

    def run(self, stop: threading.Event) -> None:
        self.state.update_health(
            "forecast_solar",
            status="starting",
            secret_in_main_config=False,
        )
        self._load_cache()
        while not stop.is_set():
            try:
                self._fetch()
                self.store.update_actual(self.state)
                self.state.update_health(
                    "forecast_solar",
                    status=(
                        "degraded"
                        if self.persistence_failures
                        else self.store.snapshot()["status"]
                    ),
                    successful_requests=self.requests,
                    failed_requests=self.failures,
                    persistence_failures=self.persistence_failures,
                    secret_in_main_config=False,
                )
                delay = self.config.refresh_interval_seconds
            except (OSError, ValueError, KeyError, HTTPError, URLError) as exc:
                self.failures += 1
                self.store.fail(f"{type(exc).__name__}: forecast refresh failed")
                snapshot = self.store.snapshot()
                self.state.update_health(
                    "forecast_solar",
                    status=(
                        "degraded"
                        if snapshot["status"] in ("ok", "stale")
                        else "failed"
                    ),
                    error="forecast refresh failed; cached data retained",
                    successful_requests=self.requests,
                    failed_requests=self.failures,
                    persistence_failures=self.persistence_failures,
                    secret_in_main_config=False,
                )
                delay = self.config.retry_interval_seconds
            stop.wait(delay)

    def _fetch(self) -> None:
        key = _read_key(self.config.api_key_file)
        latitude, longitude = _read_location(self.config.location_file)
        url = _endpoint(
            self.config,
            key=key,
            latitude=latitude,
            longitude=longitude,
        )
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "solplanet-fasttalk/0.3",
            },
        )
        with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
            raw = json.load(response)
        result = raw["result"]
        watts = result["watts"]
        points = [
            {
                "timestamp": self._timestamp(value).isoformat(),
                "power_w": float(power),
            }
            for value, power in sorted(watts.items())
        ]
        payload = {
            "fetched_at": utc_now(),
            "points": points,
            "daily_energy_wh": {
                key: float(value)
                for key, value in result.get("watt_hours_day", {}).items()
            },
        }
        self.store.replace(payload, cached=False)
        if self.history is not None:
            try:
                self.history.record_forecast(
                    "forecast.solar",
                    payload["fetched_at"],
                    points,
                    {
                        "plane_names": [
                            plane.name for plane in self.config.planes
                        ],
                        "scope": "combined",
                    },
                )
            except sqlite3.Error:
                self.persistence_failures += 1
        self._save_cache(payload)
        self.requests += 1

    def _timestamp(self, value: str) -> dt.datetime:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone, fold=0)
        return parsed.astimezone(dt.timezone.utc)

    def _load_cache(self) -> None:
        try:
            payload = json.loads(
                Path(self.config.cache_file).read_text(encoding="utf-8")
            )
            self.store.replace(payload, cached=True)
        except (OSError, ValueError, TypeError):
            return

    def _save_cache(self, payload: dict[str, Any]) -> None:
        path = Path(self.config.cache_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
