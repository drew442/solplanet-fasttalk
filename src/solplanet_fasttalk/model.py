"""Thread-safe current plant model with provenance and freshness."""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import asdict, dataclass, field
from queue import Full, Queue
from typing import Any, Callable


Value = int | float | str | bool | None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Measurement:
    name: str
    value: Value
    unit: str
    source: str
    authority: str
    access_mode: str
    observed_at: str
    observed_monotonic: float
    max_age_seconds: float
    quality: str = "good"
    metadata: dict[str, Any] = field(default_factory=dict)

    def current_dict(self, now: float | None = None) -> dict[str, Any]:
        current = asdict(self)
        current.pop("observed_monotonic")
        age = max(0.0, (time.monotonic() if now is None else now) - self.observed_monotonic)
        current["age_seconds"] = round(age, 3)
        if current["quality"] == "good" and age > self.max_age_seconds:
            current["quality"] = "stale"
        return current


class PlantState:
    DERIVATION_INPUTS = (
        "grid.active_power",
        "external_pv.active_power",
        "asw.active_power",
        "asw.pv.active_power",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._measurements: dict[str, Measurement] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._sinks: list[Callable[[Measurement], None]] = []

    def add_sink(self, sink: Callable[[Measurement], None]) -> None:
        with self._lock:
            self._sinks.append(sink)

    def publish(self, measurement: Measurement) -> None:
        with self._changed:
            self._measurements[measurement.name] = measurement
            derived = (
                self._derive_locked(measurement.observed_monotonic)
                if measurement.name in self.DERIVATION_INPUTS
                else []
            )
            self._sequence += 1
            sinks = tuple(self._sinks)
            self._changed.notify_all()
        for item in (measurement, *derived):
            for sink in sinks:
                sink(item)

    def publish_many(self, measurements: list[Measurement]) -> None:
        if not measurements:
            return
        with self._changed:
            for measurement in measurements:
                self._measurements[measurement.name] = measurement
            derived = (
                self._derive_locked(
                    max(item.observed_monotonic for item in measurements)
                )
                if any(
                    item.name in self.DERIVATION_INPUTS for item in measurements
                )
                else []
            )
            self._sequence += 1
            sinks = tuple(self._sinks)
            self._changed.notify_all()
        for measurement in [*measurements, *derived]:
            for sink in sinks:
                sink(measurement)

    def _derive_locked(self, now: float) -> list[Measurement]:
        names = self.DERIVATION_INPUTS
        inputs = [self._measurements.get(name) for name in names]
        if any(item is None for item in inputs):
            self._clear_derived_locked()
            return []
        typed = [item for item in inputs if item is not None]
        if any(
            item.value is None
            or not isinstance(item.value, (int, float))
            or item.quality != "good"
            or now - item.observed_monotonic > item.max_age_seconds
            for item in typed
        ):
            self._clear_derived_locked()
            return []
        grid, external, asw, asw_pv = typed
        expiry = min(
            item.observed_monotonic + item.max_age_seconds for item in typed
        )
        max_age = max(0.001, expiry - now)
        value = (
            float(grid.value)
            + float(external.value)
            + float(asw.value)
        )
        # ASW AC power includes battery charge/discharge. It is not PV power.
        # The initial plant has no PV connected to the ASW, and the dedicated
        # ASW PV register correctly reports zero. Keep these concepts separate
        # so battery discharge can never appear as night-time solar production.
        generation = max(0.0, float(external.value)) + max(
            0.0, float(asw_pv.value)
        )
        export = max(0.0, -float(grid.value))
        load = max(0.0, value)
        grid_import = max(0.0, float(grid.value))
        self_consumed = max(0.0, min(generation, generation - export))
        local_supply = max(0.0, min(load, load - grid_import))
        specifications = (
            (
                "site.load_power",
                round(load, 3),
                "W",
                "max(0, grid.active_power + external_pv.active_power + asw.active_power)",
            ),
            (
                "site.generation_power",
                round(generation, 3),
                "W",
                "max(0, external_pv.active_power) + max(0, asw.pv.active_power)",
            ),
            (
                "site.pv_generation_power",
                round(generation, 3),
                "W",
                "max(0, external_pv.active_power) + max(0, asw.pv.active_power)",
            ),
            (
                "site.local_supply_power",
                round(local_supply, 3),
                "W",
                "max(0, min(site.load_power, site.load_power - grid import))",
            ),
            (
                "site.self_consumption_power",
                round(self_consumed, 3),
                "W",
                "max(0, min(site.generation_power, site.generation_power - grid export))",
            ),
            (
                "site.self_consumption_ratio",
                round(self_consumed / generation, 6) if generation else None,
                "ratio",
                "site.self_consumption_power / site.generation_power",
            ),
            (
                "site.self_sufficiency_ratio",
                round(local_supply / load, 6) if load else None,
                "ratio",
                "site.local_supply_power / site.load_power",
            ),
        )
        derived = [
            Measurement(
                name,
                result,
                unit,
                "plant_model",
                "derived",
                "calculated",
                utc_now(),
                now,
                max_age,
                "unavailable" if result is None else "good",
                {"formula": formula, "inputs": list(names)},
            )
            for name, result, unit, formula in specifications
        ]
        self._measurements.update({item.name: item for item in derived})
        return derived

    def _clear_derived_locked(self) -> None:
        for name in (
            "site.load_power",
            "site.generation_power",
            "site.pv_generation_power",
            "site.local_supply_power",
            "site.self_consumption_power",
            "site.self_consumption_ratio",
            "site.self_sufficiency_ratio",
        ):
            self._measurements.pop(name, None)

    def update_health(self, component: str, **values: Any) -> None:
        with self._changed:
            health = self._health.setdefault(component, {})
            health.update(values)
            health["updated_at"] = utc_now()
            self._sequence += 1
            self._changed.notify_all()

    def current(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            result = {}
            for name, measurement in sorted(self._measurements.items()):
                current = measurement.current_dict(now)
                if (
                    measurement.source == "plant_model"
                    and current["quality"] == "stale"
                ):
                    continue
                result[name] = current
            return result

    def plant(self) -> dict[str, Any]:
        current = self.current()
        prefixes = ("grid.", "external_pv.", "asw.", "battery.", "site.")
        return {
            "timestamp": utc_now(),
            "measurements": {
                name: value
                for name, value in current.items()
                if name.startswith(prefixes)
            },
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            components = {
                name: dict(value) for name, value in sorted(self._health.items())
            }
        qualities: dict[str, int] = {}
        for measurement in self.current().values():
            quality = measurement["quality"]
            qualities[quality] = qualities.get(quality, 0) + 1
        states = [value.get("status", "unknown") for value in components.values()]
        overall = (
            "failed"
            if "failed" in states
            else "degraded"
            if "degraded" in states or qualities.get("stale", 0)
            else "ok"
            if states and all(value == "ok" for value in states)
            else "starting"
        )
        return {
            "status": overall,
            "timestamp": utc_now(),
            "components": components,
            "measurement_quality": qualities,
        }

    def wait_for_change(self, sequence: int, timeout: float) -> int:
        with self._changed:
            if sequence == self._sequence:
                self._changed.wait(timeout)
            return self._sequence

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence


class MeasurementQueue:
    """Bounded persistence sink that never blocks serial workers."""

    def __init__(self, maxsize: int = 10000) -> None:
        self.queue: Queue[Measurement | None] = Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, measurement: Measurement) -> None:
        try:
            self.queue.put_nowait(measurement)
        except Full:
            self.dropped += 1
