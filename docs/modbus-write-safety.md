# Modbus write safety policy

This policy protects people and the high-voltage/high-current PV, inverter and
battery equipment connected to `solplanet-fasttalk`. It distinguishes commands
that this project must never transmit from legitimate plant controls that may
be developed only behind an informed approval gate.

The policy is machine-readable in
[`modbus_safety.py`](../src/solplanet_fasttalk/modbus_safety.py). That module
classifies proposed operations but deliberately cannot build, approve or
transmit a write. The current daemon still contains no Modbus write builder or
control endpoint.

## Evidence and scope

The initial classification uses:

- `reference/MB001_ASW.GEN-Modbus-en_V2.1.4.pdf`;
- `reference/MB001_ASW GEN-Modbus-en_V2.1.3.pdf`;
- `reference/MB001_ASW GEN-Modbus-en_V2.1.1.pdf`;
- `reference/UM0035_ASW05-12KH-T2-T3_EN_V05_0225.pdf`;
- `reference/UM0037_Ai-HB-075-200A_EN_V03_0625.pdf`; and
- `reference/RS485_MODBUS Communication Protocol_Solis Inverters.pdf`.

The Solplanet manuals identify lethal high-voltage, battery short-circuit,
electric-arc, backup-circuit, grounding and grid-protection hazards. The
official inverter manual also states that an invalid country grid code can
disturb the PV system and grid, and describes AFCI, anti-islanding, insulation,
protective-earth and DC-injection protections. The battery manual identifies
the Ai-HB G2 as a high-voltage lithium-ion BESS and warns of electric shock,
fire and explosion hazards.

The ASW Modbus document labels many safety and measured-value registers `RW`.
That label means only that the device protocol may accept a write. It does not
make the write appropriate or safe for this daemon.

The supplied ASW file is named version 2.1.4 while its page title says 2.1.3
and its revision note describes the addition of CT data. The live ASW firmware
also differs from this map in confirmed read-only fields. No write is treated
as valid for the test inverter merely because it appears in this document.

Addresses below are the decimal addresses printed in the vendor tables. They
are not zero-based PDU addresses. For an ASW 4x address:

```text
PDU address = documented address - 40001

documented 41153 -> PDU 1152
```

Every future risk assessment must show both forms to prevent an off-by-one or
wrong-address-family write.

## Policy outcomes

Every proposed command has exactly one outcome:

| Outcome | Meaning |
| --- | --- |
| Read-only allowed | A bounded `0x03` or `0x04` read on an approved active bus |
| Approval required | A documented plant control; no frame may be built or sent until its risk assessment is approved |
| Permanently prohibited | The project must never transmit it; per-operation approval cannot override this |
| Unreviewed deny | Not enough evidence exists to classify it; no transmission is allowed |

Reclassifying a permanently prohibited operation requires an explicit policy
change requested by the owner, new authoritative evidence, code review and
tests. It cannot be reclassified by approving a live test.

## Permanent blacklist

### All devices and buses

The following are permanently prohibited:

| Operation | Reason |
| --- | --- |
| Any transmission on the ASW–Eastron terminal-8 bus | A second master can collide with the inverter's export-control meter loop; the SH-U11F connection is physically receive-only |
| Any Modbus broadcast write, including ASW `0x10` broadcast and slave `0x00`/`0xFF` write forms | It cannot provide a per-device response or reliable read-after-write verification and may change multiple devices |
| Raw/arbitrary frames or a write path that bypasses the safety classifier | Address, function, range, approval and audit enforcement would be bypassed |
| Undocumented write functions, mask writes, combined read/write operations, diagnostics/restart functions and file/firmware functions | Device-specific effects are unknown and can include reset, reconfiguration or firmware corruption |
| Writes through the Ai dongle, vendor CGI, vendor cloud or another gateway | They bypass direct-bus validation, exact frame auditing and deterministic readback |
| Direct BMS or battery-stack writes | No verified Ai-HB G2 write protocol, per-stack coordination rule or safe control requirement exists; battery operation must be commanded through verified ASW controls |

