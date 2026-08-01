"""Privacy-preserving Open-Meteo weather context for PV forecasting."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import ForecastPlane, WeatherConfig
from .forecast import _read_location
from .model import PlantState, utc_now


CONTEXT_VARIABLES = (
    "temperature_2m",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "precipitation_probability",
    "weather_code",
    "shortwave_radiation",
    "terrestrial_radiation",
    "is_day",
    "global_tilted_irradiance",
)


def _endpoint(
    config: WeatherConfig,
    plane: ForecastPlane,
    *,
    latitude: float,
    longitude: float,
    include_context: bool,
) -> str:
    variables = CONTEXT_VARIABLES if include_context else (
        "global_tilted_irradiance",
    )
    query = urlencode(
        {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "hourly": ",".join(variables),
            "timezone": "UTC",
            "forecast_days": config.forecast_days,
            "past_days": 1,
            "tilt": f"{plane.declination:g}",
            "azimuth": f"{plane.azimuth:g}",
        }
    )
    return f"https://api.open-meteo.com/v1/forecast?{query}"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


class WeatherStore:
    def __init__(self, config: WeatherConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._payload: dict[str, Any] | None = None
        self._error: str | None = None

    def replace(self, payload: dict[str, Any], *, cached: bool) -> None:
        with self._lock:
            self._payload = {**payload, "cached": cached}
            self._error = None

    def fail(self, error: str) -> None:
        with self._lock:
            self._error = error

    def closest(self, when: dt.datetime) -> dict[str, Any] | None:
        with self._lock:
            payload = self._payload or {}
            points = payload.get("points", [])
            if not points:
                return None
            fetched_at = payload.get("fetched_at")
            if not fetched_at:
                return None
            age = (
                dt.datetime.now(dt.timezone.utc)
                - dt.datetime.fromisoformat(fetched_at).astimezone(
                    dt.timezone.utc
                )
            ).total_seconds()
            if age > self.config.max_cache_age_seconds:
                return None
            target = when.astimezone(dt.timezone.utc).timestamp()
            point = min(
                points,
                key=lambda item: abs(
                    dt.datetime.fromisoformat(item["timestamp"]).timestamp()
                    - target
                ),
            )
            point_time = dt.datetime.fromisoformat(point["timestamp"]).timestamp()
            return dict(point) if abs(point_time - target) <= 5400 else None

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
                "provider": "open-meteo",
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
                "points": payload.get("points", []),
                "last_error": self._error,
                "location_private": True,
            }


class OpenMeteoWorker:
    def __init__(
        self,
        config: WeatherConfig,
        planes: tuple[ForecastPlane, ...],
        store: WeatherStore,
        state: PlantState,
        history=None,
    ) -> None:
        self.config = config
        self.planes = planes
        self.store = store
        self.state = state
        self.history = history
        self.requests = 0
        self.failures = 0

    def run(self, stop: threading.Event) -> None:
        self.state.update_health(
            "weather",
            status="starting",
            provider="open-meteo",
            location_private=True,
        )
        self.load_cache()
        while not stop.is_set():
            try:
                self._fetch()
                self.state.update_health(
                    "weather",
                    status=self.store.snapshot()["status"],
                    error=None,
                    successful_refreshes=self.requests,
                    failed_refreshes=self.failures,
                    location_private=True,
                )
                delay = self.config.refresh_interval_seconds
            except (OSError, ValueError, KeyError, HTTPError, URLError):
                self.failures += 1
                self.store.fail("weather refresh failed")
                snapshot = self.store.snapshot()
                self.state.update_health(
                    "weather",
                    status=(
                        "degraded"
                        if snapshot["status"] in ("ok", "stale")
                        else "failed"
                    ),
                    error="weather refresh failed; cached data retained",
                    successful_refreshes=self.requests,
                    failed_refreshes=self.failures,
                    location_private=True,
                )
                delay = self.config.retry_interval_seconds
            stop.wait(delay)

    def _fetch(self) -> None:
        latitude, longitude = _read_location(self.config.location_file)
        responses = []
        for index, plane in enumerate(self.planes):
            request = Request(
                _endpoint(
                    self.config,
                    plane,
                    latitude=latitude,
                    longitude=longitude,
                    include_context=index == 0,
                ),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "solplanet-fasttalk/0.5.0",
                },
            )
            with urlopen(
                request,
                timeout=self.config.request_timeout_seconds,
            ) as response:
                responses.append(json.load(response))

        context = responses[0]["hourly"]
        times = context["time"]
        irradiance_by_plane = [
            response["hourly"]["global_tilted_irradiance"]
            for response in responses
        ]
        points = []
        for index, raw_time in enumerate(times):
            timestamp = dt.datetime.fromisoformat(raw_time)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
            plane_irradiance = {
                plane.name: round(
                    max(0.0, _number(irradiance_by_plane[p][index])),
                    3,
                )
                for p, plane in enumerate(self.planes)
            }
            terrestrial = max(
                0.0,
                _number(context["terrestrial_radiation"][index]),
            )
            shortwave = max(
                0.0,
                _number(context["shortwave_radiation"][index]),
            )
            points.append(
                {
                    "timestamp": timestamp.astimezone(
                        dt.timezone.utc
                    ).isoformat(),
                    "temperature_c": _number(
                        context["temperature_2m"][index]
                    ),
                    "cloud_cover_percent": _number(
                        context["cloud_cover"][index]
                    ),
                    "cloud_cover_low_percent": _number(
                        context["cloud_cover_low"][index]
                    ),
                    "cloud_cover_mid_percent": _number(
                        context["cloud_cover_mid"][index]
                    ),
                    "cloud_cover_high_percent": _number(
                        context["cloud_cover_high"][index]
                    ),
                    "precipitation_probability_percent": _number(
                        context["precipitation_probability"][index]
                    ),
                    "weather_code": int(
                        _number(context["weather_code"][index])
                    ),
                    "shortwave_radiation_w_m2": round(shortwave, 3),
                    "terrestrial_radiation_w_m2": round(terrestrial, 3),
                    "clearness_ratio": round(
                        min(1.25, shortwave / terrestrial)
                        if terrestrial > 20
                        else 0.0,
                        4,
                    ),
                    "is_day": bool(_number(context["is_day"][index])),
                    "plane_irradiance_w_m2": plane_irradiance,
                    "pv_potential_w": round(
                        sum(
                            plane_irradiance[plane.name]
                            * plane.peak_power_kw
                            for plane in self.planes
                        ),
                        3,
                    ),
                }
            )
        payload = {"fetched_at": utc_now(), "points": points}
        self.store.replace(payload, cached=False)
        if self.history is not None:
            self.history.record_forecast(
                "open-meteo.weather-pv-potential",
                payload["fetched_at"],
                [
                    {
                        "timestamp": point["timestamp"],
                        "power_w": point["pv_potential_w"],
                    }
                    for point in points
                ],
                {"scope": "aggregate theoretical plane irradiance"},
            )
        self._save_cache(payload)
        self.requests += 1

    def load_cache(self) -> None:
        """Load sanitized weather before dependent workers are started."""

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
