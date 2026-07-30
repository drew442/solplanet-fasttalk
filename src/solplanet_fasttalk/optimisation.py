"""Constrained battery planning and replay simulation in shadow mode only."""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import asdict, dataclass
from typing import Any

from .config import OptimisationConfig
from .forecast import ForecastStore
from .model import PlantState, utc_now
from .tariff import ZeroHeroTariff


@dataclass(frozen=True)
class ForecastSlot:
    timestamp: dt.datetime
    load_w: float
    pv_w: float


@dataclass(frozen=True)
class Recommendation:
    timestamp: str
    action: str
    battery_power_w: float
    expected_grid_power_w: float
    expected_soc_percent: float
    import_price_per_kwh: float
    export_price_per_kwh: float
    explanation: tuple[str, ...]
    constraints: dict[str, float]
    feasible: bool


def _bill(
    tariff: ZeroHeroTariff,
    flows: list[tuple[dt.datetime, float, float]],
    step_minutes: int,
) -> float:
    """Price interval import/export including ZEROHERO and export caps."""

    cost = 0.0
    super_export_used: dict[dt.date, float] = {}
    zerohero_import: dict[tuple[dt.date, int], float] = {}
    zerohero_slots: dict[tuple[dt.date, int], int] = {}
    for timestamp, imported, exported in flows:
        quote = tariff.quote(timestamp)
        local = timestamp.astimezone(tariff.timezone)
        cost += imported * quote.import_price_per_kwh
        if quote.export_period == "super_export":
            used = super_export_used.get(local.date(), 0.0)
            premium = min(exported, max(0.0, 15.0 - used))
            base = exported - premium
            cost -= premium * 0.15 + base * 0.05
            super_export_used[local.date()] = used + exported
        else:
            cost -= exported * quote.export_price_per_kwh
        if quote.zerohero_window:
            key = (local.date(), local.hour)
            zerohero_import[key] = zerohero_import.get(key, 0.0) + imported
            zerohero_slots[key] = zerohero_slots.get(key, 0) + 1
    expected_slots = 60 // step_minutes
    dates = {key[0] for key in zerohero_import}
    for day in dates:
        keys = [(day, hour) for hour in (18, 19, 20)]
        if all(
            zerohero_slots.get(key, 0) >= expected_slots
            and zerohero_import.get(key, 0.0) <= 0.03
            for key in keys
        ):
            cost -= 1.0
    return cost


class PlanStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plan: dict[str, Any] = {
            "mode": "shadow",
            "status": "starting",
            "control_commands_sent": 0,
            "recommendations": [],
        }

    def replace(self, value: dict[str, Any]) -> None:
        with self._lock:
            self._plan = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._plan)


def _no_action(reason: str, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "mode": "shadow",
        "status": "no_action",
        "reason": reason,
        "inputs": inputs,
        "recommendations": [],
        "simulation": None,
        "control_commands_sent": 0,
        "execution_available": False,
    }


