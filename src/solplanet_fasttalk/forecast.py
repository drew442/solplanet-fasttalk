"""Forecast.Solar adapter with secret-file, cache, and offline handling."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import ForecastSolarConfig
from .model import PlantState, utc_now


def solar_elevation_degrees(
    when: dt.datetime,
    latitude: float,
    longitude: float,
) -> float:
    """Approximate apparent solar elevation without retaining the location."""

    instant = when.astimezone(dt.timezone.utc)
    day = instant.timetuple().tm_yday
    hour = instant.hour + instant.minute / 60 + instant.second / 3600
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    equation = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    solar_minutes = hour * 60 + equation + 4 * longitude
    hour_angle = math.radians(solar_minutes / 4 - 180)
    latitude_radians = math.radians(latitude)
    cosine_zenith = (
        math.sin(latitude_radians) * math.sin(declination)
        + math.cos(latitude_radians)
        * math.cos(declination)
        * math.cos(hour_angle)
    )
    zenith = math.acos(max(-1.0, min(1.0, cosine_zenith)))
    return 90 - math.degrees(zenith)


def _weighted_median(values: list[tuple[float, float]]) -> float:
    ordered = sorted(values)
    total = sum(weight for _, weight in ordered)
    threshold = total / 2
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


class ForecastCorrector:
    """Robust two-timescale correction of the external PV forecast."""

    def __init__(self, config: ForecastSolarConfig, timezone: str, history) -> None:
        self.config = config
        self.timezone = ZoneInfo(timezone)
        self.history = history

    def correct(
        self,
        points: list[dict[str, Any]],
        *,
        issued_at: str,
        latitude: float,
        longitude: float,
        weather=None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = dt.datetime.fromisoformat(issued_at).astimezone(dt.timezone.utc)
        samples = self._training_samples(now)
        long_samples = [
            sample for sample in samples if sample["forecast_power_w"] >= 200
        ]
        days = {
            dt.datetime.fromisoformat(sample["forecast_at"])
            .astimezone(self.timezone)
            .date()
            for sample in long_samples
        }
        long_ready = len(long_samples) >= 250 and len(days) >= 14
        long_factor = (
            statistics.median(
                max(
                    0.5,
                    min(
                        1.5,
                        sample["actual_power_w"]
                        / sample["forecast_power_w"],
                    ),
                )
                for sample in long_samples
            )
            if long_ready
            else 1.0
        )

        # An aggregate east/west array can have materially different morning
        # and afternoon bias even when its all-day energy error is small. Learn
        # independent two-hour local-solar-time buckets only after each bucket
        # has enough days; otherwise retain the robust global fallback.
        bucket_samples: dict[int, list[dict[str, Any]]] = {}
        for sample in long_samples:
            local = dt.datetime.fromisoformat(sample["forecast_at"]).astimezone(
                self.timezone
            )
            bucket_samples.setdefault((local.hour // 2) * 2, []).append(sample)
        bucket_factors: dict[int, float] = {}
        if long_ready:
            for bucket, values in bucket_samples.items():
                bucket_days = {
                    dt.datetime.fromisoformat(value["forecast_at"])
                    .astimezone(self.timezone)
                    .date()
                    for value in values
                }
                if len(values) >= 40 and len(bucket_days) >= 7:
                    bucket_factors[bucket] = statistics.median(
                        max(
                            0.5,
                            min(
                                1.5,
                                value["actual_power_w"]
                                / value["forecast_power_w"],
                            ),
                        )
                        for value in values
                    )

        def factor_for(timestamp: dt.datetime) -> float:
            local = timestamp.astimezone(self.timezone)
            return bucket_factors.get((local.hour // 2) * 2, long_factor)

        short_start = now - dt.timedelta(minutes=120)
        recent = [
            sample
            for sample in long_samples
            if dt.datetime.fromisoformat(sample["forecast_at"]) >= short_start
        ]
        weighted = []
        for sample in recent:
            age_minutes = max(
                0.0,
                (
                    now - dt.datetime.fromisoformat(sample["forecast_at"])
                ).total_seconds()
                / 60,
            )
            sample_time = dt.datetime.fromisoformat(sample["forecast_at"])
            local_long_factor = factor_for(sample_time)
            ratio = max(
                0.4,
                min(
                    1.8,
                    sample["actual_power_w"]
                    / (sample["forecast_power_w"] * local_long_factor),
                ),
            )
            weighted.append((ratio, math.exp(-age_minutes / 60)))
        short_ready = len(weighted) >= 4
        short_residual = _weighted_median(weighted) if short_ready else 1.0

        current_weather = weather.closest(now) if weather is not None else None
        peak_w = sum(plane.peak_power_kw for plane in self.config.planes) * 1050
        corrected = []
        for point in points:
            timestamp = dt.datetime.fromisoformat(point["timestamp"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=self.timezone, fold=0)
            timestamp = timestamp.astimezone(dt.timezone.utc)
            horizon_hours = max(0.0, (timestamp - now).total_seconds() / 3600)
            elevation = solar_elevation_degrees(
                timestamp,
                latitude,
                longitude,
            )
            base = max(0.0, float(point["power_w"]))
            future_weather = (
                weather.closest(timestamp) if weather is not None else None
            )
            similarity = 1.0
            if current_weather and future_weather:
                similarity = math.exp(
                    -abs(
                        float(future_weather["clearness_ratio"])
                        - float(current_weather["clearness_ratio"])
                    )
                    / 0.2
                )
            persistence = math.exp(-horizon_hours / 2.0) * (
                0.35 + 0.65 * similarity
            )
            point_long_factor = factor_for(timestamp)
            factor = point_long_factor * (
                1 + (short_residual - 1) * persistence
            )
            factor = max(0.35, min(1.65, factor))
            power = 0.0 if elevation <= -0.833 else min(peak_w, base * factor)
            corrected.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "power_w": round(power, 3),
                    "base_power_w": round(base, 3),
                    "correction_factor": round(factor, 5),
                    "long_term_factor": round(point_long_factor, 5),
                    "short_term_factor": round(short_residual, 5),
                    "short_term_weight": round(persistence, 5),
                    "solar_elevation_degrees": round(elevation, 3),
                    "daylight_gate": elevation > -0.833,
                    "weather": (
                        {
                            "cloud_cover_percent": future_weather[
                                "cloud_cover_percent"
                            ],
                            "clearness_ratio": future_weather[
                                "clearness_ratio"
                            ],
                            "temperature_c": future_weather["temperature_c"],
                            "precipitation_probability_percent": (
                                future_weather[
                                    "precipitation_probability_percent"
                                ]
                            ),
                            "pv_potential_w": future_weather["pv_potential_w"],
                        }
                        if future_weather
                        else None
                    ),
                }
            )
        summary = {
            "method": "robust long-term median plus weather-aware short-term residual",
            "base_provider": "forecast.solar",
            "long_term_factor": round(long_factor, 5),
            "long_term_ready": long_ready,
            "long_term_samples": len(long_samples),
            "long_term_days": len(days),
            "long_term_required_samples": 250,
            "long_term_required_days": 14,
            "long_term_time_bucket_factors": {
                f"{hour:02d}:00-{hour + 2:02d}:00": round(value, 5)
                for hour, value in sorted(bucket_factors.items())
            },
            "short_term_factor": round(short_residual, 5),
            "short_term_factor_scope": "residual after long-term calibration",
            "short_term_ready": short_ready,
            "short_term_samples": len(recent),
            "short_term_window_minutes": 120,
            "short_term_decay_hours": 2,
            "weather_available": current_weather is not None,
            "daylight_gate": "local solar elevation above -0.833 degrees",
            "quality": (
                "mature"
                if long_ready and short_ready
                else "short_term_learning"
                if short_ready
                else "long_term_learning"
            ),
            "validation": self._validation(now),
        }
        summary["control_ready"] = bool(
            long_ready and summary["validation"]["passed"]
        )
        return corrected, summary

    def _training_samples(self, now: dt.datetime) -> list[dict[str, Any]]:
        if self.history is None or not hasattr(
            self.history,
            "forecast_comparison",
        ):
            return []
        return self.history.forecast_comparison(
            provider="forecast.solar",
            since=(now - dt.timedelta(days=60)).isoformat(),
            until=now.isoformat(),
            limit=10000,
        )

    def _validation(self, now: dt.datetime) -> dict[str, Any]:
        if self.history is None or not hasattr(self.history, "forecast_samples"):
            return {
                "passed": False,
                "reason": "history unavailable",
                "samples": 0,
                "days": 0,
                "by_horizon": {},
            }
        samples = self.history.forecast_samples(
            provider="fasttalk.corrected",
            since=(now - dt.timedelta(days=60)).isoformat(),
            until=now.isoformat(),
            limit=100000,
        )
        daylight = [
            sample
            for sample in samples
            if sample["forecast_power_w"] >= 50
            or sample["actual_power_w"] >= 50
        ]
        days = {
            dt.datetime.fromisoformat(sample["forecast_at"])
            .astimezone(self.timezone)
            .date()
            for sample in daylight
        }
        buckets = {
            "0_to_2_hours": [],
            "2_to_8_hours": [],
            "8_to_24_hours": [],
            "24_plus_hours": [],
        }
        for sample in daylight:
            horizon = sample["horizon_hours"]
            key = (
                "0_to_2_hours"
                if horizon <= 2
                else "2_to_8_hours"
                if horizon <= 8
                else "8_to_24_hours"
                if horizon <= 24
                else "24_plus_hours"
            )
            buckets[key].append(sample)
        peak_w = sum(plane.peak_power_kw for plane in self.config.planes) * 1000
        metrics = {}
        for name, values in buckets.items():
            errors = [value["error_w"] for value in values]
            metrics[name] = {
                "samples": len(values),
                "mae_w": (
                    round(statistics.mean(abs(error) for error in errors), 3)
                    if errors
                    else None
                ),
                "bias_w": (
                    round(statistics.mean(errors), 3) if errors else None
                ),
                "normalized_bias": (
                    round(statistics.mean(errors) / peak_w, 5)
                    if errors
                    else None
                ),
                "normalized_mae": (
                    round(
                        statistics.mean(abs(error) for error in errors)
                        / peak_w,
                        5,
                    )
                    if errors
                    else None
                ),
            }
        required = (
            metrics["0_to_2_hours"],
            metrics["2_to_8_hours"],
            metrics["8_to_24_hours"],
        )
        mature = len(days) >= 28 and all(item["samples"] >= 300 for item in required)
        accurate = mature and all(
            item["normalized_mae"] is not None
            and item["normalized_mae"] <= 0.15
            and abs(item["bias_w"]) / peak_w <= 0.08
            for item in required
        )
        return {
            "passed": accurate,
            "reason": (
                "accuracy thresholds passed"
                if accurate
                else "insufficient independent history"
                if not mature
                else "one or more horizon accuracy thresholds failed"
            ),
            "samples": len(daylight),
            "days": len(days),
            "required_days": 28,
            "required_samples_per_scored_horizon": 300,
            "maximum_normalized_mae": 0.15,
            "maximum_normalized_bias": 0.08,
            "by_horizon": metrics,
        }


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
                "base_forecast_power_w": closest.get(
                    "base_power_w",
                    forecast,
                ),
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
                "provider_daily_energy_wh": payload.get(
                    "provider_daily_energy_wh", {}
                ),
                "correction": payload.get("correction", {}),
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
        weather=None,
    ) -> None:
        self.config = config
        self.timezone = ZoneInfo(timezone)
        self.store = store
        self.state = state
        self.history = history
        self.weather = weather
        self.corrector = ForecastCorrector(config, timezone, history)
        self.requests = 0
        self.failures = 0
        self.persistence_failures = 0
        self.last_persistence_ok = True

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
                        if not self.last_persistence_ok
                        else self.store.snapshot()["status"]
                    ),
                    error=None,
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
                    "User-Agent": "solplanet-fasttalk/0.8.0",
            },
        )
        with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
            raw = json.load(response)
        result = raw["result"]
        watts = result["watts"]
        provider_points = [
            {
                "timestamp": self._timestamp(value).isoformat(),
                "power_w": float(power),
            }
            for value, power in sorted(watts.items())
        ]
        fetched_at = utc_now()
        points, correction = self.corrector.correct(
            provider_points,
            issued_at=fetched_at,
            latitude=latitude,
            longitude=longitude,
            weather=self.weather,
        )
        corrected_daily: dict[str, float] = {}
        for index, point in enumerate(points[:-1]):
            start = dt.datetime.fromisoformat(point["timestamp"])
            end = dt.datetime.fromisoformat(points[index + 1]["timestamp"])
            hours = min(1.0, max(0.0, (end - start).total_seconds() / 3600))
            local_day = start.astimezone(self.timezone).date().isoformat()
            corrected_daily[local_day] = corrected_daily.get(local_day, 0.0) + (
                float(point["power_w"]) * hours
            )
        payload = {
            "fetched_at": fetched_at,
            "points": points,
            "daily_energy_wh": {
                key: round(value, 3) for key, value in corrected_daily.items()
            },
            "provider_daily_energy_wh": {
                key: float(value)
                for key, value in result.get("watt_hours_day", {}).items()
            },
            "correction": correction,
        }
        self.store.replace(payload, cached=False)
        if self.history is not None:
            try:
                self.history.record_forecast(
                    "forecast.solar",
                    payload["fetched_at"],
                    provider_points,
                    {
                        "plane_names": [
                            plane.name for plane in self.config.planes
                        ],
                        "scope": "combined",
                    },
                )
                self.history.record_forecast(
                    "fasttalk.corrected",
                    payload["fetched_at"],
                    points,
                    {
                        "base_provider": "forecast.solar",
                        "correction": correction,
                    },
                )
                self.last_persistence_ok = True
            except sqlite3.Error:
                self.persistence_failures += 1
                self.last_persistence_ok = False
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
