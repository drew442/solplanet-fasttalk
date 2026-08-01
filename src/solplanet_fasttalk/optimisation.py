"""Constrained battery planning and replay simulation in shadow mode only."""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
import statistics
import threading
from dataclasses import asdict, dataclass
from typing import Any

from .config import OptimisationConfig
from .forecast import ForecastStore
from .model import PlantState, utc_now
from .tariff import ZeroHeroTariff


ASW12KH_T3_MAX_BATTERY_CHARGE_W = 12000.0
ASW12KH_T3_MAX_BATTERY_DISCHARGE_W = 12000.0
PREDICTION_ARCHIVE_INTERVAL_SECONDS = 3 * 3600


@dataclass(frozen=True)
class ForecastSlot:
    timestamp: dt.datetime
    load_w: float
    pv_w: float
    base_pv_w: float | None = None
    load_lower_w: float | None = None
    load_upper_w: float | None = None
    load_samples: int = 0
    weather: dict[str, Any] | None = None


@dataclass(frozen=True)
class LoadBucket:
    median_w: float
    lower_w: float
    upper_w: float
    samples: int


@dataclass(frozen=True)
class Recommendation:
    timestamp: str
    action: str
    forecast_load_w: float
    forecast_pv_w: float
    forecast_base_pv_w: float
    forecast_load_lower_w: float
    forecast_load_upper_w: float
    forecast_load_samples: int
    weather: dict[str, Any] | None
    baseline_grid_power_w: float
    baseline_battery_power_w: float
    baseline_expected_soc_percent: float
    battery_power_w: float
    expected_grid_power_w: float
    expected_soc_percent: float
    expected_soc_lower_percent: float
    expected_soc_upper_percent: float
    import_price_per_kwh: float
    export_price_per_kwh: float
    explanation: tuple[str, ...]
    constraints: dict[str, float]
    feasible: bool


