"""Read-only Solplanet ASW MONITOR integration."""

from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import ASWConfig
from .model import Measurement, PlantState, utc_now
from .modbus import ModbusError, build_read_request, parse_read_response
from .serial_rtu import SerialRTU


@dataclass(frozen=True)
class Field:
    offset: int
    name: str
    kind: str = "u16"
    scale: float = 1.0
    unit: str = ""


@dataclass(frozen=True)
class Group:
    name: str
    function: int
    pdu_start: int
    count: int
    interval: float | None
    max_age: float
    fields: tuple[Field, ...]


IDENTITY_GROUPS = (
    Group(
        "device_header",
        0x04,
        1000,
        2,
        None,
        86400,
        (Field(0, "asw.device_type", "string1"), Field(1, "asw.modbus_address")),
    ),
    Group(
        "machine_identity",
        0x04,
        1018,
        32,
        None,
        86400,
        (
            Field(0, "asw.model", "string8"),
            Field(8, "asw.grid_code"),
            Field(9, "asw.rated_power", "u32", 1, "W"),
            Field(11, "asw.firmware.master", "string7"),
            Field(18, "asw.firmware.slave", "string7"),
            Field(25, "asw.firmware.safety", "string7"),
        ),
    ),
)

POLL_GROUPS = (
    Group(
        "inverter_power",
        0x04,
        1370,
        9,
        1.0,
        3.0,
        (
            Field(0, "asw.active_power", "s32", 1, "W"),
            Field(2, "asw.reactive_power", "s32", 1, "var"),
            Field(4, "asw.power_factor", "s16", 0.01),
            Field(6, "asw.fault.state", "hex16"),
            Field(7, "asw.fault.error", "hex16"),
            Field(8, "asw.fault.warning", "hex16"),
        ),
    ),
    Group(
        "storage_battery",
        0x04,
        1600,
        33,
        1.0,
        3.0,
        (
            Field(0, "asw.pv.active_power", "u32", 1, "W"),
            Field(2, "asw.pv.energy.today", "u32", 0.1, "kWh"),
            Field(4, "asw.pv.energy.total", "u32", 0.1, "kWh"),
            Field(6, "battery.communication_status", "hex16"),
            Field(7, "battery.status"),
            Field(8, "battery.error.1", "hex16"),
            Field(9, "battery.error.2", "hex16"),
            Field(12, "battery.warning.1", "hex16"),
            Field(16, "battery.voltage", "u16", 0.01, "V"),
            Field(17, "battery.current", "s16", 0.1, "A"),
            Field(18, "battery.power", "s32", 1, "W"),
            Field(20, "battery.temperature", "s16", 0.1, "degC"),
            Field(21, "battery.soc", "u16", 1, "%"),
            # The live firmware returns 100 for a healthy battery, matching
            # whole-percent SOC encoding rather than the documented 0.01 scale.
            Field(22, "battery.soh", "u16", 1, "%"),
            Field(23, "battery.limit.charge_current", "u16", 0.1, "A"),
            Field(24, "battery.limit.discharge_current", "u16", 0.1, "A"),
            Field(25, "battery.energy.charge_today", "u32", 0.1, "kWh"),
            Field(27, "battery.energy.discharge_today", "u32", 0.1, "kWh"),
            Field(
                29,
                "asw.reported_site.energy.consumption_today",
                "u32",
                0.1,
                "kWh",
            ),
            Field(
                31,
                "asw.reported_site.energy.generation_today",
                "u32",
                0.1,
                "kWh",
            ),
        ),
    ),
    Group(
        "meter_state",
        0x03,
        1107,
        8,
        2.0,
        7.0,
        (
            Field(0, "asw.smart_meter.status", "hex16"),
            Field(1, "asw.smart_meter.adjustment_flag", "hex16"),
            Field(2, "asw.smart_meter.target_power", "s32", 1, "W"),
            Field(4, "asw.smart_meter.active_power", "s32", 1, "W"),
            Field(6, "asw.anti_reverse_flag", "hex16"),
            Field(7, "asw.battery_wakeup_flag", "hex16"),
        ),
    ),
    Group(
        "control_state",
        0x03,
        1150,
        6,
        2.0,
        7.0,
        (
            Field(0, "asw.cloud_communication_status", "hex16"),
            Field(1, "asw.control.charge_discharge_state"),
            Field(2, "asw.control.power_command", "s16", 1, "W"),
            Field(3, "battery.limit.soc_upper", "u16", 0.01, "%"),
            Field(4, "battery.limit.soc_lower", "u16", 0.01, "%"),
            Field(5, "asw.control.grid_power_ratio", "u16", 0.01, "%"),
        ),
    ),
    Group(
        "inverter_status",
        0x04,
        1300,
        9,
        5.0,
        15.0,
        (
            Field(0, "asw.grid_rated_voltage", "u16", 0.1, "V"),
            Field(1, "asw.grid_rated_frequency", "u16", 0.01, "Hz"),
            Field(2, "asw.energy.today", "s32", 0.1, "kWh"),
            Field(4, "asw.energy.total", "s32", 0.1, "kWh"),
            Field(6, "asw.operating_hours", "u32", 1, "h"),
            Field(8, "asw.device_state"),
        ),
    ),
    Group(
        "grid_port",
        0x04,
        1662,
        19,
        5.0,
        15.0,
        (
            Field(0, "asw.grid_port.phase.l1.active_power", "s32", 1, "W"),
            Field(2, "asw.grid_port.phase.l1.reactive_power", "s32", 1, "var"),
            Field(4, "asw.grid_port.phase.l2.active_power", "s32", 1, "W"),
            Field(6, "asw.grid_port.phase.l2.reactive_power", "s32", 1, "var"),
            Field(8, "asw.grid_port.phase.l3.active_power", "s32", 1, "W"),
            Field(10, "asw.grid_port.phase.l3.reactive_power", "s32", 1, "var"),
            Field(12, "asw.grid.energy.charge_today", "u32", 0.1, "kWh"),
            Field(14, "asw.grid.energy.charge_total", "u32", 0.1, "kWh"),
            Field(16, "battery.insulation_resistance", "u16", 1, "kohm"),
            Field(17, "battery.cycles"),
            Field(18, "asw.environment_temperature", "u16", 0.1, "degC"),
        ),
    ),
)


