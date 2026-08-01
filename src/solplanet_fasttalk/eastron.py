"""Passive Eastron SEM3 terminal-8 integration."""

from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .config import EastronConfig
from .model import Measurement, PlantState, utc_now
from .modbus import RTUStreamDecoder, Transaction, TransactionMatcher
from .serial_readonly import ReadOnlySerial, ReadOnlySerialError


@dataclass(frozen=True)
class FloatField:
    offset: int
    suffix: str
    unit: str


GENERAL_FIELDS = (
    FloatField(0, "phase.l1.voltage", "V"),
    FloatField(2, "phase.l2.voltage", "V"),
    FloatField(4, "phase.l3.voltage", "V"),
    FloatField(6, "phase.l1.current", "A"),
    FloatField(8, "phase.l2.current", "A"),
    FloatField(10, "phase.l3.current", "A"),
    FloatField(12, "phase.l1.active_power", "W"),
    FloatField(14, "phase.l2.active_power", "W"),
    FloatField(16, "phase.l3.active_power", "W"),
    FloatField(18, "phase.l1.apparent_power", "VA"),
    FloatField(20, "phase.l2.apparent_power", "VA"),
    FloatField(22, "phase.l3.apparent_power", "VA"),
    FloatField(24, "phase.l1.reactive_power", "var"),
    FloatField(26, "phase.l2.reactive_power", "var"),
    FloatField(28, "phase.l3.reactive_power", "var"),
    FloatField(30, "phase.l1.power_factor", ""),
    FloatField(32, "phase.l2.power_factor", ""),
    FloatField(34, "phase.l3.power_factor", ""),
    FloatField(36, "phase.l1.angle", "deg"),
    FloatField(38, "phase.l2.angle", "deg"),
    FloatField(40, "phase.l3.angle", "deg"),
    FloatField(42, "voltage.average", "V"),
    FloatField(46, "current.average", "A"),
    FloatField(48, "current.sum", "A"),
    FloatField(52, "meter_total_active_power", "W"),
    FloatField(56, "apparent_power", "VA"),
    FloatField(60, "reactive_power", "var"),
    FloatField(62, "power_factor", ""),
    FloatField(66, "angle", "deg"),
    FloatField(70, "frequency", "Hz"),
    FloatField(72, "energy.forward_active", "kWh"),
    FloatField(74, "energy.reverse_active", "kWh"),
    FloatField(76, "energy.forward_reactive", "kvarh"),
    FloatField(78, "energy.reverse_reactive", "kvarh"),
    FloatField(80, "energy.apparent", "kVAh"),
)

LINE_FIELDS = (
    FloatField(200, "voltage.l1_l2", "V"),
    FloatField(202, "voltage.l2_l3", "V"),
    FloatField(204, "voltage.l3_l1", "V"),
    FloatField(206, "voltage.line_average", "V"),
)

ENERGY_FIELDS = (
    FloatField(342, "energy.total_active", "kWh"),
    FloatField(344, "energy.total_reactive", "kvarh"),
    FloatField(346, "phase.l1.energy.forward_active", "kWh"),
    FloatField(348, "phase.l2.energy.forward_active", "kWh"),
    FloatField(350, "phase.l3.energy.forward_active", "kWh"),
    FloatField(352, "phase.l1.energy.reverse_active", "kWh"),
    FloatField(354, "phase.l2.energy.reverse_active", "kWh"),
    FloatField(356, "phase.l3.energy.reverse_active", "kWh"),
    FloatField(358, "phase.l1.energy.total_active", "kWh"),
    FloatField(360, "phase.l2.energy.total_active", "kWh"),
    FloatField(362, "phase.l3.energy.total_active", "kWh"),
    FloatField(364, "phase.l1.energy.forward_reactive", "kvarh"),
    FloatField(366, "phase.l2.energy.forward_reactive", "kvarh"),
    FloatField(368, "phase.l3.energy.forward_reactive", "kvarh"),
    FloatField(370, "phase.l1.energy.reverse_reactive", "kvarh"),
    FloatField(372, "phase.l2.energy.reverse_reactive", "kvarh"),
    FloatField(374, "phase.l3.energy.reverse_reactive", "kvarh"),
    FloatField(376, "phase.l1.energy.total_reactive", "kvarh"),
    FloatField(378, "phase.l2.energy.total_reactive", "kvarh"),
    FloatField(380, "phase.l3.energy.total_reactive", "kvarh"),
)


