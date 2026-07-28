#!/usr/bin/env python3
"""Read-only Modbus RTU discovery for the solplanet-fasttalk test plant.

This utility deliberately implements only function codes 0x03 (read holding
registers) and 0x04 (read input registers). It has no arbitrary request or
write-register interface.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fcntl
import glob
import json
import math
import os
from pathlib import Path
import select
import struct
import sys
import termios
import time
from typing import Any, Iterable, Sequence


TOOL_VERSION = "0.4"
READ_FUNCTIONS = frozenset((0x03, 0x04))
BAUD_RATES = {
    2400: termios.B2400,
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


class DiscoveryError(Exception):
    """Expected discovery or protocol failure."""


@dataclasses.dataclass(frozen=True)
class Field:
    name: str
    reference: int
    kind: str = "u16"
    scale: float = 1.0
    unit: str = ""


@dataclasses.dataclass(frozen=True)
class ReadGroup:
    name: str
    function: int
    reference_start: int
    pdu_start: int
    count: int
    fields: tuple[Field, ...] = ()
    extended: bool = False
    note: str = ""


@dataclasses.dataclass(frozen=True)
class Profile:
    name: str
    description: str
    slave: int
    default_baud: int
    request_gap: float
    groups: tuple[ReadGroup, ...]


def f(
    name: str,
    reference: int,
    kind: str = "u16",
    scale: float = 1.0,
    unit: str = "",
) -> Field:
    return Field(name, reference, kind, scale, unit)


SOLIS_PROFILE = Profile(
    name="solis",
    description="Solis-10K direct RS485 read-only discovery",
    slave=1,
    default_baud=9600,
    request_gap=0.35,
    groups=(
        ReadGroup(
            "identity_and_power",
            0x04,
            3000,
            2999,
            9,
            (
                f("product_model_code", 3000),
                f("dsp_software_version", 3001, "hex16"),
                f("lcd_software_version", 3002, "hex16"),
                f("ac_output_type", 3003),
                f("dc_input_type", 3004),
                f("active_power", 3005, "u32", 1, "W"),
                f("total_dc_power", 3007, "u32", 1, "W"),
            ),
        ),
        ReadGroup(
            "energy",
            0x04,
            3009,
            3008,
            8,
            (
                f("total_energy", 3009, "u32", 1, "kWh"),
                f("energy_this_month", 3011, "u32", 1, "kWh"),
                f("energy_last_month", 3013, "u32", 1, "kWh"),
                f("energy_today", 3015, "u16", 0.1, "kWh"),
                f("energy_last_day", 3016, "u16", 0.1, "kWh"),
            ),
        ),
        ReadGroup(
            "dc_inputs",
            0x04,
            3022,
            3021,
            8,
            (
                f("dc_voltage_1", 3022, "u16", 0.1, "V"),
                f("dc_current_1", 3023, "u16", 0.1, "A"),
                f("dc_voltage_2", 3024, "u16", 0.1, "V"),
                f("dc_current_2", 3025, "u16", 0.1, "A"),
                f("dc_voltage_3", 3026, "u16", 0.1, "V"),
                f("dc_current_3", 3027, "u16", 0.1, "A"),
                f("dc_voltage_4", 3028, "u16", 0.1, "V"),
                f("dc_current_4", 3029, "u16", 0.1, "A"),
            ),
        ),
        ReadGroup(
            "ac_output",
            0x04,
            3034,
            3033,
            11,
            (
                f("phase_or_line_voltage_a", 3034, "u16", 0.1, "V"),
                f("phase_or_line_voltage_b", 3035, "u16", 0.1, "V"),
                f("phase_or_line_voltage_c", 3036, "u16", 0.1, "V"),
                f("phase_current_a", 3037, "u16", 0.1, "A"),
                f("phase_current_b", 3038, "u16", 0.1, "A"),
                f("phase_current_c", 3039, "u16", 0.1, "A"),
                f("working_mode", 3041),
                f("inverter_temperature", 3042, "u16", 0.1, "degC"),
                f("grid_frequency", 3043, "u16", 0.01, "Hz"),
                f("inverter_status", 3044),
            ),
        ),
    ),
)


ASW_PROFILE = Profile(
    name="asw",
    description="Solplanet ASW12kH-T3 direct MONITOR-port read-only discovery",
    slave=3,
    default_baud=9600,
    request_gap=0.15,
    groups=(
        ReadGroup(
            "device_header",
            0x04,
            31001,
            1000,
            2,
            (
                f("device_type", 31001, "string1"),
                f("configured_modbus_address", 31002),
            ),
            note="The inverter serial-number range 31003-31018 is intentionally skipped.",
        ),
        ReadGroup(
            "machine_identity",
            0x04,
            31019,
            1018,
            32,
            (
                f("machine_type", 31019, "string8"),
                f("grid_code", 31027),
                f("rated_power", 31028, "u32", 1, "W"),
                f("master_software_version", 31030, "string7"),
                f("slave_software_version", 31037, "string7"),
                f("safety_version", 31044, "string7"),
            ),
        ),
        ReadGroup(
            "manufacturer",
            0x04,
            31057,
            1056,
            16,
            (
                f("manufacturer_name", 31057, "string8"),
                f("brand_name", 31065, "string8"),
            ),
        ),
        ReadGroup(
            "inverter_status",
            0x04,
            31301,
            1300,
            9,
            (
                f("grid_rated_voltage", 31301, "u16", 0.1, "V"),
                f("grid_rated_frequency", 31302, "u16", 0.01, "Hz"),
                f("inverter_energy_today", 31303, "s32", 0.1, "kWh"),
                f("inverter_energy_total", 31305, "s32", 0.1, "kWh"),
                f("operating_hours_total", 31307, "u32", 1, "h"),
                f("device_state", 31309),
            ),
            note=(
                "The tested firmware encodes the two inverter-energy counters "
                "as signed net energy despite V2.1.4 declaring U32."
            ),
        ),
        ReadGroup(
            "inverter_power_and_faults",
            0x04,
            31371,
            1370,
            9,
            (
                f("active_power", 31371, "s32", 1, "W"),
                f("reactive_power", 31373, "s32", 1, "var"),
                f("power_factor", 31375, "s16", 0.01),
                f("inverter_fault_state", 31377),
                f("inverter_error_message", 31378, "hex16"),
                f("inverter_warning_message", 31379, "hex16"),
            ),
        ),
        ReadGroup(
            "storage_and_battery",
            0x04,
            31601,
            1600,
            33,
            (
                f("pv_total_power", 31601, "u32", 1, "W"),
                f("pv_energy_today", 31603, "u32", 0.1, "kWh"),
                f("pv_energy_total", 31605, "u32", 0.1, "kWh"),
                f("battery_communication_status", 31607, "hex16"),
                f("battery_status", 31608),
                f("battery_error_status_1", 31609, "hex16"),
                f("battery_error_status_2", 31610, "hex16"),
                f("battery_warning_status_1", 31613, "hex16"),
                f("battery_voltage", 31617, "u16", 0.01, "V"),
                f("battery_current", 31618, "s16", 0.1, "A"),
                f("battery_power", 31619, "s32", 1, "W"),
                f("battery_temperature", 31621, "s16", 0.1, "degC"),
                f("battery_soc", 31622, "u16", 1, "%"),
                f("battery_soh", 31623, "u16", 0.01, "%"),
                f("battery_charge_current_limit", 31624, "u16", 0.1, "A"),
                f("battery_discharge_current_limit", 31625, "u16", 0.1, "A"),
                f("battery_energy_charge_today", 31626, "u32", 0.1, "kWh"),
                f("battery_energy_discharge_today", 31628, "u32", 0.1, "kWh"),
                f("ac_consumption_today", 31630, "u32", 0.1, "kWh"),
                f("ac_generation_today", 31632, "u32", 0.1, "kWh"),
            ),
        ),
        ReadGroup(
            "grid",
            0x04,
            31663,
            1662,
            19,
            (
                f("grid_phase_1_active_power", 31663, "s32", 1, "W"),
                f("grid_phase_1_reactive_power", 31665, "s32", 1, "var"),
                f("grid_phase_2_active_power", 31667, "s32", 1, "W"),
                f("grid_phase_2_reactive_power", 31669, "s32", 1, "var"),
                f("grid_phase_3_active_power", 31671, "s32", 1, "W"),
                f("grid_phase_3_reactive_power", 31673, "s32", 1, "var"),
                f("grid_energy_charge_today", 31675, "u32", 0.1, "kWh"),
                f("grid_energy_charge_total", 31677, "u32", 0.1, "kWh"),
                f("battery_insulation_resistance", 31679, "u16", 1, "kohm"),
                f("battery_charge_discharge_cycles", 31680),
                f("environment_temperature", 31681, "u16", 0.1, "degC"),
            ),
            note=(
                "The tested firmware encodes phase active power as signed S32 "
                "despite V2.1.4 declaring U32. These values describe the "
                "inverter grid-side AC port, not utility-meter phases."
            ),
        ),
        ReadGroup(
            "smart_meter_state",
            0x03,
            41108,
            1107,
            8,
            (
                f("smart_meter_status", 41108, "hex16"),
                f("smart_meter_adjustment_flag", 41109, "hex16"),
                f("smart_meter_target_power", 41110, "s32", 1, "W"),
                f("smart_meter_current_power", 41112, "s32", 1, "W"),
                f("anti_reverse_current_flag", 41114, "hex16"),
                f("battery_wakeup_flag", 41115, "hex16"),
            ),
            note="These are holding registers, but this request only reads them with function 0x03.",
        ),
        ReadGroup(
            "storage_control_state",
            0x03,
            41151,
            1150,
            6,
            (
                f("cloud_communication_status", 41151, "hex16"),
                f("charge_discharge_state", 41152),
                f("charge_discharge_power_command", 41153, "s16", 1, "W"),
                f("charging_soc_upper_limit", 41154, "u16", 0.01, "%"),
                f("discharge_soc_lower_limit", 41155, "u16", 0.01, "%"),
                f("grid_power_ratio", 41156, "u16", 0.01, "%"),
            ),
            note="Read-only observation of configured state; no value is written.",
        ),
        ReadGroup(
            "ct_phase_voltage_current_experimental",
            0x03,
            46401,
            6400,
            6,
            (
                f("ct_phase_1_voltage", 46401, "u16", 0.1, "V"),
                f("ct_phase_2_voltage", 46402, "u16", 0.1, "V"),
                f("ct_phase_3_voltage", 46403, "u16", 0.1, "V"),
                f("ct_phase_1_current", 46404, "u16", 0.1, "A"),
                f("ct_phase_2_current", 46405, "u16", 0.1, "A"),
                f("ct_phase_3_current", 46406, "u16", 0.1, "A"),
            ),
            extended=True,
            note=(
                "V2.1.4 labels this RW 'CT Data'. This tool only reads it. "
                "Its relationship to the terminal-8 Eastron meter is unverified."
            ),
        ),
        ReadGroup(
            "ct_phase_active_power_experimental",
            0x03,
            46407,
            6406,
            6,
            (
                f("ct_phase_1_power", 46407, "s32", 1, "W"),
                f("ct_phase_2_power", 46409, "s32", 1, "W"),
                f("ct_phase_3_power", 46411, "s32", 1, "W"),
            ),
            extended=True,
        ),
        ReadGroup(
            "ct_phase_apparent_power_experimental",
            0x03,
            46413,
            6412,
            6,
            (
                f("ct_phase_1_apparent_power", 46413, "u32", 1, "VA"),
                f("ct_phase_2_apparent_power", 46415, "u32", 1, "VA"),
                f("ct_phase_3_apparent_power", 46417, "u32", 1, "VA"),
            ),
            extended=True,
        ),
        ReadGroup(
            "ct_phase_reactive_power_experimental",
            0x03,
            46419,
            6418,
            6,
            (
                f("ct_phase_1_reactive_power", 46419, "s32", 1, "var"),
                f("ct_phase_2_reactive_power", 46421, "s32", 1, "var"),
                f("ct_phase_3_reactive_power", 46423, "s32", 1, "var"),
            ),
            extended=True,
        ),
        ReadGroup(
            "ct_phase_factor_angle_experimental",
            0x03,
            46425,
            6424,
            9,
            (
                f("ct_phase_1_power_factor", 46425, "s16", 0.01),
                f("ct_phase_2_power_factor", 46426, "s16", 0.01),
                f("ct_phase_3_power_factor", 46427, "s16", 0.01),
                f("ct_phase_1_angle", 46428, "u16", 1, "deg"),
                f("ct_phase_2_angle", 46429, "u16", 1, "deg"),
                f("ct_phase_3_angle", 46430, "u16", 1, "deg"),
                f("ct_average_voltage", 46431, "u16", 0.1, "V"),
                f("ct_average_current", 46432, "u16", 0.1, "A"),
                f("ct_sum_line_currents", 46433, "u16", 0.1, "A"),
            ),
            extended=True,
        ),
        ReadGroup(
            "ct_system_totals_experimental",
            0x03,
            46434,
            6433,
            9,
            (
                f("ct_total_system_power", 46434, "s32", 1, "W"),
                f("ct_total_system_apparent_power", 46436, "u32", 1, "VA"),
                f("ct_total_system_reactive_power", 46438, "s32", 1, "var"),
                f("ct_total_system_power_factor", 46440, "s16", 0.01),
                f("ct_total_system_angle", 46441, "u16", 1, "deg"),
                f("ct_frequency", 46442, "u16", 0.01, "Hz"),
            ),
            extended=True,
        ),
        ReadGroup(
            "ct_energy_experimental",
            0x03,
            46443,
            6442,
            8,
            (
                f("ct_import_energy", 46443, "u32", 1, "Wh"),
                f("ct_export_energy", 46445, "u32", 1, "Wh"),
                f("ct_import_reactive_energy", 46447, "u32", 1, "varh"),
                f("ct_export_reactive_energy", 46449, "u32", 1, "varh"),
            ),
            extended=True,
        ),
        ReadGroup(
            "ct_register_46451_experimental",
            0x03,
            46451,
            6450,
            1,
            (f("ct_register_46451", 46451, "u16", 0.1, "V"),),
            extended=True,
            note=(
                "V2.1.4 repeats 'Phase 1 line to neutral volts' at 46451 and "
                "marks the CT block as supported only on some models."
            ),
        ),
    ),
)

EASTRON_TUNNEL_PROFILE = Profile(
    name="asw-meter-tunnel",
    description=(
        "Experimental read-only Eastron smart-meter probe through the "
        "Solplanet ASW MONITOR connection"
    ),
    slave=1,
    default_baud=9600,
    request_gap=0.20,
    groups=(
        ReadGroup(
            "channel_1_total_active_power",
            0x04,
            30053,
            52,
            2,
            (f("channel_1_total_active_power", 30053, "float32", 1, "W"),),
            note=(
                "This exact function/address/count combination was recovered "
                "from the 610-50017-05 Ai dongle firmware."
            ),
        ),
        ReadGroup(
            "channel_1_phase_active_power",
            0x04,
            30013,
            12,
            6,
            (
                f("channel_1_phase_1_active_power", 30013, "float32", 1, "W"),
                f("channel_1_phase_2_active_power", 30015, "float32", 1, "W"),
                f("channel_1_phase_3_active_power", 30017, "float32", 1, "W"),
            ),
            note=(
                "The dongle's full three-phase poll starts at this address; "
                "this probe requests only the three active-power values."
            ),
        ),
        ReadGroup(
            "channel_1_frequency_and_active_energy",
            0x04,
            30071,
            70,
            6,
            (
                f("channel_1_frequency", 30071, "float32", 1, "Hz"),
                f("channel_1_import_active_energy", 30073, "float32", 1, "kWh"),
                f("channel_1_export_active_energy", 30075, "float32", 1, "kWh"),
            ),
        ),
        ReadGroup(
            "channel_2_segmented_total_active_power_candidate",
            0x04,
            33053,
            3052,
            2,
            (f("channel_2_total_active_power_candidate", 33053, "float32", 1, "W"),),
            extended=True,
            note=(
                "Experimental read-only hypothesis: other Eastron "
                "multi-channel meters map channel 2 to the channel-1 register "
                "map plus 3000. No SEM3-M-2L protocol document has yet "
                "confirmed this address."
            ),
        ),
        ReadGroup(
            "channel_2_segmented_phase_active_power_candidate",
            0x04,
            33013,
            3012,
            6,
            (
                f("channel_2_phase_1_active_power_candidate", 33013, "float32", 1, "W"),
                f("channel_2_phase_2_active_power_candidate", 33015, "float32", 1, "W"),
                f("channel_2_phase_3_active_power_candidate", 33017, "float32", 1, "W"),
            ),
            extended=True,
            note=(
                "Companion to the channel-2 total-power hypothesis. The "
                "request remains function 0x04 and cannot change meter state."
            ),
        ),
    ),
)

PROFILES = {
    profile.name: profile
    for profile in (SOLIS_PROFILE, ASW_PROFILE, EASTRON_TUNNEL_PROFILE)
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_crc(frame: bytes) -> bytes:
    crc = crc16_modbus(frame)
    return frame + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def build_read_request(slave: int, function: int, pdu_start: int, count: int) -> bytes:
    if not 1 <= slave <= 247:
        raise ValueError("Modbus RTU slave must be between 1 and 247")
    if function not in READ_FUNCTIONS:
        raise ValueError("Only Modbus read functions 0x03 and 0x04 are permitted")
    if not 0 <= pdu_start <= 0xFFFF:
        raise ValueError("PDU start address is outside the 16-bit range")
    if not 1 <= count <= 125:
        raise ValueError("Register count must be between 1 and 125")
    payload = bytes(
        (
            slave,
            function,
            (pdu_start >> 8) & 0xFF,
            pdu_start & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        )
    )
    return append_crc(payload)


def parse_read_response(
    request: bytes,
    response: bytes,
    expected_count: int,
) -> list[int]:
    if len(response) < 5:
        raise DiscoveryError(f"short response ({len(response)} bytes)")
    received_crc = response[-2] | (response[-1] << 8)
    calculated_crc = crc16_modbus(response[:-2])
    if received_crc != calculated_crc:
        raise DiscoveryError(
            f"CRC mismatch: received 0x{received_crc:04x}, "
            f"calculated 0x{calculated_crc:04x}"
        )
    if response[0] != request[0]:
        raise DiscoveryError(
            f"unexpected slave {response[0]}, expected {request[0]}"
        )
    if response[1] == (request[1] | 0x80):
        code = response[2]
        raise DiscoveryError(f"Modbus exception 0x{code:02x}")
    if response[1] != request[1]:
        raise DiscoveryError(
            f"unexpected function 0x{response[1]:02x}, "
            f"expected 0x{request[1]:02x}"
        )
    expected_bytes = expected_count * 2
    if response[2] != expected_bytes:
        raise DiscoveryError(
            f"unexpected byte count {response[2]}, expected {expected_bytes}"
        )
    if len(response) != response[2] + 5:
        raise DiscoveryError(
            f"response length {len(response)} does not match byte count"
        )
    payload = response[3:-2]
    return [
        (payload[index] << 8) | payload[index + 1]
        for index in range(0, len(payload), 2)
    ]


def classify_scan_response(
    request: bytes,
    response: bytes,
    expected_count: int,
) -> dict[str, Any]:
    """Classify a scan response without treating Modbus exceptions as absence."""
    if len(response) < 5:
        raise DiscoveryError(f"short response ({len(response)} bytes)")
    received_crc = response[-2] | (response[-1] << 8)
    calculated_crc = crc16_modbus(response[:-2])
    if received_crc != calculated_crc:
        raise DiscoveryError(
            f"CRC mismatch: received 0x{received_crc:04x}, "
            f"calculated 0x{calculated_crc:04x}"
        )
    if response[0] != request[0]:
        raise DiscoveryError(
            f"unexpected slave {response[0]}, expected {request[0]}"
        )
    if response[1] == (request[1] | 0x80):
        if len(response) != 5:
            raise DiscoveryError(
                f"exception response has unexpected length {len(response)}"
            )
        return {
            "kind": "exception",
            "exception_code": f"0x{response[2]:02x}",
        }
    registers = parse_read_response(request, response, expected_count)
    return {
        "kind": "data",
        "registers": [f"0x{value:04x}" for value in registers],
    }


def _signed(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def _field_width(kind: str) -> int:
    if kind in ("u16", "s16", "hex16"):
        return 1
    if kind in ("u32", "s32", "float32"):
        return 2
    if kind.startswith("string"):
        return int(kind.removeprefix("string"))
    raise ValueError(f"unsupported field type {kind!r}")


def _decode_string(words: Sequence[int]) -> str:
    raw = b"".join(word.to_bytes(2, "big") for word in words)
    text = raw.strip(b"\x00\xff ").decode("ascii", errors="replace")
    return "".join(char if char.isprintable() else "?" for char in text)


def _is_documented_nan(kind: str, words: Sequence[int]) -> bool:
    if kind.startswith("string"):
        return all(word == 0x0000 for word in words)
    if kind in ("u16", "hex16"):
        return list(words) == [0xFFFF]
    if kind == "s16":
        return list(words) == [0x8000]
    if kind == "u32":
        return list(words) == [0xFFFF, 0xFFFF]
    if kind == "s32":
        return list(words) == [0x8000, 0x0000]
    if kind == "float32":
        return list(words) in ([0xFFFF, 0xFFFF], [0x7FC0, 0x0000])
    return False


def _scaled(value: int, scale: float) -> int | float:
    if scale == 1:
        return value
    return round(value * scale, 9)


def decode_fields(group: ReadGroup, registers: Sequence[int]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for field in group.fields:
        offset = field.reference - group.reference_start
        width = _field_width(field.kind)
        words = list(registers[offset : offset + width])
        entry: dict[str, Any] = {
            "reference": field.reference,
            "type": field.kind,
            "raw_words": [f"0x{word:04x}" for word in words],
        }
        if len(words) != width:
            entry["error"] = "field falls outside returned register range"
            decoded[field.name] = entry
            continue
        if _is_documented_nan(field.kind, words):
            entry["value"] = None
            entry["quality"] = "documented_nan"
        elif field.kind == "hex16":
            entry["value"] = f"0x{words[0]:04x}"
        elif field.kind == "u16":
            entry["value"] = _scaled(words[0], field.scale)
        elif field.kind == "s16":
            entry["value"] = _scaled(_signed(words[0], 16), field.scale)
        elif field.kind == "u32":
            entry["value"] = _scaled(
                (words[0] << 16) | words[1],
                field.scale,
            )
        elif field.kind == "s32":
            raw = (words[0] << 16) | words[1]
            entry["value"] = _scaled(_signed(raw, 32), field.scale)
        elif field.kind == "float32":
            value = struct.unpack(
                ">f",
                struct.pack(">HH", words[0], words[1]),
            )[0]
            if math.isfinite(value):
                entry["value"] = round(value * field.scale, 9)
            else:
                entry["value"] = None
                entry["quality"] = "non_finite"
        elif field.kind.startswith("string"):
            entry["value"] = _decode_string(words)
        else:
            entry["error"] = f"unsupported field type {field.kind!r}"
        if field.unit:
            entry["unit"] = field.unit
        decoded[field.name] = entry
    return decoded


def _configure_serial(fd: int, baud: int) -> None:
    try:
        baud_constant = BAUD_RATES[baud]
    except KeyError as exc:
        supported = ", ".join(str(value) for value in sorted(BAUD_RATES))
        raise DiscoveryError(
            f"unsupported baud {baud}; supported values: {supported}"
        ) from exc

    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = baud_constant
    attrs[5] = baud_constant
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)


class SerialRTU:
    def __init__(self, device: str, baud: int, timeout: float) -> None:
        self.device = device
        self.baud = baud
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "SerialRTU":
        try:
            fd = os.open(
                self.device,
                os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK,
            )
        except OSError as exc:
            raise DiscoveryError(f"cannot open {self.device}: {exc}") from exc
        self.fd = fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(fd, termios.TIOCEXCL)
            _configure_serial(fd, self.baud)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        except Exception:
            os.close(fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def exchange(self, request: bytes) -> tuple[bytes, bytes, bool]:
        if self.fd is None:
            raise RuntimeError("serial port is not open")
        termios.tcflush(self.fd, termios.TCIFLUSH)
        written = os.write(self.fd, request)
        if written != len(request):
            raise DiscoveryError(
                f"short serial write: sent {written} of {len(request)} bytes"
            )
        termios.tcdrain(self.fd)

        wire = bytearray()
        deadline = time.monotonic() + self.timeout
        expected_length: int | None = None
        echo_removed = False
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            readable, _, _ = select.select([self.fd], [], [], max(0.0, remaining))
            if not readable:
                break
            chunk = os.read(self.fd, 256)
            if not chunk:
                continue
            wire.extend(chunk)

            candidate = bytes(wire)
            if candidate.startswith(request):
                candidate = candidate[len(request) :]
                echo_removed = True
            if len(candidate) >= 3:
                expected_length = (
                    5
                    if candidate[1] & 0x80
                    else 5 + candidate[2]
                )
            if expected_length is not None and len(candidate) >= expected_length:
                return bytes(wire), candidate[:expected_length], echo_removed

        candidate = bytes(wire)
        if candidate.startswith(request):
            candidate = candidate[len(request) :]
            echo_removed = True
        if not candidate:
            raise DiscoveryError(
                f"no response within {self.timeout:.2f}s"
                + (" (the adapter echoed the request only)" if echo_removed else "")
            )
        return bytes(wire), candidate, echo_removed


def perform_group_read(
    serial_port: SerialRTU,
    slave: int,
    group: ReadGroup,
) -> dict[str, Any]:
    request = build_read_request(
        slave,
        group.function,
        group.pdu_start,
        group.count,
    )
    result: dict[str, Any] = {
        "name": group.name,
        "timestamp_utc": utc_now(),
        "function": f"0x{group.function:02x}",
        "reference_start": group.reference_start,
        "pdu_start": group.pdu_start,
        "count": group.count,
        "request_hex": request.hex(" "),
    }
    if group.note:
        result["note"] = group.note
    try:
        wire, response, echo_removed = serial_port.exchange(request)
        result["wire_hex"] = wire.hex(" ")
        result["response_hex"] = response.hex(" ")
        result["adapter_echo_removed"] = echo_removed
        registers = parse_read_response(request, response, group.count)
        result["registers"] = [
            {
                "reference": group.reference_start + index,
                "pdu_address": group.pdu_start + index,
                "hex": f"0x{value:04x}",
                "unsigned": value,
            }
            for index, value in enumerate(registers)
        ]
        result["decoded"] = decode_fields(group, registers)
        result["status"] = "ok"
    except DiscoveryError as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


SCAN_PROBES = (
    ReadGroup(
        "eastron_total_active_power_signature",
        0x04,
        30053,
        52,
        2,
        note=(
            "The exact Eastron total-active-power request recovered from the "
            "Ai dongle firmware."
        ),
    ),
    ReadGroup(
        "asw_device_header_signature",
        0x04,
        31001,
        1000,
        2,
        note=(
            "The confirmed ASW device-header request; attempted only when the "
            "Eastron signature receives no valid response."
        ),
    ),
)


def perform_slave_scan(
    serial_port: SerialRTU,
    start_slave: int,
    end_slave: int,
    request_gap: float,
    verbose: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for slave in range(start_slave, end_slave + 1):
        slave_result: dict[str, Any] = {
            "slave": slave,
            "status": "silent",
            "probes": [],
        }
        print(f"  slave {slave:3d}: ", end="", flush=True)
        for probe_index, group in enumerate(SCAN_PROBES):
            request = build_read_request(
                slave,
                group.function,
                group.pdu_start,
                group.count,
            )
            probe_result: dict[str, Any] = {
                "name": group.name,
                "timestamp_utc": utc_now(),
                "function": f"0x{group.function:02x}",
                "pdu_start": group.pdu_start,
                "count": group.count,
                "request_hex": request.hex(" "),
                "note": group.note,
            }
            try:
                wire, response, echo_removed = serial_port.exchange(request)
                probe_result["wire_hex"] = wire.hex(" ")
                probe_result["response_hex"] = response.hex(" ")
                probe_result["adapter_echo_removed"] = echo_removed
                classification = classify_scan_response(
                    request,
                    response,
                    group.count,
                )
                probe_result.update(classification)
                probe_result["status"] = "response"
                slave_result["status"] = "present"
                slave_result["matched_probe"] = group.name
                slave_result["response_kind"] = classification["kind"]
                if "exception_code" in classification:
                    slave_result["exception_code"] = classification[
                        "exception_code"
                    ]
                slave_result["probes"].append(probe_result)
                break
            except DiscoveryError as exc:
                probe_result["status"] = (
                    "no_response"
                    if str(exc).startswith("no response within")
                    else "invalid_response"
                )
                probe_result["error"] = str(exc)
                slave_result["probes"].append(probe_result)
                if probe_result["status"] == "invalid_response":
                    slave_result["status"] = "invalid_response"
                    break
            if probe_index + 1 < len(SCAN_PROBES):
                time.sleep(request_gap)

        if slave_result["status"] == "present":
            detail = slave_result["matched_probe"]
            if slave_result["response_kind"] == "exception":
                detail += f", exception {slave_result['exception_code']}"
            print(f"present ({detail})")
        elif slave_result["status"] == "invalid_response":
            print("invalid response")
        else:
            print("silent")
        if verbose:
            for probe in slave_result["probes"]:
                print(f"       {probe['name']}: {probe['status']}")
                print(f"         request: {probe['request_hex']}")
                if probe.get("response_hex"):
                    print(f"         response: {probe['response_hex']}")
                elif probe.get("error"):
                    print(f"         error: {probe['error']}")
        results.append(slave_result)
        if slave < end_slave:
            time.sleep(request_gap)
    return results


def _display_value(entry: dict[str, Any]) -> str:
    value = entry.get("value")
    unit = entry.get("unit", "")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value}{(' ' + unit) if unit else ''}"


def print_group_result(result: dict[str, Any], verbose: bool) -> None:
    if result["status"] == "error":
        print(f"  [error] {result['name']}: {result['error']}")
        if verbose and result.get("wire_hex"):
            print(f"          wire: {result['wire_hex']}")
        return
    print(f"  [ok] {result['name']}")
    for name, entry in result.get("decoded", {}).items():
        print(f"       {name}: {_display_value(entry)}")
    if verbose:
        print(f"       request:  {result['request_hex']}")
        print(f"       response: {result['response_hex']}")


def _available_ports() -> list[dict[str, str]]:
    candidates: list[str] = []
    for pattern in (
        "/dev/serial/by-id/*",
        "/dev/serial/by-path/*",
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
    ):
        candidates.extend(glob.glob(pattern))
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in sorted(candidates):
        resolved = os.path.realpath(candidate)
        key = (candidate, resolved)
        if key in seen:
            continue
        seen.add(key)
        result.append({"path": candidate, "resolves_to": resolved})
    return result


def command_list(_: argparse.Namespace) -> int:
    ports = _available_ports()
    if not ports:
        print("No USB serial devices found.")
        print("Expected paths include /dev/serial/by-id/* or /dev/ttyUSB*.")
        return 1
    print("Available serial devices:")
    for port in ports:
        if port["path"] == port["resolves_to"]:
            print(f"  {port['path']}")
        else:
            print(f"  {port['path']} -> {port['resolves_to']}")
    return 0


def selected_groups(profile: Profile, extended: bool) -> tuple[ReadGroup, ...]:
    return tuple(
        group for group in profile.groups if extended or not group.extended
    )


def _result_status(samples: Sequence[dict[str, Any]]) -> str:
    reads = [read for sample in samples for read in sample["reads"]]
    successes = sum(read["status"] == "ok" for read in reads)
    if successes == len(reads) and reads:
        return "ok"
    if successes:
        return "partial"
    return "failed"


def write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def command_probe(args: argparse.Namespace) -> int:
    profile = PROFILES[args.profile]
    baud = args.baud if args.baud is not None else profile.default_baud
    if baud not in BAUD_RATES:
        raise DiscoveryError(f"unsupported baud rate: {baud}")
    if args.samples < 1:
        raise DiscoveryError("--samples must be at least 1")
    if args.interval < 0:
        raise DiscoveryError("--interval cannot be negative")
    if args.timeout <= 0:
        raise DiscoveryError("--timeout must be greater than zero")

    groups = selected_groups(profile, args.extended)
    started_at = utc_now()
    result: dict[str, Any] = {
        "schema_version": 1,
        "tool": "solplanet-fasttalk-live-modbus-discovery",
        "tool_version": TOOL_VERSION,
        "started_at_utc": started_at,
        "profile": profile.name,
        "profile_description": profile.description,
        "serial": {
            "device": args.device,
            "resolved_device": os.path.realpath(args.device),
            "baud": baud,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
            "slave": profile.slave,
            "timeout_seconds": args.timeout,
        },
        "safety": {
            "permitted_function_codes": ["0x03", "0x04"],
            "write_capability_present": False,
            "inverter_serial_number_range_queried": False,
        },
        "extended_reads_enabled": args.extended,
        "samples": [],
    }

    print(
        f"Opening {args.device}: profile={profile.name}, slave={profile.slave}, "
        f"{baud}-8-N-1",
        flush=True,
    )
    try:
        with SerialRTU(args.device, baud, args.timeout) as serial_port:
            for sample_index in range(args.samples):
                sample: dict[str, Any] = {
                    "number": sample_index + 1,
                    "timestamp_utc": utc_now(),
                    "reads": [],
                }
                print(f"Sample {sample_index + 1}/{args.samples}")
                for group_index, group in enumerate(groups):
                    read_result = perform_group_read(
                        serial_port,
                        profile.slave,
                        group,
                    )
                    sample["reads"].append(read_result)
                    print_group_result(read_result, args.verbose)
                    if group_index == 0 and read_result["status"] == "error":
                        print(
                            "  Stopping: the profile's first validation read "
                            "did not succeed."
                        )
                        break
                    if group_index + 1 < len(groups):
                        time.sleep(profile.request_gap)
                result["samples"].append(sample)
                if (
                    sample["reads"]
                    and sample["reads"][0]["status"] == "error"
                ):
                    break
                if sample_index + 1 < args.samples:
                    time.sleep(args.interval)
    except (DiscoveryError, OSError) as exc:
        result["fatal_error"] = str(exc)
        print(f"[fatal] {exc}", file=sys.stderr)

    result["finished_at_utc"] = utc_now()
    result["status"] = _result_status(result["samples"])
    if "fatal_error" in result and result["status"] != "partial":
        result["status"] = "failed"

    if args.output:
        output = Path(args.output)
        write_json(output, result)
        print(f"Saved discovery output to {output}")
    else:
        print("No --output path supplied; results were not saved.")
    print(f"Discovery status: {result['status']}")
    return 0 if result["status"] in ("ok", "partial") else 2


def command_scan(args: argparse.Namespace) -> int:
    if args.baud not in BAUD_RATES:
        raise DiscoveryError(f"unsupported baud rate: {args.baud}")
    if not 1 <= args.start_slave <= 247:
        raise DiscoveryError("--start-slave must be between 1 and 247")
    if not 1 <= args.end_slave <= 247:
        raise DiscoveryError("--end-slave must be between 1 and 247")
    if args.start_slave > args.end_slave:
        raise DiscoveryError("--start-slave cannot exceed --end-slave")
    if args.timeout <= 0:
        raise DiscoveryError("--timeout must be greater than zero")

    result: dict[str, Any] = {
        "schema_version": 1,
        "tool": "solplanet-fasttalk-live-modbus-discovery",
        "tool_version": TOOL_VERSION,
        "started_at_utc": utc_now(),
        "operation": "read_only_slave_scan",
        "serial": {
            "device": args.device,
            "resolved_device": os.path.realpath(args.device),
            "baud": args.baud,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
            "timeout_seconds": args.timeout,
        },
        "range": {
            "start_slave": args.start_slave,
            "end_slave": args.end_slave,
        },
        "safety": {
            "permitted_function_codes": ["0x04"],
            "write_capability_present": False,
            "fixed_probe_addresses": [52, 1000],
            "registers_per_request": 2,
        },
        "slaves": [],
    }

    print(
        f"Opening {args.device}: read-only slave scan "
        f"{args.start_slave}-{args.end_slave}, {args.baud}-8-N-1",
        flush=True,
    )
    try:
        with SerialRTU(args.device, args.baud, args.timeout) as serial_port:
            result["slaves"] = perform_slave_scan(
                serial_port,
                args.start_slave,
                args.end_slave,
                request_gap=0.05,
                verbose=args.verbose,
            )
    except (DiscoveryError, OSError) as exc:
        result["fatal_error"] = str(exc)
        print(f"[fatal] {exc}", file=sys.stderr)

    result["finished_at_utc"] = utc_now()
    present = [
        entry["slave"]
        for entry in result["slaves"]
        if entry["status"] == "present"
    ]
    invalid = [
        entry["slave"]
        for entry in result["slaves"]
        if entry["status"] == "invalid_response"
    ]
    result["present_slaves"] = present
    result["invalid_response_slaves"] = invalid
    result["status"] = "failed" if "fatal_error" in result else "ok"

    if args.output:
        output = Path(args.output)
        write_json(output, result)
        print(f"Saved discovery output to {output}")
    else:
        print("No --output path supplied; results were not saved.")
    print(
        "Valid responding slave addresses: "
        + (", ".join(str(slave) for slave in present) if present else "none")
    )
    if invalid:
        print(
            "Addresses with invalid/corrupt responses: "
            + ", ".join(str(slave) for slave in invalid)
        )
    return 0 if result["status"] == "ok" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded, read-only Modbus RTU discovery for the "
            "solplanet-fasttalk test plant."
        )
    )
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser(
        "list",
        help="list likely USB serial devices",
    )
    list_parser.set_defaults(handler=command_list)

    probe_parser = commands.add_parser(
        "probe",
        help="run a predefined read-only device profile",
    )
    probe_parser.add_argument("profile", choices=sorted(PROFILES))
    probe_parser.add_argument(
        "--device",
        required=True,
        help="serial device, preferably a /dev/serial/by-id path",
    )
    probe_parser.add_argument(
        "--baud",
        type=int,
        help="override the profile baud rate",
    )
    probe_parser.add_argument(
        "--timeout",
        type=float,
        default=1.5,
        help="response timeout in seconds (default: 1.5)",
    )
    probe_parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="number of complete samples (default: 1)",
    )
    probe_parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between complete samples (default: 5)",
    )
    probe_parser.add_argument(
        "--extended",
        action="store_true",
        help="include explicitly marked experimental read-only groups",
    )
    probe_parser.add_argument(
        "--output",
        help="write structured JSON results to this path",
    )
    probe_parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print raw request and response frames",
    )
    probe_parser.set_defaults(handler=command_probe)

    scan_parser = commands.add_parser(
        "scan",
        help="scan a bounded slave range using two fixed read-only signatures",
    )
    scan_parser.add_argument(
        "--device",
        required=True,
        help="serial device, preferably a /dev/serial/by-id path",
    )
    scan_parser.add_argument(
        "--baud",
        type=int,
        default=9600,
        help="serial baud rate (default: 9600)",
    )
    scan_parser.add_argument(
        "--start-slave",
        type=int,
        default=1,
        help="first Modbus slave address (default: 1)",
    )
    scan_parser.add_argument(
        "--end-slave",
        type=int,
        default=16,
        help="last Modbus slave address, inclusive (default: 16)",
    )
    scan_parser.add_argument(
        "--timeout",
        type=float,
        default=0.35,
        help="response timeout per fixed probe in seconds (default: 0.35)",
    )
    scan_parser.add_argument(
        "--output",
        help="write structured JSON results to this path",
    )
    scan_parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print raw request and response frames",
    )
    scan_parser.set_defaults(handler=command_scan)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except DiscoveryError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
