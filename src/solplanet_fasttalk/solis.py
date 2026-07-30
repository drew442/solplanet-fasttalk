"""Optional read-only Solis diagnostics plugin."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import SolisConfig
from .model import Measurement, PlantState, utc_now
from .modbus import ModbusError, build_read_request, parse_read_response
from .plugins import PluginDescriptor
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
    start: int
    count: int
    fields: tuple[Field, ...]


GROUPS = (
    Group(
        "identity_and_power",
        2999,
        9,
        (
            Field(0, "solis.product_model_code"),
            Field(1, "solis.firmware.dsp", "hex16"),
            Field(2, "solis.firmware.lcd", "hex16"),
            Field(5, "solis.active_power", "u32", 1, "W"),
            Field(7, "solis.dc.total_power", "u32", 1, "W"),
        ),
    ),
    Group(
        "energy",
        3008,
        8,
        (
            Field(0, "solis.energy.total", "u32", 1, "kWh"),
            Field(2, "solis.energy.this_month", "u32", 1, "kWh"),
            Field(4, "solis.energy.last_month", "u32", 1, "kWh"),
            Field(6, "solis.energy.today", "u16", 0.1, "kWh"),
            Field(7, "solis.energy.yesterday", "u16", 0.1, "kWh"),
        ),
    ),
    Group(
        "dc_inputs",
        3021,
        8,
        tuple(
            item
            for index in range(4)
            for item in (
                Field(index * 2, f"solis.dc.input.{index + 1}.voltage", "u16", 0.1, "V"),
                Field(index * 2 + 1, f"solis.dc.input.{index + 1}.current", "u16", 0.1, "A"),
            )
        ),
    ),
    Group(
        "ac_output",
        3033,
        11,
        (
            Field(0, "solis.ac.phase.l1.voltage", "u16", 0.1, "V"),
            Field(1, "solis.ac.phase.l2.voltage", "u16", 0.1, "V"),
            Field(2, "solis.ac.phase.l3.voltage", "u16", 0.1, "V"),
            Field(3, "solis.ac.phase.l1.current", "u16", 0.1, "A"),
            Field(4, "solis.ac.phase.l2.current", "u16", 0.1, "A"),
            Field(5, "solis.ac.phase.l3.current", "u16", 0.1, "A"),
            Field(7, "solis.working_mode"),
            Field(8, "solis.temperature", "u16", 0.1, "degC"),
            Field(9, "solis.grid_frequency", "u16", 0.01, "Hz"),
            Field(10, "solis.status"),
        ),
    ),
)


def _decode(field: Field, registers: list[int]) -> Any:
    width = 2 if field.kind == "u32" else 1
    words = registers[field.offset : field.offset + width]
    if len(words) != width or all(word == 0xFFFF for word in words):
        return None
    if field.kind == "hex16":
        return f"0x{words[0]:04x}"
    raw = words[0] if width == 1 else words[0] << 16 | words[1]
    return raw * field.scale


def decode_group(
    group: Group,
    registers: list[int],
    observed_at: str,
    observed_monotonic: float,
    *,
    slave: int = 1,
) -> list[Measurement]:
    result = []
    for field in group.fields:
        value = _decode(field, registers)
        result.append(
            Measurement(
                field.name,
                value,
                field.unit,
                "plugin.solis_rs485",
                "diagnostic",
                "direct_wired_modbus",
                observed_at,
                observed_monotonic,
                20.0,
                "unavailable" if value is None else "good",
                {
                    "plugin_interface": 1,
                    "slave": slave,
                    "function": "0x04",
                    "pdu_start": group.start,
                    "register_offset": field.offset,
                },
            )
        )
    return result


class SolisPlugin:
    descriptor = PluginDescriptor(
        "solis-rs485",
        1,
        "pv_inverter",
        (
            "dc_inputs",
            "temperature",
            "operating_state",
            "diagnostic_ac_power",
            "energy_counters",
        ),
        (),
        ("external_pv_ac",),
    )

    def __init__(self, config: SolisConfig, state: PlantState) -> None:
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
                    "solis",
                    status="starting",
                    device=self.config.device,
                    access_mode="direct_wired_modbus",
                    plugin_interface=1,
                    permitted_functions=["0x04"],
                    write_capability=False,
                )
                with SerialRTU(
                    self.config.device,
                    self.config.baud,
                    self.config.timeout_seconds,
                ) as port:
                    backoff = 1.0
                    while not stop.is_set():
                        for group in GROUPS:
                            self._read(port, group)
                            if stop.wait(0.12):
                                return
                        self.state.update_health(
                            "solis",
                            status="ok",
                            connected=True,
                            successful_reads=self.reads,
                            failed_reads=self.failures,
                            reconnects=self.reconnects,
                            accounting_authority=False,
                        )
                        stop.wait(self.config.poll_interval_seconds)
            except (ModbusError, OSError) as exc:
                self.failures += 1
                self.reconnects += 1
                self.state.update_health(
                    "solis",
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
            self.config.slave, 0x04, group.start, group.count
        )
        response = port.exchange(request)
        registers = parse_read_response(request, response, group.count)
        self.state.publish_many(
            decode_group(
                group,
                registers,
                utc_now(),
                time.monotonic(),
                slave=self.config.slave,
            )
        )
        self.reads += 1