Only `0x06` and `0x10` can enter the register approval workflow for ASW. Solis
also documents an `0x05` grid on/off coil; only that exact operation can enter
the Solis approval workflow. Other `0x05` operations remain prohibited.

### Solplanet ASW MONITOR

| Documented address | Permanent reason |
| --- | --- |
| `41103` | Selects storage-machine topology/type and includes a forced-grid-charge mode; a wrong topology can change power-stage and battery behaviour |
| `41105` | Selects the battery manufacturer/BMS protocol; a mismatch can invalidate battery limits and protection communication |
| `41108`, `41112–41113` | Represent smart-meter online state and measured power; spoofing the export-control input can drive unintended full-power import/export or battery response |
| `41116` | Changes EPS/UPS behaviour and therefore which backup circuits may remain energised; incorrect assumptions can expose a person to live conductors |
| `44004–44005` | Enables voltage/frequency power-response behaviour that belongs to the commissioned grid code |
| `44007–44010` | Changes LVRT, HVRT, overvoltage and anti-islanding protections |
| `44012` | Changes protective-earth connection checking |
| `44014–44015` | Changes AFCI and PV-string fault monitoring |
| `44017` | Changes overload protection |
| `44019–44021` | Changes SPD detection and voltage/frequency grid-response protection |
| `44023–44024` | Changes primary-frequency and communications-loss safety behaviour |
| `44026–44027` | Enables external or SunSpec write surfaces that can bypass the daemon's control boundary |
| `45201–45255` | Changes grid code, voltage/frequency/ROCOF trip and recovery values, connection timing, insulation threshold, DC-injection threshold or DC-injection time |
| `45408–45452` | Changes commissioned voltage/frequency/DRM grid-response curves |
| `45601–45619` | Changes LVRT/HVRT fault detection, reactive-current injection and active-power priority during grid faults |
| `46401–46451` | Represents CT/meter measurements and counters; writes would spoof authoritative plant and export-control data |
| `46521–46523` | Changes AFCI sensitivity/reconnection or clears an arc fault; remote reset can re-energise a real DC arc/fire fault before inspection |

These prohibitions include disabling a protection and changing its threshold.
Writing back a value that appears to be the current value is also prohibited:
firmware interpretation, scaling and model applicability have not been proven.

### Solis RS-485

The Solis protocol is a broad multi-model document and has not been validated
as a write map for the live Solis-10K. The following documented writes are
permanently prohibited even if the live inverter accepts them:

| Documented address | Permanent reason |
| --- | --- |
| `3009` | Selects a grid/power curve |
| `3011–3027` | Changes grid protection and demand-response behaviour |
| `3055` | Selects DSP firmware upgrade |
| `3068` | Changes the commissioned grid standard |
| `3069` | Enables persistent flash writes; the document explicitly warns that flash write life is limited |
| `3074–3076` | Starts live grid-protection tests |
| `3077` | Changes AFCI protection |
| `3080` | Persists a maximum-power flag allowing operation above rated power |
| `3084–3085` | Changes leakage-current and PV-insulation protection thresholds |
| `3089–3151` | Changes grid-code accuracy, trip/recovery thresholds, response curves, ramps or EPM mode |
| `3304` | Exposes factory/test controls including disabling DC-injection adjustment |

The current daemon does not require any Solis write for authoritative plant
accounting. All other Solis writes remain unreviewed-deny unless specifically
added to the controlled list through policy review.

## Approval-required controls

Presence in this list is not approval and does not mean the live firmware
supports the command. It means only that the operation has a plausible daemon
use and can be proposed after simulation, read-only validation and a complete
risk assessment.

### Solplanet ASW controls