def _float_at(transaction: Transaction, absolute_offset: int) -> float | None:
    relative = absolute_offset - transaction.pdu_start
    if relative < 0 or relative + 2 > transaction.count:
        return None
    start = relative * 2
    value = struct.unpack(">f", transaction.data[start : start + 4])[0]
    return value if math.isfinite(value) else None


class EastronDecoder:
    def __init__(self, config: EastronConfig) -> None:
        self.config = config

    def decode(
        self,
        transaction: Transaction,
        *,
        observed_at: str | None = None,
        observed_monotonic: float | None = None,
    ) -> list[Measurement]:
        channel = self._channel(transaction.slave)
        if channel is None or transaction.function != 0x04:
            return []
        prefix, source, multiplier = channel
        now_mono = time.monotonic() if observed_monotonic is None else observed_monotonic
        timestamp = utc_now() if observed_at is None else observed_at
        fields = (
            GENERAL_FIELDS
            if transaction.pdu_start in (0, 12)
            else LINE_FIELDS
            if transaction.pdu_start == 200
            else ENERGY_FIELDS
            if transaction.pdu_start == 342
            else ()
        )
        max_age = 2.0 if transaction.slave == self.config.grid_slave and transaction.pdu_start == 12 else 20.0
        if transaction.pdu_start in (200, 342):
            max_age = 90.0
        result: list[Measurement] = []
        phase_powers: list[float] = []
        decoded_values: dict[str, float] = {}
        for field in fields:
            value = _float_at(transaction, field.offset)
            if value is None:
                continue
            if field.suffix.endswith("active_power"):
                value *= multiplier
            name = f"{prefix}.{field.suffix}"
            decoded_values[field.suffix] = value
            field_max_age = 90.0 if ".energy." in f".{field.suffix}." else max_age
            if field.suffix == "energy.forward_active":
                alias = "energy.import" if prefix == "grid" else "energy.generated"
                result.append(
                    self._measurement(
                        f"{prefix}.{alias}",
                        value,
                        field.unit,
                        source,
                        timestamp,
                        now_mono,
                        field_max_age,
                        transaction,
                    )
                )
            elif field.suffix == "energy.reverse_active":
                alias = "energy.export" if prefix == "grid" else "energy.reverse"
                result.append(
                    self._measurement(
                        f"{prefix}.{alias}",
                        value,
                        field.unit,
                        source,
                        timestamp,
                        now_mono,
                        field_max_age,
                        transaction,
                    )
                )
            result.append(
                self._measurement(
                    name,
                    value,
                    field.unit,
                    source,
                    timestamp,
                    now_mono,
                    field_max_age,
                    transaction,
                )
            )
            if field.suffix in (
                "phase.l1.active_power",
                "phase.l2.active_power",
                "phase.l3.active_power",
            ):
                phase_powers.append(value)
        if len(phase_powers) == 3:
            raw_total = round(sum(phase_powers), 6)
            # Slave 2 is installed around a generation feeder. Small negative
            # readings around dusk are inverter standby consumption and CT /
            # meter noise, not negative PV generation. Preserve the signed
            # per-phase and meter-total registers for diagnostics while making
            # the authoritative plant-facing PV aggregate physically bounded.
            aggregate = max(0.0, raw_total) if prefix == "external_pv" else raw_total
            result.append(
                self._measurement(
                    f"{prefix}.active_power",
                    aggregate,
                    "W",
                    source,
                    timestamp,
                    now_mono,
                    max_age,
                    transaction,
                    {
                        "method": "sum_of_phases",
                        "negative_generation_clamped": prefix == "external_pv",
                        "unclamped_value_w": raw_total,
                    },
                )
            )
        if transaction.pdu_start == 342:
            for direction, alias in (
                (
                    "forward",
                    "energy.import" if prefix == "grid" else "energy.generated",
                ),
                (
                    "reverse",
                    "energy.export" if prefix == "grid" else "energy.reverse",
                ),
            ):
                keys = [
                    f"phase.{phase}.energy.{direction}_active"
                    for phase in ("l1", "l2", "l3")
                ]
                if all(key in decoded_values for key in keys):
                    result.append(
                        self._measurement(
                            f"{prefix}.{alias}",
                            sum(decoded_values[key] for key in keys),
                            "kWh",
                            source,
                            timestamp,
                            now_mono,
                            90.0,
                            transaction,
                            {"method": "sum_of_phases"},
                        )
                    )
        return result

    def _channel(self, slave: int) -> tuple[str, str, float] | None:
        if slave == self.config.grid_slave:
            return "grid", "eastron.grid", self.config.grid_power_multiplier
        if slave == self.config.external_pv_slave:
            return (
                "external_pv",
                "eastron.external_pv",
                self.config.external_pv_power_multiplier,
            )
        return None

    @staticmethod
    def _measurement(
        name: str,
        value: float,
        unit: str,
        source: str,
        timestamp: str,
        monotonic: float,
        max_age: float,
        transaction: Transaction,
        metadata: dict[str, str] | None = None,
    ) -> Measurement:
        details = {
            "slave": transaction.slave,
            "function": f"0x{transaction.function:02x}",
            "pdu_start": transaction.pdu_start,
            "count": transaction.count,
        }
        if metadata:
            details.update(metadata)
        return Measurement(
            name,
            round(value, 9),
            unit,
            source,
            "authoritative",
            "passive_bus",
            timestamp,
            monotonic,
            max_age,
            metadata=details,
        )


