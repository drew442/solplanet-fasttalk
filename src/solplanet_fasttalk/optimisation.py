"""Constrained battery planning and replay simulation in shadow mode only."""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
import statistics
import threading
from dataclasses import asdict, dataclass
from typing import Any

from .config import NativeScheduleWindow, OptimisationConfig
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
    scope: str = "weekday"


@dataclass(frozen=True)
class Recommendation:
    timestamp: str
    action: str
    asw_mode: str
    window_mode: str
    command_power_w: float
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
    mode: str = "custom_self_consumption"
    requested_power_w: float = 0.0
    minimum_soc_percent: float = 0.0
    maximum_soc_percent: float = 100.0
    source: str = "confirmed plant default"
    assumption: str = (
        "Custom mode has no known future fixed-power windows; outside a "
        "window the ASW autonomously matches site consumption"
    )
    schedule: tuple[NativeScheduleWindow, ...] = ()


@dataclass(frozen=True)
class Dispatch:
    action: str
    window_mode: str
    command_power_w: float
    battery_power_w: float
    grid_power_w: float
    stored_wh: float
    explanation: tuple[str, ...]


@dataclass
class SearchNode:
    stored_wh: float
    objective_cost: float
    intervention_penalty: float
    premium_date: dt.date | None
    premium_exported_kwh: float
    fixed_intervals: int
    parent: "SearchNode | None"
    dispatch: Dispatch | None


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


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def forecast_confidence(
    load_quality: dict[str, Any],
    pv_validation: dict[str, Any],
    *,
    full_days: int,
) -> dict[str, Any]:
    """Score independent evidence for the export-to-next-free-window horizon.

    This is deliberately conservative and inspectable. Repeated forecast
    vintages cannot substitute for independent days, and strong point-error
    metrics cannot substitute for a calibrated load interval.
    """

    required_horizons = ("0_to_2_hours", "2_to_8_hours", "8_to_24_hours")

    def evidence(summary: dict[str, Any], required_samples: int) -> dict[str, float]:
        days = max(0, int(summary.get("days") or 0))
        day_score = math.sqrt(_clamp(days / max(1, full_days)))
        by_horizon = summary.get("by_horizon") or {}
        sample_score = min(
            (
                _clamp(
                    float((by_horizon.get(name) or {}).get("samples") or 0)
                    / max(1, required_samples)
                )
                for name in required_horizons
            ),
            default=0.0,
        )
        return {
            "independent_days": days,
            "day_diversity_score": day_score,
            "horizon_sample_score": sample_score,
            "evidence_score": min(day_score, sample_score),
        }

    load_evidence = evidence(
        load_quality,
        int(load_quality.get("required_samples_per_horizon") or 300),
    )
    load_horizon = (load_quality.get("by_horizon") or {}).get(
        "8_to_24_hours",
        {},
    )
    load_wape = load_horizon.get("weighted_absolute_percentage_error")
    load_coverage = load_horizon.get("prediction_interval_coverage")
    load_error_score = (
        _clamp((1.0 - float(load_wape)) / 0.8)
        if load_wape is not None
        else 0.0
    )
    load_interval_score = (
        _clamp(float(load_coverage) / 0.8)
        if load_coverage is not None
        else 0.0
    )
    load_accuracy_score = min(load_error_score, load_interval_score)
    load_score = load_evidence["evidence_score"] * load_accuracy_score

    pv_required_samples = int(
        pv_validation.get("required_samples_per_scored_horizon") or 300
    )
    pv_evidence = evidence(pv_validation, pv_required_samples)
    pv_horizons = pv_validation.get("by_horizon") or {}
    maximum_mae = float(pv_validation.get("maximum_normalized_mae") or 0.15)
    maximum_bias = float(pv_validation.get("maximum_normalized_bias") or 0.08)
    pv_accuracy_scores: list[float] = []
    for name in required_horizons:
        metrics = pv_horizons.get(name) or {}
        normalized_mae = metrics.get("normalized_mae")
        normalized_bias = metrics.get("normalized_bias")
        if normalized_mae is None:
            pv_accuracy_scores.append(0.0)
            continue
        mae_score = _clamp(1.0 - float(normalized_mae) / maximum_mae)
        bias_score = (
            _clamp(1.0 - abs(float(normalized_bias)) / maximum_bias)
            if normalized_bias is not None
            else mae_score
        )
        pv_accuracy_scores.append(min(mae_score, bias_score))
    pv_accuracy_score = min(pv_accuracy_scores, default=0.0)
    pv_score = pv_evidence["evidence_score"] * pv_accuracy_score
    combined = min(load_score, pv_score)

    def rounded_evidence(value: dict[str, float]) -> dict[str, float]:
        return {
            key: (item if key == "independent_days" else round(item, 5))
            for key, item in value.items()
        }

    return {
        "score": round(combined, 5),
        "status": (
            "mature"
            if combined >= 0.9
            else "developing"
            if combined >= 0.5
            else "learning"
            if combined >= 0.1
            else "high_buffer"
        ),
        "full_confidence_days": full_days,
        "decision_horizon": "export window through next free-import period",
        "load": {
            **rounded_evidence(load_evidence),
            "accuracy_score": round(load_accuracy_score, 5),
            "score": round(load_score, 5),
            "eight_to_24_hour_wape": load_wape,
            "eight_to_24_hour_interval_coverage": load_coverage,
        },
        "pv": {
            **rounded_evidence(pv_evidence),
            "accuracy_score": round(pv_accuracy_score, 5),
            "score": round(pv_score, 5),
        },
        "method": (
            "minimum of independently scored load and PV evidence; day diversity, "
            "lead-time coverage, error and interval calibration all constrain confidence"
        ),
    }