| Address | Intended use | Principal hazards |
| --- | --- | --- |
| `40201` | Remote inverter power off/on | Loss of production or backup; power-on can energise AC, DC and backup circuits |
| `41001–41006` | Set inverter RTC | Incorrect schedules, tariff periods, daily counters and audit times |
| `41102` | Storage inverter off/on | Abrupt loss or restoration of battery power and backup capability |
| `41104` | Select storage run mode | Immediate change to self-consumption, backup or custom charge/discharge behaviour |
| `41109–41111` | Meter adjustment and target power | Can drive import/export or battery power toward a large target |
| `41114` | Anti-reverse/export-control enable | Disabling or mis-setting it can violate the site export limit |
| `41115` | Battery wake-up/forced charge | Energises a sleeping battery and may initiate high charging current |
| `41152` | Stop/charge/discharge selection | Direction change can create a large and immediate site-power step |
| `41153` | Signed charge/discharge power command | Wrong sign, scale or range can request maximum charging or discharging |
| `41154–41155` | Charge upper and discharge lower SOC limits | Wrong limits can remove backup reserve or request operation near battery limits |
| `41156` | Grid-power ratio | Can alter import/export response and control-loop stability |
| `44001`, `44003` | Active-power and slope control enable | Can curtail, ramp or release inverter output unexpectedly |
| `44006`, `45501–45527` | Reactive power/power-factor control | Can increase AC current, affect voltage and conflict with grid requirements |
| `44025` | Shadow MPPT | Can change PV operating point and production; not needed on the PV-less test ASW |
| `45401–45405` | Connection/reconnection ramps and active-power limit | Can curtail or rapidly restore inverter power and affect grid import/export |

The likely minimum ESS control surface is `41152–41155`, possibly combined
with `41104`. The target register, persistence, valid range, sign, atomicity and
failure behaviour must still be proven for the exact ASW model and firmware.

The intended control strategy does not use a fixed window to follow ordinary
site load. Custom mode with no active schedule is preferred because the ASW
then performs native self-consumption and dynamically matches the load. A
fixed-power charge window is reserved for deliberate grid charging; a
fixed-power discharge window is reserved for deliberate grid export. The fixed
command is battery power, not net grid power, so site load and PV forecast error
directly affect resulting import/export. See
[ASW battery operating modes](asw-operating-modes.md).

### Solis controls

| Address/function | Intended use | Principal hazards |
| --- | --- | --- |
| `3000–3005`, `0x06`/`0x10` | Set inverter RTC | Incorrect schedules, daily counters and audit correlation |
| `3007`, `0x06` | Inverter off/on | Loss or restoration of external PV; power-on energises inverter power stages |
| `5000`, `0x05` | Documented grid on/off coil for some model families | Model applicability is unproven; can disconnect or reconnect PV generation |
| `3051–3052`, `0x06`/`0x10` | Reactive/active power limitation | Unexpected curtailment, current, voltage or power-factor response |
| `3070–3071`, `0x06` | Enable active/reactive limitation | Can release or apply an existing limit immediately |
| `3073`, `0x06` | Select active/reactive working mode | Can apply an existing curve or setpoint immediately |
| `3081`, `0x06` | Active-power limit value | Wrong scale or sign can fully curtail or incorrectly release power |
| `3083`, `0x06` | Reactive-power limit value | Wrong scale or sign can request excessive reactive current |

Direct Solis control is not required by the current daemon plan. These entries
reserve a safe review route if PV curtailment or device control is deliberately
added later.

## Unreviewed default deny

A write not explicitly present in either list is denied. This includes
reserved addresses, status fields, passwords, calibration counters, device
addresses, undocumented ranges and commands discovered only through reverse
engineering.

Unreviewed deny is intentionally not an approval queue. Before an operation
can be proposed, its exact semantics, model/firmware applicability, units,
range, persistence, atomicity, failure behaviour and readback must be
documented and the machine-readable policy must be updated and tested.

## Required risk assessment

Every physical write sequence, including an apparently harmless RTC update,
requires a written assessment containing:

