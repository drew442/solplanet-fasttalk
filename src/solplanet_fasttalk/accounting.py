"""Persistent tariff accounting from the authoritative grid meter."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from .model import PlantState
from .storage import HistoryReader, dt_from_iso
from .tariff import ZeroHeroTariff


class FinancialAccountingWorker:
    """Convert complete grid-power minutes into an auditable cost ledger."""

    def __init__(
        self,
        history: HistoryReader,
        tariff: ZeroHeroTariff,
        state: PlantState,
        *,
        raw_retention_days: int,
    ) -> None:
        self.history = history
        self.tariff = tariff
        self.state = state
        self.raw_retention_days = raw_retention_days
        self.intervals_written = 0
        self.failures = 0

    def run(self, stop: threading.Event) -> None:
        self.state.update_health("financial_accounting", status="starting")
        while not stop.is_set():
            try:
                written = self.run_once()
                self.state.update_health(
                    "financial_accounting",
                    status="ok",
                    error=None,
                    intervals_written=self.intervals_written,
                    latest_period=self.history.latest_financial_period(),
                    model="authoritative grid minute averages",
                )
                delay = 5 if written else 60
            except Exception as exc:
                self.failures += 1
                self.state.update_health(
                    "financial_accounting",
                    status="degraded",
                    failures=self.failures,
                    error=str(exc),
                )
                delay = 30
            stop.wait(delay)

    def run_once(self, now: dt.datetime | None = None) -> int:
        instant = (now or dt.datetime.now(dt.timezone.utc)).astimezone(
            dt.timezone.utc
        )
        until = instant.replace(second=0, microsecond=0)
        latest = self.history.latest_financial_period()
        retention_start = until - dt.timedelta(days=self.raw_retention_days)
        since = (
            dt_from_iso(latest) + dt.timedelta(minutes=1)
            if latest
            else retention_start
        )
        since = max(since, retention_start)
        if since >= until:
            return 0

        rows = self.history.grid_minute_buckets(
            since=since.isoformat(),
            until=until.isoformat(),
        )
        by_day: dict[str, dict[str, Any]] = {}
        intervals: list[dict[str, Any]] = []
        for row in rows:
            period = dt_from_iso(row["period_start"])
            local = period.astimezone(self.tariff.timezone)
            local_date = local.date().isoformat()
            day = by_day.setdefault(
                local_date,
                self.history.financial_day_state(local_date),
            )
            quote = self.tariff.quote(period + dt.timedelta(seconds=30))
            power = float(row["average_grid_power_w"])
            imported = max(0.0, power) / 60000.0
            exported = max(0.0, -power) / 60000.0
            export_price = quote.export_price_per_kwh
            if quote.export_period == "super_export":
                remaining = max(0.0, 15.0 - day["super_export_kwh"])
                premium = min(exported, remaining)
                export_credit = premium * 0.15 + (exported - premium) * 0.05
                day["super_export_kwh"] += exported
                export_price = (
                    export_credit / exported if exported else export_price
                )
            else:
                export_credit = exported * export_price
            import_cost = imported * quote.import_price_per_kwh
            intervals.append(
                {
                    "period_start": period.isoformat(),
                    "local_date": local_date,
                    "local_hour": local.hour,
                    "average_grid_power_w": power,
                    "imported_kwh": imported,
                    "exported_kwh": exported,
                    "import_price_per_kwh": quote.import_price_per_kwh,
                    "export_price_per_kwh": export_price,
                    "import_cost": import_cost,
                    "export_credit": export_credit,
                    "net_energy_cost": import_cost - export_credit,
                    "samples": int(row["samples"]),
                    "import_period": quote.import_period,
                    "export_period": quote.export_period,
                    "plan_id": quote.plan_id,
                }
            )
            if local.hour in day["zerohero_import_kwh"]:
                day["zerohero_import_kwh"][local.hour] += imported
                day["zerohero_minutes"][local.hour] += 1

        self.history.record_financial_intervals(intervals)
        self.intervals_written += len(intervals)
        for local_date in by_day:
            self._apply_daily_adjustments(local_date, until)
        return len(intervals)

    def _apply_daily_adjustments(
        self,
        local_date: str,
        processed_until: dt.datetime,
    ) -> None:
        state = self.history.financial_day_state(local_date)
        day = dt.date.fromisoformat(local_date)
        local_midnight = dt.datetime.combine(
            day,
            dt.time(),
            tzinfo=self.tariff.timezone,
        )
        quote = self.tariff.quote(local_midnight)
        if state["intervals"] and "daily_supply" not in state["adjustments"]:
            self.history.record_financial_adjustment(
                local_date=local_date,
                kind="daily_supply",
                occurred_at=local_midnight.astimezone(dt.timezone.utc).isoformat(),
                amount=quote.daily_supply_charge,
                description="Daily supply charge",
                plan_id=quote.plan_id,
            )

        end_of_window = local_midnight + dt.timedelta(hours=21)
        window_complete = processed_until >= end_of_window.astimezone(
            dt.timezone.utc
        )
        earned = all(
            state["zerohero_minutes"][hour] >= 60
            and state["zerohero_import_kwh"][hour] <= 0.03
            for hour in (18, 19, 20)
        )
        if (
            window_complete
            and earned
            and "zerohero_credit" not in state["adjustments"]
        ):
            self.history.record_financial_adjustment(
                local_date=local_date,
                kind="zerohero_credit",
                occurred_at=end_of_window.astimezone(dt.timezone.utc).isoformat(),
                amount=-1.0,
                description="Earned ZEROHERO 18:00–21:00 credit",
                plan_id=quote.plan_id,
            )