def _planning_bucket_ids(
    now: dt.datetime,
    cutoff: dt.datetime,
    step_seconds: int,
) -> range:
    """Return every planning interval, including zero-PV overnight slots."""

    return range(
        math.ceil(now.timestamp() / step_seconds),
        math.floor(cutoff.timestamp() / step_seconds) + 1,
    )


def _native_schedule_window(
    timestamp: dt.datetime,
    schedule: tuple[NativeScheduleWindow, ...],
    timezone: dt.tzinfo,
) -> NativeScheduleWindow | None:
    """Return the recurring daily native window active at a timestamp."""

    local = timestamp.astimezone(timezone)
    minute = local.hour * 60 + local.minute
    for window in schedule:
        start_hour, start_minute = (int(part) for part in window.starts_at.split(":"))
        end_hour, end_minute = (int(part) for part in window.ends_at.split(":"))
        starts = start_hour * 60 + start_minute
        ends = end_hour * 60 + end_minute
        active = (
            starts <= minute < ends
            if starts < ends
            else minute >= starts or minute < ends
        )
        if active:
            return window
    return None


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


def _self_consumption_dispatch(
    *,
    natural_grid_w: float,
    stored_wh: float,
    minimum_wh: float,
    maximum_wh: float,
    charge_limit_w: float,
    discharge_limit_w: float,
    step_hours: float,
    charge_efficiency: float,
    discharge_efficiency: float,
) -> Dispatch:
    """Model Custom mode outside a fixed-power schedule."""

    battery_power_w = 0.0
    explanation = "custom mode self-consumption is active with no fixed-power window"
    if natural_grid_w > 0:
        available_w = max(
            0.0,
            (stored_wh - minimum_wh)
            * discharge_efficiency
            / max(step_hours, 0.001),
        )
        battery_power_w = min(
            natural_grid_w,
            discharge_limit_w,
            available_w,
        )
        stored_wh -= (
            battery_power_w
            * step_hours
            / discharge_efficiency
        )
        explanation = (
            "custom mode automatically discharges only enough to follow site consumption"
        )
    elif natural_grid_w < 0:
        room_w = max(
            0.0,
            (maximum_wh - stored_wh)
            / max(step_hours * charge_efficiency, 0.001),
        )
        charge_w = min(-natural_grid_w, charge_limit_w, room_w)
        battery_power_w = -charge_w
        stored_wh += charge_w * step_hours * charge_efficiency
        explanation = (
            "custom mode automatically absorbs available site PV surplus"
        )
    stored_wh = min(maximum_wh, max(minimum_wh, stored_wh))
    return Dispatch(
        "self_consumption",
        "none",
        0.0,
        battery_power_w,
        natural_grid_w - battery_power_w,
        stored_wh,
        (explanation,),
    )


def _fixed_window_dispatch(
    *,
    direction: str,
    command_power_w: float,
    natural_grid_w: float,
    stored_wh: float,
    minimum_wh: float,
    maximum_wh: float,
    charge_limit_w: float,
    discharge_limit_w: float,
    step_hours: float,
    charge_efficiency: float,
    discharge_efficiency: float,
) -> Dispatch:
    """Model the ASW fixed battery-power semantics inside a Custom window."""

    if direction == "charge":
        room_w = max(
            0.0,
            (maximum_wh - stored_wh)
            / max(step_hours * charge_efficiency, 0.001),
        )
        power_w = min(abs(command_power_w), charge_limit_w, room_w)
        stored_wh += power_w * step_hours * charge_efficiency
        battery_power_w = -power_w
        action = "grid_charge"
        explanation = (
            "fixed Custom charge window commands battery input power; site load is not folded into the command",
        )
    elif direction == "discharge":
        available_w = max(
            0.0,
            (stored_wh - minimum_wh)
            * discharge_efficiency
            / max(step_hours, 0.001),
        )
        power_w = min(abs(command_power_w), discharge_limit_w, available_w)
        stored_wh -= power_w * step_hours / discharge_efficiency
        battery_power_w = power_w
        action = "export_discharge"
        explanation = (
            "fixed Custom discharge window commands battery output power; site load remains an independent grid-balance term",
        )
    else:
        raise ValueError("fixed window direction must be charge or discharge")
    stored_wh = min(maximum_wh, max(minimum_wh, stored_wh))
    return Dispatch(
        action,
        direction,
        power_w,
        battery_power_w,
        natural_grid_w - battery_power_w,
        stored_wh,
        explanation,
    )