1. **Approval ID and expiry** — unique identifier, exact proposed window and
   whether it is one-shot or a separately reviewed bounded control envelope.
2. **Target identity** — device family, exact model, firmware versions, bus,
   slave and approved by-ID adapter. Private serial numbers are not recorded.
3. **Exact command** — function, documented address, zero-based PDU address,
   count, engineering value, scaled register words and final request bytes.
4. **Evidence** — vendor reference, confirmed applicability, current register
   value, observed valid ranges and any firmware deviations.
5. **Purpose and necessity** — why monitoring, simulation or a safer existing
   control cannot achieve the result.
6. **Expected physical effect** — direction and maximum change in grid, PV,
   inverter and battery power; expected SOC/current/voltage impact; circuits
   that may become energised or de-energised.
7. **Worst credible outcome** — wrong sign/scale/address, partial multi-write,
   lost response, daemon crash, stale meter/SOC, device reboot, command
   persistence, oscillation and loss of communications.
8. **Hard interlocks** — fresh authoritative grid power and SOC, ASW/BMS
   healthy state, no active relevant fault, temperature within operating
   limits, live BMS limits, conservative site caps, rate/duration limits and
   one serial owner.
9. **Human/site conditions** — confirmation that nobody is installing,
   servicing or touching electrical equipment, no conductor is assumed dead,
   backup loads can tolerate the test, and the owner is available for the
   supervised test.
10. **Execution sequence** — pre-read, single bounded write, immediate
    readback, telemetry observation period and prohibition on retries after an
    ambiguous result.
11. **Rollback and safe stop** — exact rollback value and frame, whether
    rollback itself needs the same approval, and the independent manual method
    for returning the plant to native operation.
12. **Abort thresholds** — communications errors, meter offline, unexpected
    readback, current/power/SOC deviation, temperature, fault/warning, native
    app disagreement or any person entering the electrical work area.
13. **Audit artefacts** — approved assessment, UTC timestamps, before/after
    reads, request/response bytes, measured result and final state, with
    private data excluded.

Command limits use the most conservative of the current BMS limit, inverter
limit, configured site limit and owner-approved test cap. A documentation
maximum is never used as the first live test value. Charge/discharge polarity
must be independently confirmed, not inferred from a register description.

For the three parallel Ai-HB G2 stacks, the daemon treats ASW-reported limits
as aggregate unless authoritative documentation proves otherwise. It must not
divide or multiply a limit based only on the number of stacks.

## Approval protocol

The assistant presents the completed assessment before constructing or sending
a frame and asks the owner to approve or deny the exact Approval ID.

Approval must identify the proposed operation or bounded sequence. Silence,
general development permission, root access, an earlier approval, or phrases
such as “continue development” do not approve a write. A denial sends no frame.

An approval expires when its stated time window ends or when any of these
changes:

- device model or firmware;
- register value, command value, function, address or scale;
- connection, slave, adapter or bus ownership;
- BMS/inverter/meter health or relevant plant state;
- safety interlock, execution order or rollback plan; or
- code revision implementing the operation.

Until automatic control receives a separately approved, versioned safety
envelope, every live write sequence requires individual approval. A future
automatic-control approval may cover only the documented command types and
hard bounds in that exact envelope; it cannot include permanently prohibited
operations or grant a generic raw-write capability.

## Live-test progression

An approval-required command progresses through:

1. unit tests of scaling, bounds and policy classification;
2. saved-capture/replay tests;
3. optimiser shadow mode with no writer linked;
4. a simulated Modbus device including timeout and bad-readback cases;
5. owner review of the complete risk assessment;
6. one supervised, low-magnitude, short-duration command;
7. immediate readback and independent Eastron verification;
8. a stop/rollback test; and
9. only then consideration of a larger bounded envelope.

No control test is performed while anyone may rely on an inverter, PV,
battery, grid or EPS conductor being de-energised.