def _width(kind: str) -> int:
    return int(kind[6:]) if kind.startswith("string") else 2 if kind in ("u32", "s32", "float32") else 1


def _documented_nan(kind: str, words: list[int]) -> bool:
    return (
        (kind in ("u16", "hex16") and words == [0xFFFF])
        or (kind == "s16" and words == [0x8000])
        or (kind == "u32" and words == [0xFFFF, 0xFFFF])
        or (kind == "s32" and words == [0x8000, 0])
    )


def _decode(kind: str, words: list[int], scale: float) -> Any:
    if _documented_nan(kind, words):
        return None
    if kind.startswith("string"):
        raw = b"".join(word.to_bytes(2, "big") for word in words)
        return raw.strip(b"\x00\xff ").decode("ascii", "replace")
    if kind == "hex16":
        return f"0x{words[0]:04x}"
    raw = words[0] if len(words) == 1 else words[0] << 16 | words[1]
    if kind in ("s16", "s32"):
        bits = 16 if kind == "s16" else 32
        if raw & 1 << (bits - 1):
            raw -= 1 << bits
    if kind == "float32":
        value = struct.unpack(">f", struct.pack(">HH", *words))[0]
        return value * scale if math.isfinite(value) else None
    return raw * scale


def decode_group(
    group: Group,
    registers: list[int],
    observed_at: str,
    observed_monotonic: float,
    slave: int = 3,
) -> list[Measurement]:
    result: list[Measurement] = []
    for field in group.fields:
        width = _width(field.kind)
        words = registers[field.offset : field.offset + width]
        if len(words) != width:
            continue
        value = _decode(field.kind, words, field.scale)
        authority = (
            "reported"
            if field.name.startswith("asw.reported_site.")
            else "diagnostic"
            if field.name.startswith(("asw.smart_meter.", "asw.grid_port."))
            else "authoritative"
        )
        result.append(
            Measurement(
                field.name,
                value,
                field.unit,
                "asw.monitor",
                authority,
                "direct_wired_modbus",
                observed_at,
                observed_monotonic,
                group.max_age,
                "unavailable" if value is None else "good",
                {
                    "slave": slave,
                    "function": f"0x{group.function:02x}",
                    "pdu_start": group.pdu_start,
                    "register_offset": field.offset,
                    "raw_words": [f"0x{word:04x}" for word in words],
                },
            )
        )
    return result


class ASWWorker:
    def __init__(self, config: ASWConfig, state: PlantState) -> None:
        self.config = config
        self.state = state
        self.reads = 0
        self.failures = 0
        self.reconnects = 0

    def run(self, stop: threading.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                self.state.update_health(
                    "asw",
                    status="starting",
                    device=self.config.device,
                    access_mode="direct_wired_modbus",
                    permitted_functions=["0x03", "0x04"],
                    write_capability=False,
                )
                with SerialRTU(
                    self.config.device,
                    self.config.baud,
                    self.config.timeout_seconds,
                ) as port:
                    for group in IDENTITY_GROUPS:
                        self._read(port, group)
                        if stop.wait(0.15):
                            return
                    due = {group.name: time.monotonic() for group in POLL_GROUPS}
                    groups = {group.name: group for group in POLL_GROUPS}
                    backoff = 1.0
                    self.state.update_health("asw", status="ok", connected=True)
                    while not stop.is_set():
                        name = min(due, key=due.get)
                        wait = due[name] - time.monotonic()
                        if wait > 0 and stop.wait(wait):
                            return
                        group = groups[name]
                        self._read(port, group)
                        due[name] = time.monotonic() + float(group.interval)
                        self.state.update_health(
                            "asw",
                            status="ok",
                            connected=True,
                            successful_reads=self.reads,
                            failed_reads=self.failures,
                            reconnects=self.reconnects,
                        )
                        if stop.wait(0.10):
                            return
            except (ModbusError, OSError) as exc:
                self.failures += 1
                self.reconnects += 1
                self.state.update_health(
                    "asw",
                    status="failed",
                    connected=False,
                    error=str(exc),
                    successful_reads=self.reads,
                    failed_reads=self.failures,
                    reconnects=self.reconnects,
                )
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _read(self, port: SerialRTU, group: Group) -> None:
        request = build_read_request(
            self.config.slave,
            group.function,
            group.pdu_start,
            group.count,
        )
        response = port.exchange(request)
        registers = parse_read_response(request, response, group.count)
        observed_at = utc_now()
        observed_monotonic = time.monotonic()
        measurements = decode_group(
            group,
            registers,
            observed_at,
            observed_monotonic,
            self.config.slave,
        )
        if self.config.active_power_multiplier != 1:
            measurements = [
                Measurement(
                    **{
                        **measurement.__dict__,
                        "value": (
                            float(measurement.value)
                            * self.config.active_power_multiplier
                            if measurement.name == "asw.active_power"
                            and isinstance(measurement.value, (int, float))
                            else measurement.value
                        ),
                    }
                )
                for measurement in measurements
            ]
        self.state.publish_many(measurements)
        self.reads += 1