@dataclass(frozen=True)
class NativeBaseline:
    mode: str = "hold"
    requested_power_w: float = 0.0
    minimum_soc_percent: float = 0.0
    maximum_soc_percent: float = 100.0
    source: str = "fallback"
    assumption: str = (
        "hold battery because a fresh native inverter command was unavailable"
    )


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


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
    native_baseline: NativeBaseline | None = None,
) -> dict[str, Any]:
    """Compare a shadow schedule with continuation of the native command."""

    baseline_policy = native_baseline or NativeBaseline()
    charge_limit_w = max(
        0.0,
        min(charge_limit_w, ASW12KH_T3_MAX_BATTERY_CHARGE_W),
    )
    discharge_limit_w = max(
        0.0,
        min(discharge_limit_w, ASW12KH_T3_MAX_BATTERY_DISCHARGE_W),
    )
    step_hours = config.step_minutes / 60.0
    capacity_wh = config.battery_capacity_kwh * 1000.0
    minimum_wh = capacity_wh * config.reserve_soc_percent / 100.0
    maximum_wh = capacity_wh * config.maximum_soc_percent / 100.0
    stored_wh = min(
        maximum_wh,
        max(minimum_wh, capacity_wh * initial_soc_percent / 100.0),
    )
    baseline_minimum_wh = capacity_wh * max(
        0.0, min(100.0, baseline_policy.minimum_soc_percent)
    ) / 100.0
    baseline_maximum_wh = capacity_wh * max(
        baseline_policy.minimum_soc_percent,
        min(100.0, baseline_policy.maximum_soc_percent),
    ) / 100.0
    baseline_stored_wh = min(
        baseline_maximum_wh,
        max(
            baseline_minimum_wh,
            capacity_wh * initial_soc_percent / 100.0,
        ),
    )
    baseline_import_kwh = baseline_export_kwh = 0.0
    optimized_import_kwh = optimized_export_kwh = 0.0
    recommendations: list[Recommendation] = []
    baseline_flows: list[tuple[dt.datetime, float, float]] = []
    optimized_flows: list[tuple[dt.datetime, float, float]] = []
    plan_feasible = True
    cumulative_load_uncertainty_wh = 0.0

    future_import_prices = [
        tariff.quote(slot.timestamp).import_price_per_kwh for slot in slots
    ]
    for index, slot in enumerate(slots):
        quote = tariff.quote(slot.timestamp)
        natural_grid = slot.load_w - slot.pv_w
        baseline_battery_power = 0.0
        if baseline_policy.mode == "charge":
            room_input_w = max(
                0.0,
                (baseline_maximum_wh - baseline_stored_wh)
                / max(step_hours * config.charge_efficiency, 0.001),
            )
            baseline_battery_power = -min(
                abs(baseline_policy.requested_power_w),
                charge_limit_w,
                room_input_w,
            )
        elif baseline_policy.mode == "discharge":
            available_output_w = max(
                0.0,
                (baseline_stored_wh - baseline_minimum_wh)
                * config.discharge_efficiency
                / max(step_hours, 0.001),
            )
            baseline_battery_power = min(
                abs(baseline_policy.requested_power_w),
                discharge_limit_w,
                available_output_w,
            )
        baseline_stored_wh += (
            max(0.0, -baseline_battery_power)
            * step_hours
            * config.charge_efficiency
            - max(0.0, baseline_battery_power)
            * step_hours
            / config.discharge_efficiency
        )
        baseline_stored_wh = min(
            baseline_maximum_wh,
            max(baseline_minimum_wh, baseline_stored_wh),
        )
        baseline_grid = natural_grid - baseline_battery_power
        baseline_import = max(0.0, baseline_grid) * step_hours / 1000.0
        baseline_export = max(0.0, -baseline_grid) * step_hours / 1000.0
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
        load_lower = slot.load_w if slot.load_lower_w is None else slot.load_lower_w
        load_upper = slot.load_w if slot.load_upper_w is None else slot.load_upper_w
        cumulative_load_uncertainty_wh += max(
            abs(slot.load_w - load_lower),
            abs(load_upper - slot.load_w),
        ) * step_hours / min(config.charge_efficiency, config.discharge_efficiency)
        expected_soc = stored_wh / capacity_wh * 100.0
        soc_uncertainty = cumulative_load_uncertainty_wh / capacity_wh * 100.0
        expected_soc_lower = max(
            config.reserve_soc_percent,
            expected_soc - soc_uncertainty,
        )
        expected_soc_upper = min(
            config.maximum_soc_percent,
            expected_soc + soc_uncertainty,
        )
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
                round(slot.load_w, 3),
                round(slot.pv_w, 3),
                round(
                    slot.pv_w if slot.base_pv_w is None else slot.base_pv_w,
                    3,
                ),
                round(
                    slot.load_w
                    if slot.load_lower_w is None
                    else slot.load_lower_w,
                    3,
                ),
                round(
                    slot.load_w
                    if slot.load_upper_w is None
                    else slot.load_upper_w,
                    3,
                ),
                slot.load_samples,
                slot.weather,
                round(baseline_grid, 3),
                round(baseline_battery_power, 3),
                round(baseline_stored_wh / capacity_wh * 100.0, 3),
                round(battery_power, 3),
                round(expected_grid, 3),
                round(expected_soc, 3),
                round(expected_soc_lower, 3),
                round(expected_soc_upper, 3),
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
                    "manufacturer_max_charge_w": (
                        ASW12KH_T3_MAX_BATTERY_CHARGE_W
                    ),
                    "manufacturer_max_discharge_w": (
                        ASW12KH_T3_MAX_BATTERY_DISCHARGE_W
                    ),
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
                "policy": asdict(baseline_policy),
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
                "deterministic shadow comparison against continuation of the "
                "current native inverter command; includes ZEROHERO eligibility "
                "and Super Export cap; excludes equal daily supply charge"
            ),
        },
        "hardware_limits": {
            "model": "ASW12kH-T3",
            "manufacturer_max_charge_w": ASW12KH_T3_MAX_BATTERY_CHARGE_W,
            "manufacturer_max_discharge_w": ASW12KH_T3_MAX_BATTERY_DISCHARGE_W,
            "planning_interval_seconds": config.step_minutes * 60,
            "basis": (
                "manufacturer battery charge/discharge rating, further capped "
                "by configured and live BMS voltage×current limits"
            ),
            "excluded_limit": (
                "24 kVA EPS overload rating is limited to 10 seconds and is "
                "not used for battery dispatch planning"
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
        self.persistence_failures = 0

    def run(self, stop: threading.Event) -> None:
        self.state.update_health(
            "optimisation",
            status="starting",
            mode="shadow",
            control_commands_sent=0,
        )
        while not stop.is_set():
            plan = self.plan_once()
            if plan["status"] == "no_action":
                self.state.update_health(
                    "optimisation",
                    status="degraded",
                    mode="shadow",
                    reason=plan["reason"],
                    plans_generated=self.runs,
                    persistence_failures=self.persistence_failures,
                    control_commands_sent=0,
                )
                delay = min(30, self.config.interval_seconds)
            elif plan["status"] == "infeasible":
                self.state.update_health(
                    "optimisation",
                    status="degraded",
                    mode="shadow",
                    reason="forecast exceeds controllable site constraints",
                    plans_generated=self.runs,
                    persistence_failures=self.persistence_failures,
                    control_commands_sent=0,
                )
                delay = self.config.interval_seconds
            else:
                delay = self.config.interval_seconds
            stop.wait(delay)

    def plan_once(self) -> dict[str, Any]:
        current = self.state.current()
        required_names = (
            "battery.soc",
            "battery.voltage",
            "battery.limit.charge_current",
            "battery.limit.discharge_current",
            "grid.active_power",
            "site.load_power",
        )
        input_names = required_names + (
            "asw.control.charge_discharge_state",
            "asw.control.power_command",
            "battery.limit.soc_lower",
            "battery.limit.soc_upper",
        )
        required = {
            name: current.get(name)
            for name in input_names
        }
        bad = [
            name
            for name in required_names
            for value in (required[name],)
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
            self._publish(plan)
            return plan
        forecast = self.forecast.snapshot()
        if forecast["status"] != "ok" or not forecast["points"]:
            plan = _no_action("current PV forecast unavailable or stale", inputs)
            self._publish(plan)
            return plan

        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now + dt.timedelta(hours=self.config.horizon_hours)
        load_w = float(required["site.load_power"]["value"])
        load_profile = self._load_profile(now)
        load_correction, load_correction_samples = self._load_correction(
            now,
            load_profile,
            load_w,
        )
        step_seconds = self.config.step_minutes * 60
        pv_buckets: dict[int, list[float]] = {}
        base_pv_buckets: dict[int, list[float]] = {}
        weather_buckets: dict[int, list[dict[str, Any]]] = {}
        for point in forecast["points"]:
            timestamp = dt.datetime.fromisoformat(point["timestamp"])
            if now <= timestamp <= cutoff:
                bucket = int(timestamp.timestamp()) // step_seconds
                pv_buckets.setdefault(bucket, []).append(float(point["power_w"]))
                base_pv_buckets.setdefault(bucket, []).append(
                    float(point.get("base_power_w", point["power_w"]))
                )
                if point.get("weather"):
                    weather_buckets.setdefault(bucket, []).append(
                        point["weather"]
                    )
        slots = []
        for bucket, samples in sorted(pv_buckets.items()):
            timestamp = dt.datetime.fromtimestamp(
                bucket * step_seconds, tz=dt.timezone.utc
            )
            local = timestamp.astimezone(self.tariff.timezone)
            key = (local.weekday(), local.hour, local.minute // 15)
            load_bucket = load_profile.get(key)
            long_load = load_bucket.median_w if load_bucket else load_w
            long_lower = (
                load_bucket.lower_w if load_bucket else max(0.0, load_w * 0.7)
            )
            long_upper = load_bucket.upper_w if load_bucket else load_w * 1.3
            horizon_hours = max(
                0.0,
                (timestamp - now).total_seconds() / 3600,
            )
            correction = 1 + (load_correction - 1) * math.exp(
                -horizon_hours / 4
            )
            forecast_load = max(0.0, long_load * correction)
            forecast_load_lower = max(0.0, long_lower * correction)
            forecast_load_upper = max(
                forecast_load_lower,
                long_upper * correction,
            )
            slots.append(
                ForecastSlot(
                    timestamp,
                    forecast_load,
                    sum(samples) / len(samples),
                    sum(base_pv_buckets[bucket])
                    / len(base_pv_buckets[bucket]),
                    forecast_load_lower,
                    forecast_load_upper,
                    load_bucket.samples if load_bucket else 0,
                    (
                        weather_buckets.get(bucket, [None])[0]
                        if weather_buckets.get(bucket)
                        else None
                    ),
                )
            )
        if not slots:
            plan = _no_action("forecast has no points in planning horizon", inputs)
            self._publish(plan)
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
        charge_limit = min(
            self.config.max_charge_watts,
            ASW12KH_T3_MAX_BATTERY_CHARGE_W,
            observed_charge,
        )
        discharge_limit = min(
            self.config.max_discharge_watts,
            ASW12KH_T3_MAX_BATTERY_DISCHARGE_W,
            observed_discharge,
        )
        native_baseline = self._native_baseline(required)
        result = simulate_plan(
            self.config,
            self.tariff,
            slots,
            initial_soc_percent=float(required["battery.soc"]["value"]),
            charge_limit_w=charge_limit,
            discharge_limit_w=discharge_limit,
            native_baseline=native_baseline,
        )
        plan = {
            "generated_at": utc_now(),
            "mode": "shadow",
            "status": "ready" if result["feasible"] else "infeasible",
            "inputs": inputs,
            "load_forecast": {
                "method": (
                    "historical weekday/15-minute robust distribution with current-load fallback"
                    if load_profile
                    else "current-load persistence"
                ),
                "historical_buckets": len(load_profile),
                "long_term_statistic": (
                    "median with 10th/90th percentile interval of retained "
                    "15-minute observations"
                ),
                "long_term_window_days": 730,
                "short_term_factor": round(load_correction, 5),
                "short_term_samples": load_correction_samples,
                "short_term_decay_hours": 4,
            },
            "pv_forecast_quality": {
                "control_ready": bool(
                    forecast.get("correction", {}).get("control_ready", False)
                ),
                "learning_state": forecast.get("correction", {}).get(
                    "quality", "unknown"
                ),
                "validation": forecast.get("correction", {}).get(
                    "validation", {}
                ),
                "note": (
                    "shadow planning continues while accuracy learns; this "
                    "quality gate cannot enable hardware writes"
                ),
            },
            **result,
            "control_commands_sent": 0,
            "execution_available": False,
        }
        self._publish(plan)
        self.runs += 1
        self.state.update_health(
            "optimisation",
            status="ok",
            mode="shadow",
            reason=None,
            plans_generated=self.runs,
            persistence_failures=self.persistence_failures,
            control_commands_sent=0,
        )
        return plan

    def _publish(self, plan: dict[str, Any]) -> None:
        self.plans.replace(plan)
        if self.history is not None:
            try:
                self.history.record_plan(plan)
                self._record_predictions(plan)
            except sqlite3.Error:
                self.persistence_failures += 1

    def _record_predictions(self, plan: dict[str, Any]) -> None:
        recommendations = plan.get("recommendations") or []
        issued_at = plan.get("generated_at")
        if not issued_at or not recommendations:
            return
        latest_issue = self.history.latest_prediction_issue()
        if latest_issue and (
            dt.datetime.fromisoformat(issued_at)
            - dt.datetime.fromisoformat(latest_issue)
        ).total_seconds() < PREDICTION_ARCHIVE_INTERVAL_SECONDS:
            return
        inputs = plan.get("inputs") or {}
        baseline = (plan.get("simulation") or {}).get("baseline") or {}
        pv_quality = plan.get("pv_forecast_quality") or {}
        load_forecast = plan.get("load_forecast") or {}
        shared = {
            "read_only_shadow": True,
            "current_battery_soc_percent": (
                (inputs.get("battery.soc") or {}).get("value")
            ),
            "current_grid_power_w": (
                (inputs.get("grid.active_power") or {}).get("value")
            ),
            "native_policy": baseline.get("policy"),
            "load_forecast": {
                key: load_forecast.get(key)
                for key in (
                    "method",
                    "historical_buckets",
                    "short_term_factor",
                    "short_term_samples",
                )
            },
            "pv_forecast_quality": {
                "control_ready": pv_quality.get("control_ready"),
                "learning_state": pv_quality.get("learning_state"),
            },
            "location_included": False,
        }

        def features(item: dict[str, Any]) -> dict[str, Any]:
            target = dt.datetime.fromisoformat(item["timestamp"])
            local = target.astimezone(self.tariff.timezone)
            return {
                "local_weekday": local.weekday(),
                "local_hour": local.hour,
                "local_minute": local.minute,
                "forecast_load_w": item["forecast_load_w"],
                "forecast_load_lower_w": item[
                    "forecast_load_lower_w"
                ],
                "forecast_load_upper_w": item[
                    "forecast_load_upper_w"
                ],
                "forecast_load_samples": item[
                    "forecast_load_samples"
                ],
                "forecast_pv_w": item["forecast_pv_w"],
                "forecast_base_pv_w": item["forecast_base_pv_w"],
                "weather": item.get("weather"),
                "import_price_per_kwh": item[
                    "import_price_per_kwh"
                ],
                "export_price_per_kwh": item[
                    "export_price_per_kwh"
                ],
                "recommended_action": item["action"],
                "constraints": item["constraints"],
            }

        def persist(
            *,
            signal: str,
            scenario: str,
            field: str,
            unit: str,
            model: str,
            model_version: str,
            scoreable: bool,
            lower_field: str | None = None,
            upper_field: str | None = None,
        ) -> None:
            self.history.record_predictions(
                model=model,
                model_version=model_version,
                signal=signal,
                scenario=scenario,
                issued_at=issued_at,
                unit=unit,
                points=[
                    {
                        "timestamp": item["timestamp"],
                        "value": item[field],
                        "lower": (
                            item[lower_field] if lower_field else None
                        ),
                        "upper": (
                            item[upper_field] if upper_field else None
                        ),
                        "features": features(item),
                    }
                    for item in recommendations
                ],
                metadata={
                    **shared,
                    "scoreable_against_observed_actual": scoreable,
                },
            )

        persist(
            signal="site.load_power",
            scenario="expected",
            field="forecast_load_w",
            lower_field="forecast_load_lower_w",
            upper_field="forecast_load_upper_w",
            unit="W",
            model="fasttalk-load",
            model_version="weekday-quarter-hour-v2",
            scoreable=True,
        )
        for scenario, scoreable, soc_field, lower_field, upper_field in (
            (
                "native_no_change",
                True,
                "baseline_expected_soc_percent",
                None,
                None,
            ),
            (
                "shadow_counterfactual",
                False,
                "expected_soc_percent",
                "expected_soc_lower_percent",
                "expected_soc_upper_percent",
            ),
        ):
            persist(
                signal="battery.soc",
                scenario=scenario,
                field=soc_field,
                unit="%",
                model="fasttalk-dispatch-simulator",
                model_version="v2",
                scoreable=scoreable,
                lower_field=lower_field,
                upper_field=upper_field,
            )

    @staticmethod
    def _native_baseline(
        inputs: dict[str, dict[str, Any] | None],
    ) -> NativeBaseline:
        state = inputs.get("asw.control.charge_discharge_state")
        command = inputs.get("asw.control.power_command")
        lower = inputs.get("battery.limit.soc_lower")
        upper = inputs.get("battery.limit.soc_upper")
        fresh = lambda value: bool(
            value
            and value["quality"] == "good"
            and isinstance(value["value"], (int, float))
        )
        if not fresh(state) or not fresh(command):
            return NativeBaseline()
        mode = {1: "hold", 2: "charge", 3: "discharge"}.get(
            int(state["value"]),
            "hold",
        )
        requested = (
            abs(float(command["value"])) if mode in ("charge", "discharge") else 0.0
        )
        minimum = max(
            0.0,
            min(100.0, float(lower["value"]) if fresh(lower) else 0.0),
        )
        maximum = max(
            minimum,
            min(100.0, float(upper["value"]) if fresh(upper) else 100.0),
        )
        return NativeBaseline(
            mode=mode,
            requested_power_w=requested,
            minimum_soc_percent=minimum,
            maximum_soc_percent=maximum,
            source=(
                "ASW Modbus registers 41152/41153 and native battery SOC bounds"
            ),
            assumption=(
                "the currently stored native mode and power command persist "
                "until a native SOC bound; future native schedule changes are unknown"
            ),
        )

    def _load_profile(
        self,
        now: dt.datetime,
    ) -> dict[tuple[int, int, int], LoadBucket]:
        if self.history is None:
            return {}
        since = (now - dt.timedelta(days=730)).isoformat()
        samples = self.history.measurements(
            "site.load_power",
            since=since,
            resolution="quarter_hour",
            limit=100000,
        )
        buckets: dict[tuple[int, int, int], list[float]] = {}
        for sample in samples:
            if (
                not isinstance(sample["value"], (int, float))
                or not 0 <= float(sample["value"]) <= 30000
            ):
                continue
            timestamp = dt.datetime.fromisoformat(sample["observed_at"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
            local = timestamp.astimezone(self.tariff.timezone)
            key = (local.weekday(), local.hour, local.minute // 15)
            buckets.setdefault(key, []).append(max(0.0, float(sample["value"])))
        return {
            key: LoadBucket(
                median_w=statistics.median(values),
                lower_w=_quantile(values, 0.1),
                upper_w=_quantile(values, 0.9),
                samples=len(values),
            )
            for key, values in buckets.items()
            if len(values) >= 4
        }

    def _load_correction(
        self,
        now: dt.datetime,
        profile: dict[tuple[int, int, int], LoadBucket],
        current_load_w: float,
    ) -> tuple[float, int]:
        if self.history is None:
            return 1.0, 0
        samples = self.history.series(
            "site.load_power",
            since=(now - dt.timedelta(minutes=30)).isoformat(),
            until=now.isoformat(),
            bucket_seconds=300,
            limit=12,
        )
        values = [
            float(sample["value"])
            for sample in samples
            if isinstance(sample["value"], (int, float))
            and 0 <= float(sample["value"]) <= 30000
        ]
        local = now.astimezone(self.tariff.timezone)
        bucket = profile.get(
            (local.weekday(), local.hour, local.minute // 15)
        )
        baseline = bucket.median_w if bucket else current_load_w
        if len(values) < 3 or baseline < 100:
            return 1.0, len(values)
        factor = statistics.median(values) / baseline
        return max(0.5, min(1.5, factor)), len(values)