def _slot_financial_cost(
    tariff: ZeroHeroTariff,
    timestamp: dt.datetime,
    grid_power_w: float,
    step_hours: float,
    premium_date: dt.date | None,
    premium_exported_kwh: float,
) -> tuple[float, dt.date, float]:
    """Return exact interval cost with the daily Super Export cap."""

    quote = tariff.quote(timestamp)
    local_date = timestamp.astimezone(tariff.timezone).date()
    used = premium_exported_kwh if premium_date == local_date else 0.0
    imported = max(0.0, grid_power_w) * step_hours / 1000.0
    exported = max(0.0, -grid_power_w) * step_hours / 1000.0
    cost = imported * quote.import_price_per_kwh
    if quote.export_period == "super_export":
        premium = min(exported, max(0.0, 15.0 - used))
        cost -= premium * quote.export_price_per_kwh
        cost -= (exported - premium) * 0.05
        used = min(15.0, used + exported)
    else:
        cost -= exported * quote.export_price_per_kwh
    return cost, local_date, used


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
    observed_charge_limit_w: float | None = None,
    observed_discharge_limit_w: float | None = None,
    effective_reserve_soc_percent: float | None = None,
) -> dict[str, Any]:
    """Plan fixed windows around the ASW's native self-consumption behavior."""

    baseline_policy = native_baseline or NativeBaseline()
    charge_limit_w = max(
        0.0,
        min(charge_limit_w, ASW12KH_T3_MAX_BATTERY_CHARGE_W),
    )
    discharge_limit_w = max(
        0.0,
        min(discharge_limit_w, ASW12KH_T3_MAX_BATTERY_DISCHARGE_W),
    )
    ordered_slots = sorted(slots, key=lambda item: item.timestamp)
    step_hours = config.step_minutes / 60.0
    capacity_wh = config.battery_capacity_kwh * 1000.0
    planning_reserve_soc_percent = _clamp(
        (
            config.reserve_soc_percent
            if effective_reserve_soc_percent is None
            else effective_reserve_soc_percent
        ),
        config.reserve_soc_percent,
        config.maximum_soc_percent,
    )
    minimum_wh = capacity_wh * config.reserve_soc_percent / 100.0
    export_minimum_wh = capacity_wh * planning_reserve_soc_percent / 100.0
    maximum_wh = capacity_wh * config.maximum_soc_percent / 100.0
    initial_wh = min(
        maximum_wh,
        max(minimum_wh, capacity_wh * initial_soc_percent / 100.0),
    )
    baseline_minimum_wh = capacity_wh * max(
        0.0,
        min(100.0, baseline_policy.minimum_soc_percent),
    ) / 100.0
    baseline_maximum_wh = capacity_wh * max(
        baseline_policy.minimum_soc_percent,
        min(100.0, baseline_policy.maximum_soc_percent),
    ) / 100.0
    baseline_initial_wh = min(
        baseline_maximum_wh,
        max(
            baseline_minimum_wh,
            capacity_wh * initial_soc_percent / 100.0,
        ),
    )

    def baseline_dispatch(slot: ForecastSlot, stored_wh: float) -> Dispatch:
        natural_grid_w = slot.load_w - slot.pv_w
        if baseline_policy.mode in (
            "custom_self_consumption",
            "self_consumption",
        ):
            native_window = _native_schedule_window(
                slot.timestamp,
                baseline_policy.schedule,
                tariff.timezone,
            )
            if baseline_policy.mode == "custom_self_consumption" and native_window:
                return _fixed_window_dispatch(
                    direction=native_window.mode,
                    command_power_w=native_window.power_watts,
                    natural_grid_w=natural_grid_w,
                    stored_wh=stored_wh,
                    minimum_wh=baseline_minimum_wh,
                    maximum_wh=baseline_maximum_wh,
                    charge_limit_w=charge_limit_w,
                    discharge_limit_w=discharge_limit_w,
                    step_hours=step_hours,
                    charge_efficiency=config.charge_efficiency,
                    discharge_efficiency=config.discharge_efficiency,
                )
            return _self_consumption_dispatch(
                natural_grid_w=natural_grid_w,
                stored_wh=stored_wh,
                minimum_wh=baseline_minimum_wh,
                maximum_wh=baseline_maximum_wh,
                charge_limit_w=charge_limit_w,
                discharge_limit_w=discharge_limit_w,
                step_hours=step_hours,
                charge_efficiency=config.charge_efficiency,
                discharge_efficiency=config.discharge_efficiency,
            )
        if baseline_policy.mode == "reserve":
            if natural_grid_w < 0:
                return _self_consumption_dispatch(
                    natural_grid_w=natural_grid_w,
                    stored_wh=stored_wh,
                    minimum_wh=baseline_minimum_wh,
                    maximum_wh=baseline_maximum_wh,
                    charge_limit_w=charge_limit_w,
                    discharge_limit_w=0.0,
                    step_hours=step_hours,
                    charge_efficiency=config.charge_efficiency,
                    discharge_efficiency=config.discharge_efficiency,
                )
            return Dispatch(
                "reserve",
                "none",
                0.0,
                0.0,
                natural_grid_w,
                stored_wh,
                ("native reserve mode retains battery energy while the grid is available",),
            )
        if baseline_policy.mode in ("charge_window", "discharge_window"):
            return _fixed_window_dispatch(
                direction=(
                    "charge"
                    if baseline_policy.mode == "charge_window"
                    else "discharge"
                ),
                command_power_w=baseline_policy.requested_power_w,
                natural_grid_w=natural_grid_w,
                stored_wh=stored_wh,
                minimum_wh=baseline_minimum_wh,
                maximum_wh=baseline_maximum_wh,
                charge_limit_w=charge_limit_w,
                discharge_limit_w=discharge_limit_w,
                step_hours=step_hours,
                charge_efficiency=config.charge_efficiency,
                discharge_efficiency=config.discharge_efficiency,
            )
        return Dispatch(
            "hold",
            "none",
            0.0,
            0.0,
            natural_grid_w,
            stored_wh,
            ("native mode is unknown, so the baseline does not assume battery movement",),
        )

    baseline_stored_wh = baseline_initial_wh
    baseline_dispatches: list[Dispatch] = []
    baseline_flows: list[tuple[dt.datetime, float, float]] = []
    for slot in ordered_slots:
        dispatched = baseline_dispatch(slot, baseline_stored_wh)
        baseline_stored_wh = dispatched.stored_wh
        baseline_dispatches.append(dispatched)
        baseline_flows.append(
            (
                slot.timestamp,
                max(0.0, dispatched.grid_power_w) * step_hours / 1000.0,
                max(0.0, -dispatched.grid_power_w) * step_hours / 1000.0,
            )
        )

    root = SearchNode(initial_wh, 0.0, 0.0, None, 0.0, 0, None, None)
    nodes = [root]
    energy_quantum_wh = max(100.0, capacity_wh * 0.005)
    premium_quantum_kwh = 0.25
    for slot in ordered_slots:
        natural_grid_w = slot.load_w - slot.pv_w
        candidates: dict[tuple[int, int], SearchNode] = {}
        for node in nodes:
            automatic = _self_consumption_dispatch(
                natural_grid_w=natural_grid_w,
                stored_wh=node.stored_wh,
                minimum_wh=minimum_wh,
                maximum_wh=maximum_wh,
                charge_limit_w=charge_limit_w,
                discharge_limit_w=discharge_limit_w,
                step_hours=step_hours,
                charge_efficiency=config.charge_efficiency,
                discharge_efficiency=config.discharge_efficiency,
            )
            dispatches = [automatic]
            charged = _fixed_window_dispatch(
                direction="charge",
                command_power_w=charge_limit_w,
                natural_grid_w=natural_grid_w,
                stored_wh=node.stored_wh,
                minimum_wh=minimum_wh,
                maximum_wh=maximum_wh,
                charge_limit_w=charge_limit_w,
                discharge_limit_w=discharge_limit_w,
                step_hours=step_hours,
                charge_efficiency=config.charge_efficiency,
                discharge_efficiency=config.discharge_efficiency,
            )
            if (
                charged.command_power_w > 0
                and charged.grid_power_w > 0
                and charged.grid_power_w <= config.site_import_limit_watts
            ):
                dispatches.append(charged)
            discharged = _fixed_window_dispatch(
                direction="discharge",
                command_power_w=discharge_limit_w,
                natural_grid_w=natural_grid_w,
                stored_wh=node.stored_wh,
                minimum_wh=export_minimum_wh,
                maximum_wh=maximum_wh,
                charge_limit_w=charge_limit_w,
                discharge_limit_w=discharge_limit_w,
                step_hours=step_hours,
                charge_efficiency=config.charge_efficiency,
                discharge_efficiency=config.discharge_efficiency,
            )
            if (
                discharged.command_power_w > 0
                and discharged.grid_power_w < 0
                and discharged.grid_power_w >= -config.site_export_limit_watts
                and tariff.quote(slot.timestamp).export_price_per_kwh > 0
            ):
                dispatches.append(discharged)

            for dispatched in dispatches:
                financial, premium_date, premium_used = _slot_financial_cost(
                    tariff,
                    slot.timestamp,
                    dispatched.grid_power_w,
                    step_hours,
                    node.premium_date,
                    node.premium_exported_kwh,
                )
                fixed = dispatched.window_mode != "none"
                intervention = (
                    dispatched.command_power_w
                    * step_hours
                    / 1000.0
                    * config.minimum_arbitrage_margin_per_kwh
                    / 2.0
                    if fixed
                    else 0.0
                )
                candidate = SearchNode(
                    dispatched.stored_wh,
                    node.objective_cost + financial + intervention,
                    node.intervention_penalty + intervention,
                    premium_date,
                    premium_used,
                    node.fixed_intervals + int(fixed),
                    node,
                    dispatched,
                )
                key = (
                    round((dispatched.stored_wh - minimum_wh) / energy_quantum_wh),
                    round(premium_used / premium_quantum_kwh),
                )
                current = candidates.get(key)
                if current is None or (
                    candidate.objective_cost,
                    candidate.fixed_intervals,
                ) < (
                    current.objective_cost,
                    current.fixed_intervals,
                ):
                    candidates[key] = candidate
        nodes = list(candidates.values())

    terminal_floor = min(baseline_stored_wh, maximum_wh) - energy_quantum_wh
    terminal_nodes = [node for node in nodes if node.stored_wh >= terminal_floor]
    if not terminal_nodes:
        terminal_nodes = nodes

    def reconstruct(node: SearchNode) -> list[Dispatch]:
        result: list[Dispatch] = []
        while node.dispatch is not None:
            result.append(node.dispatch)
            if node.parent is None:
                break
            node = node.parent
        return list(reversed(result))

    def exact_objective(node: SearchNode) -> tuple[float, int, float]:
        dispatches = reconstruct(node)
        flows = [
            (
                slot.timestamp,
                max(0.0, dispatched.grid_power_w) * step_hours / 1000.0,
                max(0.0, -dispatched.grid_power_w) * step_hours / 1000.0,
            )
            for slot, dispatched in zip(ordered_slots, dispatches)
        ]
        return (
            _bill(tariff, flows, config.step_minutes)
            + node.intervention_penalty,
            node.fixed_intervals,
            -node.stored_wh,
        )

    winner = min(terminal_nodes, key=exact_objective)
    optimized_dispatches = reconstruct(winner)
    optimized_flows = [
        (
            slot.timestamp,
            max(0.0, dispatched.grid_power_w) * step_hours / 1000.0,
            max(0.0, -dispatched.grid_power_w) * step_hours / 1000.0,
        )
        for slot, dispatched in zip(ordered_slots, optimized_dispatches)
    ]
    baseline_cost = _bill(tariff, baseline_flows, config.step_minutes)
    optimized_cost = _bill(tariff, optimized_flows, config.step_minutes)
    terminal_import_price = max(
        (tariff.quote(slot.timestamp).import_price_per_kwh for slot in ordered_slots),
        default=0.0,
    )

    def terminal_adjustment(dispatches: list[Dispatch]) -> float:
        ending_wh = dispatches[-1].stored_wh if dispatches else initial_wh
        shortfall_wh = max(0.0, baseline_stored_wh - ending_wh)
        return (
            shortfall_wh
            / 1000.0
            * config.discharge_efficiency
            * terminal_import_price
        )

    energy_adjustment = terminal_adjustment(optimized_dispatches)
    estimated_improvement = baseline_cost - optimized_cost - energy_adjustment
    if estimated_improvement <= 0.000001:
        optimized_dispatches = [
            Dispatch(
                "preserve_native",
                "none",
                0.0,
                dispatched.battery_power_w,
                dispatched.grid_power_w,
                dispatched.stored_wh,
                (
                    "no daemon intervention improves on the owner-confirmed native ASW policy",
                ),
            )
            for dispatched in baseline_dispatches
        ]
        optimized_flows = list(baseline_flows)
        optimized_cost = baseline_cost
        energy_adjustment = 0.0
        estimated_improvement = 0.0

    cumulative_load_uncertainty_wh = 0.0
    recommendations: list[Recommendation] = []
    for slot, baseline, optimized in zip(
        ordered_slots,
        baseline_dispatches,
        optimized_dispatches,
    ):
        load_lower = slot.load_w if slot.load_lower_w is None else slot.load_lower_w
        load_upper = slot.load_w if slot.load_upper_w is None else slot.load_upper_w
        cumulative_load_uncertainty_wh += max(
            abs(slot.load_w - load_lower),
            abs(load_upper - slot.load_w),
        ) * step_hours / min(config.charge_efficiency, config.discharge_efficiency)
        expected_soc = optimized.stored_wh / capacity_wh * 100.0
        soc_uncertainty = cumulative_load_uncertainty_wh / capacity_wh * 100.0
        trajectory_minimum_soc = (
            baseline_policy.minimum_soc_percent
            if optimized.action == "preserve_native"
            else planning_reserve_soc_percent
            if optimized.action == "export_discharge"
            else config.reserve_soc_percent
        )
        trajectory_maximum_soc = (
            baseline_policy.maximum_soc_percent
            if optimized.action == "preserve_native"
            else config.maximum_soc_percent
        )
        quote = tariff.quote(slot.timestamp)
        explanations = optimized.explanation + (
            f"forecast load {slot.load_w:.0f} W and PV {slot.pv_w:.0f} W",
            (
                f"tariff import ${quote.import_price_per_kwh:.3f}/kWh "
                f"and export ${quote.export_price_per_kwh:.3f}/kWh"
            ),
            (
                f"trajectory SOC kept within {trajectory_minimum_soc:.1f}%–"
                f"{trajectory_maximum_soc:.1f}%"
            ),
        )
        recommendations.append(
            Recommendation(
                slot.timestamp.astimezone(dt.timezone.utc).isoformat(),
                optimized.action,
                "custom",
                optimized.window_mode,
                round(optimized.command_power_w, 3),
                round(slot.load_w, 3),
                round(slot.pv_w, 3),
                round(slot.pv_w if slot.base_pv_w is None else slot.base_pv_w, 3),
                round(load_lower, 3),
                round(load_upper, 3),
                slot.load_samples,
                slot.weather,
                round(baseline.grid_power_w, 3),
                round(baseline.battery_power_w, 3),
                round(baseline.stored_wh / capacity_wh * 100.0, 3),
                round(optimized.battery_power_w, 3),
                round(optimized.grid_power_w, 3),
                round(expected_soc, 3),
                round(max(trajectory_minimum_soc, expected_soc - soc_uncertainty), 3),
                round(min(trajectory_maximum_soc, expected_soc + soc_uncertainty), 3),
                quote.import_price_per_kwh,
                quote.export_price_per_kwh,
                explanations,
                {
                    "charge_limit_w": charge_limit_w,
                    "discharge_limit_w": discharge_limit_w,
                    "observed_current_charge_limit_w": (
                        charge_limit_w
                        if observed_charge_limit_w is None
                        else observed_charge_limit_w
                    ),
                    "observed_current_discharge_limit_w": (
                        discharge_limit_w
                        if observed_discharge_limit_w is None
                        else observed_discharge_limit_w
                    ),
                    "site_import_limit_w": config.site_import_limit_watts,
                    "site_export_limit_w": config.site_export_limit_watts,
                    "reserve_soc_percent": config.reserve_soc_percent,
                    "effective_reserve_soc_percent": planning_reserve_soc_percent,
                    "maximum_soc_percent": config.maximum_soc_percent,
                    "trajectory_minimum_soc_percent": trajectory_minimum_soc,
                    "trajectory_maximum_soc_percent": trajectory_maximum_soc,
                    "manufacturer_max_charge_w": ASW12KH_T3_MAX_BATTERY_CHARGE_W,
                    "manufacturer_max_discharge_w": ASW12KH_T3_MAX_BATTERY_DISCHARGE_W,
                },
                (
                    optimized.grid_power_w <= config.site_import_limit_watts + 0.001
                    and optimized.grid_power_w >= -config.site_export_limit_watts - 0.001
                ),
            )
        )

    baseline_import = sum(item[1] for item in baseline_flows)
    baseline_export = sum(item[2] for item in baseline_flows)
    optimized_import = sum(item[1] for item in optimized_flows)
    optimized_export = sum(item[2] for item in optimized_flows)
    windows = []

    def interval_end(timestamp: str) -> str:
        return (
            dt.datetime.fromisoformat(timestamp)
            + dt.timedelta(minutes=config.step_minutes)
        ).isoformat()

    for index, recommendation in enumerate(recommendations):
        if recommendation.window_mode == "none":
            continue
        if (
            windows
            and windows[-1]["last_index"] == index - 1
            and windows[-1]["mode"] == recommendation.window_mode
            and windows[-1]["command_power_w"] == recommendation.command_power_w
        ):
            windows[-1]["intervals"] += 1
            windows[-1]["ends_at"] = interval_end(recommendation.timestamp)
            windows[-1]["last_index"] = index
        else:
            windows.append(
                {
                    "mode": recommendation.window_mode,
                    "command_power_w": recommendation.command_power_w,
                    "starts_at": recommendation.timestamp,
                    "ends_at": interval_end(recommendation.timestamp),
                    "intervals": 1,
                    "last_index": index,
                }
            )
    for window in windows:
        window.pop("last_index")
    feasible = all(item.feasible for item in recommendations)
    return {
        "recommendations": [asdict(item) for item in recommendations],
        "scheduled_windows": windows,
        "feasible": feasible,
        "simulation": {
            "baseline": {
                "cost": round(baseline_cost, 6),
                "import_kwh": round(baseline_import, 6),
                "export_kwh": round(baseline_export, 6),
                "ending_soc_percent": round(baseline_stored_wh / capacity_wh * 100.0, 3),
                "policy": asdict(baseline_policy),
            },
            "optimized": {
                "cost": round(optimized_cost, 6),
                "import_kwh": round(optimized_import, 6),
                "export_kwh": round(optimized_export, 6),
                "ending_soc_percent": round(
                    optimized_dispatches[-1].stored_wh / capacity_wh * 100.0
                    if optimized_dispatches
                    else initial_soc_percent,
                    3,
                ),
            },
            "terminal_energy_adjustment": round(energy_adjustment, 6),
            "estimated_cost_improvement": round(estimated_improvement, 6),
            "model": (
                "complete-horizon Custom-mode simulation: native self-consumption outside windows; "
                "fixed battery power only for grid charge or export discharge; includes efficiency, "
                "SOC/site limits, ZEROHERO eligibility and the Super Export cap"
            ),
        },
        "control_strategy": {
            "preferred_mode": "custom",
            "default_window_mode": "none",
            "default_behavior": "inverter-managed self-consumption",
            "fixed_windows_only_for": ["grid_charge", "export_discharge"],
            "site_load_added_to_fixed_discharge_command": False,
            "configured_soc_bounds_percent": {
                "minimum": config.reserve_soc_percent,
                "maximum": config.maximum_soc_percent,
            },
            "effective_export_reserve_soc_percent": round(
                planning_reserve_soc_percent,
                3,
            ),
            "reserve_policy": (
                "confidence-scaled for deliberate export discharge only; "
                "retained energy remains available to native self-consumption"
            ),
            "soc_bound_write_required_if_native_differs": True,
        },
        "hardware_limits": {
            "model": "ASW12kH-T3",
            "manufacturer_max_charge_w": ASW12KH_T3_MAX_BATTERY_CHARGE_W,
            "manufacturer_max_discharge_w": ASW12KH_T3_MAX_BATTERY_DISCHARGE_W,
            "planning_interval_seconds": config.step_minutes * 60,
            "basis": (
                "future slots use the 12 kW inverter battery rating capped by configured limits and projected SOC headroom; current live BMS voltage×current remains diagnostic evidence and must be rechecked before execution"
            ),
            "observed_current_charge_limit_w": observed_charge_limit_w,
            "observed_current_discharge_limit_w": observed_discharge_limit_w,
            "excluded_limit": (
                "24 kVA EPS overload rating is limited to 10 seconds and is not used for dispatch"
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
            "asw.control.run_mode",
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
        day_bounds: dict[dt.date, tuple[int, int]] = {}
        for bucket in pv_buckets:
            local_date = dt.datetime.fromtimestamp(
                bucket * step_seconds,
                tz=dt.timezone.utc,
            ).astimezone(self.tariff.timezone).date()
            lower, upper = day_bounds.get(local_date, (bucket, bucket))
            day_bounds[local_date] = (min(lower, bucket), max(upper, bucket))
        missing_daylight_buckets = 0
        zero_filled_buckets = 0
        for bucket in _planning_bucket_ids(now, cutoff, step_seconds):
            timestamp = dt.datetime.fromtimestamp(
                bucket * step_seconds, tz=dt.timezone.utc
            )
            samples = pv_buckets.get(bucket)
            if not samples:
                samples = [0.0]
                zero_filled_buckets += 1
                local_date = timestamp.astimezone(self.tariff.timezone).date()
                bounds = day_bounds.get(local_date)
                if bounds and bounds[0] <= bucket <= bounds[1]:
                    missing_daylight_buckets += 1
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
                    (
                        sum(base_pv_buckets[bucket])
                        / len(base_pv_buckets[bucket])
                        if bucket in base_pv_buckets
                        else 0.0
                    ),
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
        )
        discharge_limit = min(
            self.config.max_discharge_watts,
            ASW12KH_T3_MAX_BATTERY_DISCHARGE_W,
        )
        native_baseline = self._native_baseline(required)
        active_native_window = _native_schedule_window(
            now,
            native_baseline.schedule,
            self.tariff.timezone,
        )
        state_input = required.get("asw.control.charge_discharge_state")
        command_input = required.get("asw.control.power_command")
        state_fresh = bool(
            state_input
            and state_input["quality"] == "good"
            and isinstance(state_input["value"], (int, float))
        )
        command_fresh = bool(
            command_input
            and command_input["quality"] == "good"
            and isinstance(command_input["value"], (int, float))
        )
        expected_state = (
            2 if active_native_window and active_native_window.mode == "charge"
            else 3 if active_native_window else None
        )
        expected_command_w = (
            (-1 if active_native_window.mode == "charge" else 1)
            * active_native_window.power_watts
            if active_native_window
            else None
        )
        current_window_matches = (
            bool(
                state_fresh
                and command_fresh
                and int(state_input["value"]) == expected_state
                and abs(float(command_input["value"]) - expected_command_w) <= 50
            )
            if active_native_window
            else None
        )
        load_quality = (
            self.history.prediction_quality(
                signal="site.load_power",
                scenario="expected",
                model="fasttalk-load",
                model_version="hierarchical-quarter-hour-v3",
                since=(now - dt.timedelta(days=90)).isoformat(),
            )
            if self.history is not None
            else {"samples": 0, "days": 0, "by_horizon": {}}
        )
        pv_validation = forecast.get("correction", {}).get("validation", {})
        confidence = forecast_confidence(
            load_quality,
            pv_validation,
            full_days=self.config.forecast_confidence_full_days,
        )
        untrusted_reserve = min(
            self.config.maximum_soc_percent,
            self.config.untrusted_reserve_soc_percent,
        )
        effective_reserve = self.config.reserve_soc_percent + (
            untrusted_reserve - self.config.reserve_soc_percent
        ) * (1.0 - float(confidence["score"]))
        confidence["configured_reserve_soc_percent"] = (
            self.config.reserve_soc_percent
        )
        confidence["untrusted_reserve_soc_percent"] = untrusted_reserve
        confidence["effective_reserve_soc_percent"] = round(
            effective_reserve,
            3,
        )
        confidence["objective"] = (
            "protect consumption until the next free-import period; release "
            "more Super Export energy as independently scored confidence improves"
        )
        confidence["economic_stage"] = (
            "cost_neutral_first"
            if confidence["score"] < 0.1
            else "confidence_scaled_profit"
        )
        confidence["profit_release_fraction"] = confidence["score"]
        local_now = now.astimezone(self.tariff.timezone)
        premium_quote = self.tariff.quote(
            local_now.replace(hour=18, minute=0, second=0, microsecond=0)
        )
        fixed_cost_after_credit = max(
            0.0,
            premium_quote.daily_supply_charge
            - float(premium_quote.zerohero_daily_credit or 0.0),
        )
        cost_neutral_export_kwh = (
            fixed_cost_after_credit / premium_quote.export_price_per_kwh
            if premium_quote.export_price_per_kwh > 0
            else None
        )
        confidence["cost_neutral_target"] = {
            "daily_supply_charge": premium_quote.daily_supply_charge,
            "zerohero_credit_if_earned": premium_quote.zerohero_daily_credit,
            "remaining_fixed_cost_after_credit": round(
                fixed_cost_after_credit,
                5,
            ),
            "premium_export_kwh_if_no_import_cost": (
                round(cost_neutral_export_kwh, 5)
                if cost_neutral_export_kwh is not None
                else None
            ),
            "equivalent_battery_soc_percent": (
                round(
                    cost_neutral_export_kwh
                    / self.config.discharge_efficiency
                    / self.config.battery_capacity_kwh
                    * 100.0,
                    3,
                )
                if cost_neutral_export_kwh is not None
                else None
            ),
            "note": (
                "minimum premium export needed to offset the daily supply charge "
                "after earning ZEROHERO, before any import cost; the optimizer may "
                "release only what the confidence-scaled reserve permits"
            ),
        }
        result = simulate_plan(
            self.config,
            self.tariff,
            slots,
            initial_soc_percent=float(required["battery.soc"]["value"]),
            charge_limit_w=charge_limit,
            discharge_limit_w=discharge_limit,
            native_baseline=native_baseline,
            observed_charge_limit_w=observed_charge,
            observed_discharge_limit_w=observed_discharge,
            effective_reserve_soc_percent=effective_reserve,
        )
        load_profile_ready = bool(slots) and all(
            slot.load_samples >= 4 for slot in slots
        )
        load_accuracy_ready = bool(load_quality.get("dataset_ready", False))
        pv_control_ready = bool(
            forecast.get("correction", {}).get("control_ready", False)
        )
        planning_quality_ready = bool(
            pv_control_ready
            and load_profile_ready
            and load_accuracy_ready
            and missing_daylight_buckets == 0
            and (
                native_baseline.mode != "custom_self_consumption"
                or self.config.native_schedule_confirmed
            )
            and current_window_matches is not False
        )
        improvement = result["simulation"]["estimated_cost_improvement"]
        status = (
            "infeasible"
            if not result["feasible"]
            else "ready"
            if planning_quality_ready and improvement > 0
            else "no_change"
            if planning_quality_ready
            else "learning"
        )
        quality_reasons = []
        if not pv_control_ready:
            quality_reasons.append("PV accuracy gate has not passed")
        if not load_profile_ready:
            quality_reasons.append(
                "load profile lacks four samples for one or more intervals"
            )
        if not load_accuracy_ready:
            quality_reasons.append(
                "load prediction accuracy lacks 28 days and 300 scored samples in each required horizon"
            )
        if missing_daylight_buckets:
            quality_reasons.append(
                f"PV provider has {missing_daylight_buckets} missing daylight intervals"
            )
        if (
            native_baseline.mode == "custom_self_consumption"
            and not self.config.native_schedule_confirmed
        ):
            quality_reasons.append(
                "native Custom schedule has not been explicitly confirmed"
            )
        if current_window_matches is False:
            quality_reasons.append(
                "active native schedule window does not match ASW direction/power readback"
            )
        if improvement <= 0:
            quality_reasons.append(
                "no fixed-power window improves on Custom self-consumption"
            )
        plan = {
            "generated_at": utc_now(),
            "mode": "shadow",
            "status": status,
            "reason": "; ".join(quality_reasons) if quality_reasons else None,
            "inputs": inputs,
            "load_forecast": {
                "method": (
                    "hierarchical weekday/day-type/all-days 15-minute robust distribution with current-load fallback"
                    if load_profile
                    else "current-load persistence"
                ),
                "historical_buckets": len(load_profile),
                "profile_ready": load_profile_ready,
                "accuracy_ready": load_accuracy_ready,
                "accuracy_samples": load_quality.get("samples", 0),
                "accuracy_days": load_quality.get("days", 0),
                "long_term_statistic": (
                    "median with 10th/90th percentile interval of retained "
                    "15-minute observations"
                ),
                "long_term_window_days": 730,
                "short_term_factor": round(load_correction, 5),
                "short_term_samples": load_correction_samples,
                "short_term_decay_hours": 4,
            },
            "forecast_confidence": confidence,
            "pv_forecast_quality": {
                "control_ready": pv_control_ready,
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
            "timeline_quality": {
                "complete": missing_daylight_buckets == 0,
                "step_minutes": self.config.step_minutes,
                "intervals": len(slots),
                "night_intervals_filled_with_zero_pv": zero_filled_buckets,
                "missing_daylight_intervals": missing_daylight_buckets,
            },
            "planning_quality": {
                "control_ready": planning_quality_ready,
                "reasons": quality_reasons,
            },
            "native_schedule_quality": {
                "source": "owner-supplied recurring local-time configuration",
                "confirmed": self.config.native_schedule_confirmed,
                "configured_windows": len(native_baseline.schedule),
                "active_window": (
                    asdict(active_native_window) if active_native_window else None
                ),
                "expected_state": expected_state,
                "expected_power_command_w": expected_command_w,
                "active_window_readback_matches": current_window_matches,
                "note": (
                    "the documented ASW read map does not expose future schedule times; "
                    "41152/41153 validate an active configured window only"
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
            status="ok" if status in ("ready", "no_change") else "degraded",
            mode="shadow",
            reason=plan.get("reason"),
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
            model_version="hierarchical-quarter-hour-v3",
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
                model_version="asw-custom-mode-v3",
                scoreable=scoreable,
                lower_field=lower_field,
                upper_field=upper_field,
            )

    def _native_baseline(
        self,
        inputs: dict[str, dict[str, Any] | None],
    ) -> NativeBaseline:
        run_mode = inputs.get("asw.control.run_mode")
        lower = inputs.get("battery.limit.soc_lower")
        upper = inputs.get("battery.limit.soc_upper")
        fresh = lambda value: bool(
            value
            and value["quality"] == "good"
            and isinstance(value["value"], (int, float))
        )
        mode_value = int(run_mode["value"]) if fresh(run_mode) else 4
        if self.config.native_schedule and mode_value in (2, 4):
            mode = "custom_self_consumption"
        else:
            mode = {
                2: "self_consumption",
                3: "reserve",
                4: "custom_self_consumption",
            }.get(mode_value, "unknown")
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
            requested_power_w=0.0,
            minimum_soc_percent=minimum,
            maximum_soc_percent=maximum,
            source=(
                "owner-confirmed recurring schedule, ASW effective run-mode "
                "register 41104 and native battery SOC bounds"
            ),
            assumption=(
                "future native schedule windows are not exposed by the documented "
                "read map; owner-confirmed recurring windows define future policy "
                "even when 41104 reports effective self-consumption outside them"
            ),
            schedule=self.config.native_schedule,
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
        day_type_buckets: dict[tuple[bool, int, int], list[float]] = {}
        clock_buckets: dict[tuple[int, int], list[float]] = {}
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
            value = max(0.0, float(sample["value"]))
            buckets.setdefault(key, []).append(value)
            day_type_buckets.setdefault(
                (local.weekday() < 5, local.hour, local.minute // 15),
                [],
            ).append(value)
            clock_buckets.setdefault((local.hour, local.minute // 15), []).append(value)

        def load_bucket(values: list[float], scope: str) -> LoadBucket:
            return LoadBucket(
                median_w=statistics.median(values),
                lower_w=_quantile(values, 0.1),
                upper_w=_quantile(values, 0.9),
                samples=len(values),
                scope=scope,
            )

        result = {
            key: load_bucket(values, "weekday")
            for key, values in buckets.items()
            if len(values) >= 4
        }
        for weekday in range(7):
            for (hour, quarter), clock_values in clock_buckets.items():
                key = (weekday, hour, quarter)
                if key in result:
                    continue
                day_type_values = day_type_buckets.get(
                    (weekday < 5, hour, quarter),
                    [],
                )
                if len(day_type_values) >= 4:
                    result[key] = load_bucket(day_type_values, "day_type")
                elif len(clock_values) >= 4:
                    result[key] = load_bucket(clock_values, "all_days")
        return result

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