def simulate_plan(
    config: OptimisationConfig,
    tariff: ZeroHeroTariff,
    slots: list[ForecastSlot],
    *,
    initial_soc_percent: float,
    charge_limit_w: float,
    discharge_limit_w: float,
) -> dict[str, Any]:
    """Create a feasible greedy schedule and compare it with no battery action."""

    step_hours = config.step_minutes / 60.0
    capacity_wh = config.battery_capacity_kwh * 1000.0
    minimum_wh = capacity_wh * config.reserve_soc_percent / 100.0
    maximum_wh = capacity_wh * config.maximum_soc_percent / 100.0
    stored_wh = min(
        maximum_wh,
        max(minimum_wh, capacity_wh * initial_soc_percent / 100.0),
    )
    baseline_import_kwh = baseline_export_kwh = 0.0
    optimized_import_kwh = optimized_export_kwh = 0.0
    recommendations: list[Recommendation] = []
    baseline_flows: list[tuple[dt.datetime, float, float]] = []
    optimized_flows: list[tuple[dt.datetime, float, float]] = []
    plan_feasible = True

    future_import_prices = [
        tariff.quote(slot.timestamp).import_price_per_kwh for slot in slots
    ]
    for index, slot in enumerate(slots):
        quote = tariff.quote(slot.timestamp)
        natural_grid = slot.load_w - slot.pv_w
        baseline_import = max(0.0, natural_grid) * step_hours / 1000.0
        baseline_export = max(0.0, -natural_grid) * step_hours / 1000.0
        baseline_import_kwh += baseline_import
        baseline_export_kwh += baseline_export
        baseline_flows.append(
            (slot.timestamp, baseline_import, baseline_export)
        )

        charge_w = 0.0
        discharge_w = 0.0
        explanations: list[str] = []
        room_input_w = max(
            0.0,
            (maximum_wh - stored_wh)
            / max(step_hours * config.charge_efficiency, 0.001),
        )
        available_output_w = max(
            0.0,
            (stored_wh - minimum_wh)
            * config.discharge_efficiency
            / max(step_hours, 0.001),
        )
        if natural_grid < 0:
            charge_w = min(
                -natural_grid,
                charge_limit_w,
                room_input_w,
            )
            if charge_w:
                explanations.append(
                    "charge from forecast PV surplus to maximise self-consumption"
                )
        elif natural_grid > 0:
            discharge_w = min(
                natural_grid,
                discharge_limit_w,
                available_output_w,
            )
            if discharge_w:
                explanations.append(
                    "discharge to serve forecast site load before grid import"
                )

        later_peak = max(future_import_prices[index + 1 :], default=0.0)
        if (
            natural_grid >= 0
            and quote.import_price_per_kwh
            + config.minimum_arbitrage_margin_per_kwh
            < later_peak
            and room_input_w > charge_w
        ):
            grid_charge = min(
                charge_limit_w - charge_w,
                room_input_w - charge_w,
                config.site_import_limit_watts - natural_grid,
            )
            if grid_charge > 0:
                charge_w += grid_charge
                discharge_w = 0.0
                explanations.append(
                    "charge before a later higher import-price period"
                )

        battery_power = discharge_w - charge_w
        expected_grid = natural_grid - battery_power
        if expected_grid > config.site_import_limit_watts:
            extra = min(
                expected_grid - config.site_import_limit_watts,
                discharge_limit_w - discharge_w,
                available_output_w - discharge_w,
            )
            discharge_w += max(0.0, extra)
            battery_power = discharge_w - charge_w
            expected_grid = natural_grid - battery_power
            explanations.append("discharge constrained by configured site import limit")
        if expected_grid < -config.site_export_limit_watts:
            extra = min(
                -config.site_export_limit_watts - expected_grid,
                charge_limit_w - charge_w,
                room_input_w - charge_w,
            )
            charge_w += max(0.0, extra)
            battery_power = discharge_w - charge_w
            expected_grid = natural_grid - battery_power
            explanations.append("charge constrained by configured site export limit")
        feasible = (
            expected_grid <= config.site_import_limit_watts + 0.001
            and expected_grid >= -config.site_export_limit_watts - 0.001
        )
        if not feasible:
            plan_feasible = False
            explanations.append(
                "forecast site flow exceeds the controllable site boundary"
            )

        stored_wh += (
            charge_w * step_hours * config.charge_efficiency
            - discharge_w
            * step_hours
            / config.discharge_efficiency
        )
        stored_wh = min(maximum_wh, max(minimum_wh, stored_wh))
        optimized_import = max(0.0, expected_grid) * step_hours / 1000.0
        optimized_export = max(0.0, -expected_grid) * step_hours / 1000.0
        optimized_import_kwh += optimized_import
        optimized_export_kwh += optimized_export
        optimized_flows.append(
            (slot.timestamp, optimized_import, optimized_export)
        )
        action = "discharge" if battery_power > 0 else "charge" if battery_power < 0 else "hold"
        if not explanations:
            explanations.append("hold: no beneficial or constraint-driven action")
        explanations.extend(
            (
                f"forecast load {slot.load_w:.0f} W and PV {slot.pv_w:.0f} W",
                (
                    f"tariff import ${quote.import_price_per_kwh:.3f}/kWh "
                    f"and export ${quote.export_price_per_kwh:.3f}/kWh"
                ),
                (
                    f"SOC kept within {config.reserve_soc_percent:.1f}%–"
                    f"{config.maximum_soc_percent:.1f}%"
                ),
            )
        )
        recommendations.append(
            Recommendation(
                slot.timestamp.astimezone(dt.timezone.utc).isoformat(),
                action,
                round(battery_power, 3),
                round(expected_grid, 3),
                round(stored_wh / capacity_wh * 100.0, 3),
                quote.import_price_per_kwh,
                quote.export_price_per_kwh,
                tuple(explanations),
                {
                    "charge_limit_w": charge_limit_w,
                    "discharge_limit_w": discharge_limit_w,
                    "site_import_limit_w": config.site_import_limit_watts,
                    "site_export_limit_w": config.site_export_limit_watts,
                    "reserve_soc_percent": config.reserve_soc_percent,
                    "maximum_soc_percent": config.maximum_soc_percent,
                },
                feasible,
            )
        )

    baseline_cost = _bill(tariff, baseline_flows, config.step_minutes)
    optimized_cost = _bill(tariff, optimized_flows, config.step_minutes)
    return {
        "recommendations": [asdict(item) for item in recommendations],
        "feasible": plan_feasible,
        "simulation": {
            "baseline": {
                "cost": round(baseline_cost, 6),
                "import_kwh": round(baseline_import_kwh, 6),
                "export_kwh": round(baseline_export_kwh, 6),
            },
            "optimized": {
                "cost": round(optimized_cost, 6),
                "import_kwh": round(optimized_import_kwh, 6),
                "export_kwh": round(optimized_export_kwh, 6),
            },
            "estimated_cost_improvement": round(
                baseline_cost - optimized_cost, 6
            ),
            "model": (
                "deterministic shadow replay; includes ZEROHERO eligibility "
                "and Super Export cap; excludes equal daily supply charge"
            ),
        },
    }


