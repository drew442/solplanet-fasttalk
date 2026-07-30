"""Versioned local tariff model for the supplied pre-July 2026 plan."""

from __future__ import annotations

import datetime as dt
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import TariffConfig


PLAN_ID = "globird-zerohero-vpp-ausgrid-pre-2026-07"
PLAN_SOURCE = (
    "ZEROHERO VPP Residential (Flexible Rate CTL)-Ausgrid "
    "(GLO1059212MRE1), effective 25 May 2026"
)


@dataclass(frozen=True)
class TariffEvent:
    event_id: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    import_credit_per_kwh: float = 0.0
    export_price_per_kwh: float | None = None


@dataclass(frozen=True)
class PriceQuote:
    timestamp: str
    local_timestamp: str
    plan_id: str
    import_price_per_kwh: float
    export_price_per_kwh: float
    controlled_load_price_per_kwh: float
    daily_supply_charge: float
    import_period: str
    export_period: str
    zerohero_window: bool
    zerohero_hourly_import_threshold_kwh: float | None
    zerohero_daily_credit: float | None
    super_export_daily_cap_kwh: float | None
    exceptional_event: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ZeroHeroTariff:
    """Archived plan rates; no vendor VPP enrollment is assumed."""

    def __init__(self, config: TariffConfig) -> None:
        if config.plan != PLAN_ID:
            raise ValueError(f"unsupported tariff plan: {config.plan}")
        self.config = config
        self.timezone = ZoneInfo(config.timezone)
        self.events = self._load_events(config.exceptional_events_file)

    def quote(self, when: dt.datetime) -> PriceQuote:
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.timezone, fold=0)
        local = when.astimezone(self.timezone)
        minute = local.hour * 60 + local.minute
        if 11 * 60 <= minute < 14 * 60:
            import_price, import_period = 0.0, "off_peak"
        elif 16 * 60 <= minute < 23 * 60:
            import_price, import_period = 0.572, "peak"
        else:
            import_price, import_period = 0.462, "shoulder"

        if 18 * 60 <= minute < 21 * 60:
            export_price, export_period = 0.15, "super_export"
            export_cap = 15.0
        elif 16 * 60 <= minute < 23 * 60:
            export_price, export_period = 0.05, "base_feed_in"
            export_cap = None
        else:
            export_price, export_period = 0.0, "no_feed_in"
            export_cap = None

        event = next(
            (
                item
                for item in self.events
                if item.starts_at <= local < item.ends_at
            ),
            None,
        )
        if event is not None:
            import_price = max(
                0.0, import_price - event.import_credit_per_kwh
            )
            if event.export_price_per_kwh is not None:
                export_price = event.export_price_per_kwh
                export_period = "critical_peak_event"

        zerohero = 18 * 60 <= minute < 21 * 60
        return PriceQuote(
            when.astimezone(dt.timezone.utc).isoformat(),
            local.isoformat(),
            PLAN_ID,
            import_price,
            export_price,
            0.319,
            1.65,
            import_period,
            export_period,
            zerohero,
            0.03 if zerohero else None,
            1.0 if zerohero else None,
            export_cap,
            event.event_id if event else None,
        )

    def current(self, when: dt.datetime | None = None) -> dict[str, Any]:
        instant = when or dt.datetime.now(dt.timezone.utc)
        return {
            "plan_id": PLAN_ID,
            "timezone": self.config.timezone,
            "source": PLAN_SOURCE,
            "vpp_control_assumed": False,
            "quote": self.quote(instant).as_dict(),
            "exceptional_events_loaded": len(self.events),
        }

    def _load_events(self, path: str) -> tuple[TariffEvent, ...]:
        if not path:
            return ()
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_events = payload.get("events", [])
        result = []
        for item in raw_events:
            starts = dt.datetime.fromisoformat(item["starts_at"])
            ends = dt.datetime.fromisoformat(item["ends_at"])
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=self.timezone, fold=0)
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=self.timezone, fold=0)
            if ends <= starts:
                raise ValueError("tariff event ends_at must follow starts_at")
            result.append(
                TariffEvent(
                    str(item["id"]),
                    starts.astimezone(self.timezone),
                    ends.astimezone(self.timezone),
                    float(item.get("import_credit_per_kwh", 0.0)),
                    (
                        float(item["export_price_per_kwh"])
                        if "export_price_per_kwh" in item
                        else None
                    ),
                )
            )
        return tuple(sorted(result, key=lambda value: value.starts_at))


class TariffState:
    def __init__(self, tariff: ZeroHeroTariff) -> None:
        self.tariff = tariff
        self._lock = threading.Lock()

    def current(self) -> dict[str, Any]:
        with self._lock:
            return self.tariff.current()
