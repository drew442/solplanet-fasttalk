"""Machine-readable Modbus safety policy.

This module classifies proposed operations. It deliberately does not construct,
approve, or transmit Modbus writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SafetyDisposition(str, Enum):
    READ_ONLY_ALLOWED = "read_only_allowed"
    APPROVAL_REQUIRED = "approval_required"
    PERMANENTLY_PROHIBITED = "permanently_prohibited"
    UNREVIEWED_DENY = "unreviewed_deny"


@dataclass(frozen=True)
class RegisterRule:
    start: int
    end: int
    summary: str

    def contains(self, address: int) -> bool:
        return self.start <= address <= self.end


@dataclass(frozen=True)
class SafetyAssessment:
    disposition: SafetyDisposition
    bus: str
    function: int
    documented_address: int | None
    count: int
    reasons: tuple[str, ...]

    @property
    def may_transmit_now(self) -> bool:
        """No write is immediately transmissible under the current policy."""

        return self.disposition is SafetyDisposition.READ_ONLY_ALLOWED


READ_FUNCTIONS = frozenset((0x03, 0x04))
REVIEWABLE_WRITE_FUNCTIONS = frozenset((0x05, 0x06, 0x10))
BROADCAST_SLAVES = frozenset((0x00, 0xFF))


ASW_PERMANENTLY_PROHIBITED = (
    RegisterRule(
        41103,
        41103,
        "storage-machine topology/type selection, including forced grid charge",
    ),
    RegisterRule(
        41105,
        41105,
        "battery manufacturer/BMS protocol selection",
    ),
    RegisterRule(
        41108,
        41108,
        "smart-meter online state is an inverter-owned safety/control input",
    ),
    RegisterRule(
        41112,
        41113,
        "smart-meter measured power must never be spoofed",
    ),
    RegisterRule(
        41116,
        41116,
        "EPS/UPS supply-mode selection can change which circuits remain energised",
    ),
    RegisterRule(
        44004,
        44005,
        "voltage/frequency grid-response enable",
    ),
    RegisterRule(
        44007,
        44010,
        "ride-through, overvoltage, and anti-islanding protection enable",
    ),
    RegisterRule(
        44012,
        44012,
        "protective-earth connection check enable",
    ),
    RegisterRule(
        44014,
        44015,
        "AFCI and PV-string fault monitoring enable",
    ),
    RegisterRule(
        44017,
        44017,
        "overload protection enable",
    ),
    RegisterRule(
        44019,
        44021,
        "SPD detection and voltage/frequency grid-response enable",
    ),
    RegisterRule(
        44023,
        44024,
        "primary-frequency and communications-loss protection enable",
    ),
    RegisterRule(
        44026,
        44027,
        "external-control input or alternate SunSpec write-surface enable",
    ),
    RegisterRule(
        45201,
        45255,
        "grid code and voltage/frequency/ROCOF/insulation/DC-injection protection",
    ),
    RegisterRule(
        45408,
        45452,
        "voltage/frequency grid-response curves and DRM response",
    ),
    RegisterRule(
        45601,
        45619,
        "low/high-voltage ride-through and fault-current behaviour",
    ),
    RegisterRule(
        46401,
        46451,
        "CT/meter observations must never be overwritten or spoofed",
    ),
    RegisterRule(
        46521,
        46523,
        "AFCI sensitivity, reconnection mode, and manual fault reset",
    ),
)


ASW_APPROVAL_REQUIRED = (
    RegisterRule(40201, 40201, "remote inverter power on/off"),
    RegisterRule(41001, 41006, "inverter real-time clock"),
    RegisterRule(41102, 41102, "storage inverter on/off"),
    RegisterRule(41104, 41104, "storage operating mode"),
    RegisterRule(
        41109,
        41111,
        "smart-meter adjustment and import/export target power",
    ),
    RegisterRule(41114, 41114, "anti-reverse/export-control enable"),
    RegisterRule(41115, 41115, "battery wake-up/forced-charge trigger"),
    RegisterRule(
        41152,
        41156,
        "charge/discharge state, power, SOC bounds, and grid-power ratio",
    ),
    RegisterRule(44001, 44001, "active-power-control enable"),
    RegisterRule(44003, 44003, "active-power slope/load function"),
    RegisterRule(44006, 44006, "reactive-power-control enable"),
    RegisterRule(44025, 44025, "shadow MPPT enable"),
    RegisterRule(
        45401,
        45405,
        "grid-connection power ramps and active-power limit",
    ),
    RegisterRule(
        45501,
        45527,
        "reactive-power, power-factor, and Q(U) grid-support control",
    ),
)


SOLIS_PERMANENTLY_PROHIBITED = (
    RegisterRule(3009, 3009, "grid/power-curve selection"),
    RegisterRule(3011, 3027, "grid protection and demand-response configuration"),
    RegisterRule(3055, 3055, "DSP firmware-upgrade selection"),
    RegisterRule(
        3068,
        3069,
        "grid-standard selection and limited-endurance flash persistence",
    ),
    RegisterRule(3074, 3076, "live grid-protection test controls"),
    RegisterRule(3077, 3077, "AFCI enable"),
    RegisterRule(3080, 3080, "persistent over-rated maximum-power flag"),
    RegisterRule(3084, 3085, "leakage-current and PV-insulation protection thresholds"),
    RegisterRule(3089, 3151, "grid code, protection thresholds, response curves, and EPM mode"),
    RegisterRule(
        3304,
        3304,
        "special factory/test controls including DC-injection adjustment disable",
    ),
)


SOLIS_APPROVAL_REQUIRED = (
    RegisterRule(3000, 3005, "inverter real-time clock"),
    RegisterRule(3007, 3007, "inverter on/off"),
    RegisterRule(3051, 3052, "reactive- or active-power limitation"),
    RegisterRule(3070, 3070, "active-power-limitation enable"),
    RegisterRule(3071, 3071, "reactive-power-control enable"),
    RegisterRule(3073, 3073, "active/reactive grid working mode"),
    RegisterRule(3081, 3081, "active-power limit value"),
    RegisterRule(3083, 3083, "reactive-power limit value"),
    RegisterRule(5000, 5000, "grid on/off coil"),
)


def _matching_rules(
    rules: tuple[RegisterRule, ...],
    address: int,
    count: int,
) -> tuple[RegisterRule, ...]:
    matched: list[RegisterRule] = []
    for current in range(address, address + count):
        for rule in rules:
            if rule.contains(current) and rule not in matched:
                matched.append(rule)
    return tuple(matched)


def _all_addresses_covered(
    rules: tuple[RegisterRule, ...],
    address: int,
    count: int,
) -> bool:
    return all(
        any(rule.contains(current) for rule in rules)
        for current in range(address, address + count)
    )


def assess_modbus_command(
    *,
    bus: str,
    slave: int,
    function: int,
    documented_address: int | None = None,
    count: int = 1,
) -> SafetyAssessment:
    """Classify a proposed Modbus operation without constructing a frame.

    Addresses use the decimal address printed in the relevant vendor table,
    not the zero-based PDU address. This prevents policy checks from silently
    using the wrong 3x/4x offset convention.
    """

    if count <= 0:
        return SafetyAssessment(
            SafetyDisposition.UNREVIEWED_DENY,
            bus,
            function,
            documented_address,
            count,
            ("register count must be positive",),
        )

    if bus == "eastron_terminal8":
        return SafetyAssessment(
            SafetyDisposition.PERMANENTLY_PROHIBITED,
            bus,
            function,
            documented_address,
            count,
            ("the terminal-8 integration is physically and logically receive-only",),
        )

    if bus not in ("asw_monitor", "solis_rs485"):
        return SafetyAssessment(
            SafetyDisposition.UNREVIEWED_DENY,
            bus,
            function,
            documented_address,
            count,
            ("unknown bus or device family",),
        )

    if function in READ_FUNCTIONS:
        return SafetyAssessment(
            SafetyDisposition.READ_ONLY_ALLOWED,
            bus,
            function,
            documented_address,
            count,
            ("bounded Modbus read",),
        )

    if slave in BROADCAST_SLAVES:
        return SafetyAssessment(
            SafetyDisposition.PERMANENTLY_PROHIBITED,
            bus,
            function,
            documented_address,
            count,
            ("broadcast writes have no per-device response or read-after-write verification",),
        )

    if function not in REVIEWABLE_WRITE_FUNCTIONS:
        return SafetyAssessment(
            SafetyDisposition.PERMANENTLY_PROHIBITED,
            bus,
            function,
            documented_address,
            count,
            (
                "only explicitly reviewed 0x05, single-register 0x06, or "
                "multi-register 0x10 writes can enter the approval workflow",
            ),
        )

    if documented_address is None:
        return SafetyAssessment(
            SafetyDisposition.UNREVIEWED_DENY,
            bus,
            function,
            documented_address,
            count,
            ("a vendor-documented decimal register address is required",),
        )

    if not 0 <= documented_address <= 65535:
        return SafetyAssessment(
            SafetyDisposition.UNREVIEWED_DENY,
            bus,
            function,
            documented_address,
            count,
            ("documented register address is outside the policy domain",),
        )

    prohibited = (
        ASW_PERMANENTLY_PROHIBITED
        if bus == "asw_monitor"
        else SOLIS_PERMANENTLY_PROHIBITED
    )
    controlled = (
        ASW_APPROVAL_REQUIRED
        if bus == "asw_monitor"
        else SOLIS_APPROVAL_REQUIRED
    )
    prohibited_matches = _matching_rules(prohibited, documented_address, count)
    if prohibited_matches:
        return SafetyAssessment(
            SafetyDisposition.PERMANENTLY_PROHIBITED,
            bus,
            function,
            documented_address,
            count,
            tuple(rule.summary for rule in prohibited_matches),
        )
    if function == 0x05 and not (
        bus == "solis_rs485"
        and documented_address == 5000
        and count == 1
    ):
        return SafetyAssessment(
            SafetyDisposition.PERMANENTLY_PROHIBITED,
            bus,
            function,
            documented_address,
            count,
            ("function 0x05 is reviewable only for the documented Solis grid on/off coil",),
        )
    if _all_addresses_covered(controlled, documented_address, count):
        controlled_matches = _matching_rules(controlled, documented_address, count)
        return SafetyAssessment(
            SafetyDisposition.APPROVAL_REQUIRED,
            bus,
            function,
            documented_address,
            count,
            tuple(rule.summary for rule in controlled_matches),
        )
    return SafetyAssessment(
        SafetyDisposition.UNREVIEWED_DENY,
        bus,
        function,
        documented_address,
        count,
        ("write is not present in the reviewed approval-required register set",),
    )