class EastronWorker:
    def __init__(self, config: EastronConfig, state: PlantState) -> None:
        self.config = config
        self.state = state
        self.decoder = EastronDecoder(config)
        self.stream = RTUStreamDecoder()
        self.matcher = TransactionMatcher()
        self.transactions = 0
        self.reconnects = 0
        self.connected_since = 0.0
        self.last_transaction = 0.0

    def run(self, stop: threading.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                self.state.update_health(
                    "eastron",
                    status="starting",
                    access_mode="passive_bus",
                    device=self.config.device,
                    transmit_capability=False,
                )
                with ReadOnlySerial(self.config.device, self.config.baud) as port:
                    self.connected_since = time.monotonic()
                    self.state.update_health("eastron", status="ok", connected=True)
                    backoff = 1.0
                    while not stop.is_set():
                        chunk = port.read(0.5)
                        if not chunk:
                            self._publish_health()
                            continue
                        observed_at = utc_now()
                        observed_monotonic = time.monotonic()
                        for frame in self.stream.feed(chunk):
                            transaction = self.matcher.accept(frame)
                            if transaction is None:
                                continue
                            self.transactions += 1
                            self.last_transaction = observed_monotonic
                            measurements = self.decoder.decode(
                                transaction,
                                observed_at=observed_at,
                                observed_monotonic=observed_monotonic,
                            )
                            self.state.publish_many(measurements)
                        self._publish_health()
            except (ReadOnlySerialError, OSError) as exc:
                self.reconnects += 1
                self.state.update_health(
                    "eastron",
                    status="failed",
                    connected=False,
                    error=str(exc),
                    reconnects=self.reconnects,
                )
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _publish_health(self) -> None:
        now = time.monotonic()
        last_activity = self.last_transaction or self.connected_since
        silent_seconds = max(0.0, now - last_activity)
        degraded = (
            self.matcher.exceptions > 0
            or self.matcher.unmatched_responses > 0
            or self.matcher.missing_responses > 0
            or self.stream.suspect_crc_frames > 0
            or silent_seconds > 5.0
        )
        self.state.update_health(
            "eastron",
            status="degraded" if degraded else "ok",
            connected=True,
            frames=self.stream.frames,
            transactions=self.transactions,
            discarded_bytes=self.stream.discarded_bytes,
            suspect_crc_frames=self.stream.suspect_crc_frames,
            unmatched_responses=self.matcher.unmatched_responses,
            missing_responses=self.matcher.missing_responses,
            modbus_exceptions=self.matcher.exceptions,
            buffered_bytes=self.stream.buffered_bytes,
            seconds_since_transaction=round(silent_seconds, 3),
        )