class OptimisationWorker:
    def __init__(
        self,
        config: OptimisationConfig,
        state: PlantState,
        forecast: ForecastStore,
        tariff: ZeroHeroTariff,
        plans: PlanStore,
        history=None,
    ) -> None:
        self.config = config
        self.state = state
        self.forecast = forecast
        self.tariff = tariff
        self.plans = plans
        self.history = history
        self.runs = 0

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.plan_once()
            stop.wait(self.config.interval_seconds)

    def plan_once(self) -> dict[str, Any]:
        current = self.state.current()
        required = {
            name: current.get(name)
            for name in (
                "battery.soc",
                "battery.voltage",
                "battery.limit.charge_current",
                "battery.limit.discharge_current",
                "grid.active_power",
                "site.load_power",
            )
        }
        bad = [
            name
            for name, value in required.items()
            if not value
            or value["quality"] != "good"
            or not isinstance(value["value"], (int, float))
        ]
        inputs = {
            name: (
                {
                    "value": value["value"],
                    "unit": value["unit"],
                    "observed_at": value["observed_at"],
                    "age_seconds": value["age_seconds"],
                    "quality": value["quality"],
                    "source": value["source"],
                }
                if value
                else None
            )
            for name, value in required.items()
        }
        if bad:
            plan = _no_action(
                "required measurement unavailable or stale: " + ", ".join(bad),
                inputs,
            )
            self.plans.replace(plan)
            return plan
        forecast = self.forecast.snapshot()
        if forecast["status"] != "ok" or not forecast["points"]:
            plan = _no_action("current PV forecast unavailable or stale", inputs)
            self.plans.replace(plan)
            return plan

        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now + dt.timedelta(hours=self.config.horizon_hours)
        load_w = float(required["site.load_power"]["value"])
        load_profile = self._load_profile(now)
        step_seconds = self.config.step_minutes * 60
        pv_buckets: dict[int, list[float]] = {}
        for point in forecast["points"]:
            timestamp = dt.datetime.fromisoformat(point["timestamp"])
            if now <= timestamp <= cutoff:
                bucket = int(timestamp.timestamp()) // step_seconds
                pv_buckets.setdefault(bucket, []).append(float(point["power_w"]))
        slots = []
        for bucket, samples in sorted(pv_buckets.items()):
            timestamp = dt.datetime.fromtimestamp(
                bucket * step_seconds, tz=dt.timezone.utc
            )
            local = timestamp.astimezone(self.tariff.timezone)
            forecast_load = load_profile.get(
                (local.weekday(), local.hour), load_w
            )
            slots.append(
                ForecastSlot(
                    timestamp,
                    forecast_load,
                    sum(samples) / len(samples),
                )
            )
        if not slots:
            plan = _no_action("forecast has no points in planning horizon", inputs)
            self.plans.replace(plan)
            return plan
        voltage = required["battery.voltage"]
        charge_current = required["battery.limit.charge_current"]
        discharge_current = required["battery.limit.discharge_current"]
        observed_charge = float(voltage["value"]) * float(
            charge_current["value"]
        )
        observed_discharge = float(voltage["value"]) * float(
            discharge_current["value"]
        )
        charge_limit = min(self.config.max_charge_watts, observed_charge)
        discharge_limit = min(
            self.config.max_discharge_watts, observed_discharge
        )
        result = simulate_plan(
            self.config,
            self.tariff,
            slots,
            initial_soc_percent=float(required["battery.soc"]["value"]),
            charge_limit_w=charge_limit,
            discharge_limit_w=discharge_limit,
        )
        plan = {
            "generated_at": utc_now(),
            "mode": "shadow",
            "status": "ready" if result["feasible"] else "infeasible",
            "inputs": inputs,
            "load_forecast": {
                "method": (
                    "historical weekday/hour mean with current-load fallback"
                    if load_profile
                    else "current-load persistence"
                ),
                "historical_buckets": len(load_profile),
            },
            **result,
            "control_commands_sent": 0,
            "execution_available": False,
        }
        self.plans.replace(plan)
        self.runs += 1
        self.state.update_health(
            "optimisation",
            status="ok",
            mode="shadow",
            plans_generated=self.runs,
            control_commands_sent=0,
        )
        return plan

    def _load_profile(self, now: dt.datetime) -> dict[tuple[int, int], float]:
        if self.history is None:
            return {}
        since = (now - dt.timedelta(days=35)).isoformat()
        samples = self.history.measurements(
            "site.load_power",
            since=since,
            limit=1000,
            resolution="hourly",
        )
        buckets: dict[tuple[int, int], list[float]] = {}
        for sample in samples:
            if not isinstance(sample["value"], (int, float)):
                continue
            timestamp = dt.datetime.fromisoformat(sample["observed_at"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
            local = timestamp.astimezone(self.tariff.timezone)
            buckets.setdefault((local.weekday(), local.hour), []).append(
                max(0.0, float(sample["value"]))
            )
        return {
            key: sum(values) / len(values) for key, values in buckets.items()
        }
