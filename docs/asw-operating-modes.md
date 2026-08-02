# ASW battery operating modes

This document records the operating semantics used by the read-only shadow
planner for the ASW12kH-T3 and three parallel seven-module Ai-HB G2 stacks. It
does not authorize a Modbus write or make control available.

## Evidence

The following manufacturer evidence applies:

- `reference/UM0035_ASW05-12KH-T2-T3_EN_V05_0225.pdf`, section 4.7, describes
  self-consumption as prioritising local load from PV and battery and describes
  Custom mode as user-set charging/discharging periods and powers.
- `reference/UM0057_ASW05-12KH-T2-T3-DG_EN_V02_0425.pdf`, section 4.7, states
  explicitly that Custom mode operates in self-consumption outside schedules.
- The older ASW8–12kH-T1 manual gives the detailed balance cases that the
  current T2/T3 manual says will be documented in a future revision: during a
  fixed charge period the grid can supply charging and load together; during
  a fixed discharge period below load, battery and grid supply the load
  together. This is corroboration from a related family, not proof of the
  exact T3 command scale.
- `reference/MB001_ASW.GEN-Modbus-en_V2.1.4.pdf` identifies holding register
  41104 values 2/3/4 as self-use, backup/reserve and Customer defined; 41152 as
  stop/charge/discharge direction; 41153 as the signed battery power command;
  and 41154/41155 as upper/lower SOC limits.
- The ASW manual and system datasheet rate ASW12kH-T3 battery charge and
  discharge at 12 kW and 30 A. Live BMS voltage×current limits can only reduce
  those ceilings. The unrelated 24 kVA, 10-second EPS overload figure is not a
  battery scheduling limit.

The current official Solplanet manual is also available from the
[ASW H-T2/T3 product documentation](https://solplanet.net/wp-content/uploads/2023/12/Manuel-Utilisateur-ASW-H-T2-T3-5-12kW-EN.pdf).

Live-system evidence supplied by the plant owner establishes the fixed-window
balance rule which the high-level manual does not state numerically:

- outside a charge/discharge window, Custom mode changes battery output to
  follow site consumption;
- inside a discharge window, the configured power is battery AC output, not a
  requested net grid export; for example, a 1 kW command with 1.5 kW site load
  leaves 0.5 kW grid import; and
- consequently, producing a desired net export requires a fixed discharge
  command based on forecast site load and PV. The inverter does not add load to
  that command automatically.

The analogous fixed charge-window model commands battery input power. Site load
and PV remain separate terms in the grid balance. This is consistent with the
manufacturer's related-family description that the grid supplies loads while
charging, but it remains an explicit assumption that must be verified
quantitatively on this T3 firmware before any real control trial.

## Planner semantics

The shadow planner uses these modes:

| Recommendation | ASW configuration | Modelled grid balance |
| --- | --- | --- |
| `self_consumption` | Custom mode, no active window | ASW automatically absorbs PV surplus or discharges only enough to serve load, subject to SOC and power limits |
| `grid_charge` | Custom fixed charge window | `grid = load - PV + battery charge command` |
| `export_discharge` | Custom fixed discharge window | `grid = load - PV - battery discharge command` |

Custom mode without a window is preferred for every interval unless the
horizon-wide optimiser finds that importing energy into the battery or
exporting battery energy is economically beneficial. A fixed discharge window
is never used merely to serve site load; native self-consumption follows load
more accurately and avoids forecast-dependent residual import/export.

The proposed reserve/maximum SOC bounds remain part of the planned Custom
configuration. If they differ from the inverter's current native bounds, future
control would require separately approved writes to 41154/41155; a no-window
recommendation does not hide that configuration dependency.

The planner constructs every interval in the configured horizon. Missing
nighttime Forecast.Solar points become explicit zero-PV slots, so load, grid
flow, tariff cost and SOC continue across sunset and sunrise. A missing point
inside a provider day's daylight bounds is a quality fault, not assumed night.

Recurring native Custom windows are part of the no-daemon-change baseline.
The documented read map exposes the direction/power of a window only while it
is active, not its future start/end times. Every native window must therefore
be declared in the untracked private runtime configuration and explicitly
confirmed:

```toml
[optimisation]
native_schedule_confirmed = true

[[optimisation.native_schedule]]
mode = "charge"
starts_at = "PRIVATE_LOCAL_START"
ends_at = "PRIVATE_LOCAL_END"
power_watts = 12000
```

Times are recurring local wall-clock times in the tariff timezone; the end is
exclusive and overnight windows are supported. Windows must not overlap. An
explicitly confirmed empty list means the inverter has no native windows. An
unconfirmed Custom schedule prevents the planning-quality gate from passing.
On the live firmware, 41104 can report effective self-consumption (`2`) outside
an owner-configured Custom window and Customer defined (`4`) while the window
is active. A confirmed recurring schedule therefore defines future baseline
policy; 41104 remains evidence of the current effective state.

The schedule search compares whole-horizon alternatives rather than making an
independent greedy decision in each slot. It includes charge/discharge
efficiency, reserve and maximum SOC, site limits, the tariff's Super Export cap,
and a minimum intervention margin. The optimized trajectory must end with at
least the energy of the no-change self-consumption baseline. If no fixed-window
trajectory reduces cost, the recommendation remains self-consumption with no
window.

## Readback and future control boundary

The daemon now reads 41102–41105 as a bounded holding-register read. Register
41104 supplies the current high-level run mode. Registers 41152/41153 validate
the expected direction and power when a configured native window is active,
but are not projected across the future: an instantaneous charge/discharge
direction is not proof that the same fixed window persists for 36 hours.

No schedule registers have been confirmed for this exact firmware. No write
transport exists in the optimiser, `execution_available` remains false and all
ASW mode/window/SOC writes remain approval-required under
[Modbus write safety](modbus-write-safety.md). Before control, the exact window
representation, duration, persistence, write grouping, readback, recovery and
EEPROM wear characteristics require a separately approved test programme.
