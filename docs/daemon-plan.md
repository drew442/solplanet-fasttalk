# Daemon implementation plan

This plan turns the completed live-system discovery into a production-oriented
Linux daemon. It prioritises a read-only, observable vertical slice before any
control is enabled.

> [!NOTE]
> Phases 0–6 are implemented. The first combined phase-0–3 live run completed
> without disrupting the ASW, meters or native Solplanet app. The subsequent
> phase-4–6 canary validated direct Solis diagnostics, the two-plane online
> forecast and a constrained shadow plan. All phase-6 output is shadow-only;
> phase 7 has not begun.

Implementation details and operator-facing behaviour are documented in
[Phases 3–6 implementation](phases-3-to-6.md). The implemented
[diagnostics web UI](diagnostics-webui.md) makes the phase-3–6 read-only data
and optimiser workings directly usable from a phone or desktop browser.

## Confirmed initial plant

| Component | Connection | Role |
| --- | --- | --- |
| Solplanet ASW12kH-T3 | Direct Modbus RTU on MONITOR pins 1–2 | Solplanet inverter, ESS, aggregate battery and future control |
| Three parallel seven-module Ai-HB G2 systems | Reported through the ASW | Energy storage |
| Eastron SEM3-M-2L-CT | Passive terminal-8 observation through SH-U11F RX | Authoritative grid and external-PV measurement |
| Solis-10K with 12.4 kW east/west array | Measured by Eastron slave 2 | External AC-coupled PV |
| Forecast.Solar Personal Plus | HTTPS integration | East/west PV forecast |
| GloBird ZEROHERO VPP | Configured tariff integration | Import/export price model |

The ASW has no directly connected PV and its terminal-10 CTs are not fitted.
The Eastron grid CTs cover all three grid phases and its second set covers all
three Solis feeder phases.

## Source authority

Every measurement in the common model must have one accounting authority.
Other sources may corroborate it or add device detail without silently
replacing it.

| Data | Authoritative source | Optional corroboration or enhancement |
| --- | --- | --- |
| Grid import/export power and energy | Eastron slave 1 | ASW smart-meter mirror |
| External-PV AC power and energy | Eastron slave 2 | Direct Solis/vendor driver |
| ASW inverter power, state and faults | ASW MONITOR | None initially |
| Battery power, SOC, SOH, temperature and limits | ASW MONITOR | Future direct BMS integration |
| External-inverter DC inputs, temperature and faults | Direct vendor driver, when installed | Not required for plant accounting |
| Tariff | User-selected tariff adapter and configuration | Manual override |
| PV forecast | Forecast.Solar adapter | Persistence-based forecast correction |

If several external inverters are enclosed by the slave-2 CTs, Eastron remains
authoritative for their aggregate. Individual attribution requires a direct
driver or additional metering.

## Proposed runtime architecture

```text
 SH-U11F RX ─ passive RTU parser ─ Eastron decoder ─┐
 ASW MONITOR ─ RTU scheduler ───── ASW driver ─────┤
 Optional inverter drivers ────────────────────────┤
 Forecast/tariff adapters ─────────────────────────┤
                                                   v
                                      Normalised plant model
                                         │       │
                           event/current state   health/capabilities
                                         │       │
                           SQLite history/rollups │
                                         │       │
                                         v       v
                                 REST + streaming API
                                         │
                                  optimisation planner
                                         │
                               safety and control executor
                                         │
                                     ASW Modbus
```

The initial implementation uses Python 3.11 or newer. The existing
standard-library discovery code and capture decoder are extracted rather than
replaced. A dependency-free threaded runtime is sufficient for the 9600-baud
serial links and keeps the API, persistence, passive receiver and active ASW
bus owner isolated.

Suggested repository layout:

```text
src/solplanet_fasttalk/
  api/
  config/
  devices/asw/
  devices/eastron/
  devices/solis/
  integrations/forecast_solar/
  integrations/tariffs/
  modbus/
  optimisation/
  plant/
  storage/
  service/
tests/
  captures/
  integration/
  unit/
```

## Core data contracts

A measurement should include:

- stable plant-level name;
- value and unit;
- source device, channel and register;
- source authority and access mode;
- observation and ingestion timestamps;
- sequence or transaction identifier where available;
- quality (`good`, `stale`, `invalid`, `missing` or `conflicting`);
- expected update interval and current age; and
- optional raw value for diagnostics.

Device integrations should publish:

- identity and configured connection;
- declared capabilities;
- communication and data health;
- last successful observation;
- supported controls, if any; and
- dependencies such as `passive; requires ASW meter poller`.

Plant-level names must not expose vendor register terminology. Examples include
`grid.active_power`, `external_pv.active_power`, `battery.soc` and
`site.load_power`.

## Polling and freshness policy

The passive Eastron integration never transmits. It reconstructs observed
requests and responses, verifies CRCs, and publishes only matched, complete
transactions.

Initial freshness expectations derived from the live capture are:

| Data | Expected update | Stale after |
| --- | ---: | ---: |
| Grid phase active power | about 0.45 s | 2 s |
| External-PV phase active power | irregular 3–9 s | 20 s |
| External-PV general measurements | about 12.5 s | 30 s |
| Eastron cumulative energy | about 12.5–30 s | 90 s |

ASW polling should use separate schedules:

- fast operating power, battery power and SOC: initially 1 second;
- limits, state, smart-meter status and fault words: 2–5 seconds;
- energy counters and temperatures: 15–60 seconds; and
- identity/firmware: at startup and after reconnect.

The scheduler must serialize each active RTU bus, bound retries, introduce
backoff after failures, and never let optional requests starve safety-critical
state.

## Delivery phases

### Phase 0: foundations

Deliver:

- installable Python package and command-line entry point;
- typed configuration model with explicit serial by-ID paths;
- structured logging, monotonic timing and clean shutdown;
- reusable Modbus CRC, framing and decoding modules extracted from the tools;
- capture fixtures with private identifiers removed; and
- unit-test and formatting configuration.

Acceptance:

- existing discovery-tool tests remain green;
- the package starts with an example read-only configuration;
- invalid or duplicate serial-device assignments fail before ports are opened;
- no secret or site location is written to logs.

### Phase 1: passive Eastron vertical slice

Deliver:

- long-running read-only serial worker;
- CRC-based stream recovery across arbitrary USB chunk boundaries;
- request/response transaction matching;
- decoders for slaves 1 and 2 and observed register blocks `0`, `12`, `200`
  and `342`;
- source-authority mapping and sign/orientation configuration;
- current-value cache and communication-health metrics; and
- replay tests using the successful terminal-8 capture.

Acceptance:

- replay recovers all 302 valid frames and 151 transactions from the fixture;
- the first two partial bytes do not create a false frame;
- corrupt, truncated and unmatched frames never publish measurements;
- the serial descriptor is opened read-only and the module contains no
  transmit path;
- grid and external-PV phase sums match independently calculated fixture
  values; and
- a live soak records CRC error rate, missing responses and data freshness
  without disturbing ASW meter operation.

### Phase 2: ASW read-only integration

Deliver:

- one-owner Modbus RTU scheduler for MONITOR pins 1–2;
- identity, inverter, battery, grid, meter-state and control-state decoders;
- observed live-firmware deviations and signed-NaN handling;
- reconnect, retry, backoff and stale-data behaviour; and
- aggregate representation of the three parallel Ai-HB G2 systems.

Acceptance:

- all confirmed baseline groups decode from saved captures;
- unsupported register `46451` is never included in a normal poll;
- battery and inverter values become stale rather than silently retaining old
  values after disconnection;
- no Modbus write function exists in the read-only daemon build.

### Phase 3: plant model, history and monitoring API

Deliver:

- authoritative-source resolver and conflict reporting;
- derived site power flow and self-consumption metrics;
- SQLite storage in WAL mode for observations, events and rollups;
- configurable raw-data retention and hourly/daily aggregation;
- REST endpoints for current state, history, devices, capabilities and health;
- streaming updates using WebSocket or server-sent events; and
- metrics suitable for service monitoring.

Acceptance:

- Eastron always wins plant-accounting conflicts with a direct inverter
  counter;
- derived values become unavailable when required inputs are stale;
- daemon restart preserves history and cumulative-counter baselines;
- API responses include timestamp, unit, quality and source;
- a dashboard client can operate without accessing any hardware directly.

### Phase 4: optional device integrations

Deliver:

- stable plugin boundary for third-party inverter diagnostics;
- existing Solis decoder converted into the first reference plugin;
- capability merging without changing accounting authority; and
- explicit aggregate-versus-individual device relationships.

Acceptance:

- removing the Solis plugin does not remove external-PV power, energy,
  self-consumption or optimisation inputs;
- installing it adds DC inputs, temperature, state and diagnostics;
- disagreement with Eastron raises a diagnostic event but does not replace the
  authoritative AC measurement.

### Phase 5: tariffs and forecasting

Deliver:

- tariff model supporting import/export prices, controlled-load periods,
  time-of-use windows and exceptional events;
- versioned GloBird ZEROHERO configuration based on the supplied plan;
- Forecast.Solar east/west array adapter with credentials kept outside the
  main configuration file;
- rate limiting, cache, retry and offline behaviour; and
- forecast-versus-actual tracking using authoritative Eastron generation.

Acceptance:

- price lookup is deterministic across time-zone and daylight-saving
  transitions;
- forecast failure cannot stop local measurement or ASW operation;
- cached forecasts remain visibly aged rather than appearing current;
- historical production can be compared with each array forecast.

### Phase 6: optimisation in shadow mode

Deliver:

- short-horizon plant forecast;
- self-consumption objective followed by tariff-cost objective;
- battery SOC, charge/discharge limits, reserve and efficiency constraints;
- site import/export constraints;
- recommendation schedule with a human-readable explanation; and
- replay/simulation against historical data without sending commands.

Acceptance:

- every recommendation cites the measurements, prices and constraints that
  caused it;
- plans never exceed observed BMS or configured site limits;
- missing/stale SOC, grid power or tariff data produces a conservative
  no-action plan;
- simulation demonstrates improvement over the configured baseline before
  control work begins.

### Phase 7: bounded ASW control

This phase begins only after the exact ASW control registers and failure modes
have been verified on the live model and firmware.

Deliver:

- explicit allow-list of supported write operations;
- validation against BMS, inverter, tariff and site limits;
- read-after-write verification;
- command rate limiting, idempotency and durable audit records;
- manual override and automation-disable controls;
- watchdog and deterministic fallback state; and
- staged modes: disabled, shadow, supervised and automatic.

Acceptance:

- control is disabled by default;
- a lost authoritative grid measurement, ASW link, valid SOC or daemon
  watchdog prevents new automated commands;
- invalid, stale or out-of-range requests fail before any Modbus frame is sent;
- unexpected readback disables automation and raises an alarm;
- supervised tests prove safe charge, discharge, stop and recovery behaviour;
- control tests never depend on vendor cloud availability.

### Phase 8: operational release

Deliver:

- least-privilege `systemd` service and dedicated service account;
- serial-device permissions without running as root;
- authenticated API with local binding by default;
- configuration validation, backup and migration;
- database maintenance and retention jobs;
- installation, upgrade, recovery and troubleshooting documentation; and
- compatibility record for exact hardware, firmware and connection modes.

Acceptance:

- clean restart after power loss;
- no mandatory outbound connection;
- daemon remains useful with forecast and tariff services unavailable;
- health reports distinguish serial, device, data-freshness and integration
  failures;
- release checklist includes a multi-day terminal-8 capture soak.

## Safety gates

The following are non-negotiable:

1. The SH-U11F uses only `RXD+` and `RXD-`; `TXD+`, `TXD-`, termination, 5 V
   and ground remain disconnected from terminal 8.
2. No daemon component may actively transmit on terminal 8.
3. Only one process owns each active serial adapter.
4. Measurement freshness is enforced before derivation or control.
5. Eastron remains authoritative for grid and external-PV accounting.
6. Control remains disabled until separately verified for the exact ASW model
   and firmware.
7. Existing inverter protection and export limiting remain safety mechanisms;
   the daemon is not a substitute for mandated electrical protection.
8. Every proposed write is classified by the
   [Modbus write safety policy](modbus-write-safety.md). Permanently
   prohibited and unreviewed operations cannot reach a frame builder;
   approval-required operations need an informed, owner-approved risk
   assessment.

## Initial API surface

The first version should provide:

```text
GET  /v1/plant
GET  /v1/measurements/current
GET  /v1/measurements/history
GET  /v1/devices
GET  /v1/health
GET  /v1/tariffs/current
GET  /v1/tariffs/forecast
GET  /v1/forecasts/pv
GET  /v1/plans/current
GET  /v1/plans/history
GET  /v1/financials/history
GET  /v1/financials/summary
GET  /v1/diagnostics
GET  /v1/events
GET  /v1/stream
```

Control endpoints are deliberately excluded until phase 7. When introduced,
they must be authenticated, idempotent and audited.

## First development milestone

The first milestone is a read-only daemon that can run continuously and expose:

- authoritative grid power from Eastron slave 1;
- authoritative external-PV power from Eastron slave 2;
- ASW inverter and aggregate battery state;
- source freshness and communication health;
- derived site flow using only fresh inputs;
- current state and recent history through the API.

It does not require direct Solis integration, tariffs, forecasts or any
control. Completing this milestone validates the serial architecture and
source-authority model before higher-level behaviour is added.

## Open validation work

- Run a multi-day SH-U11F capture and quantify CRC, unmatched-frame and missing
  response rates.
- Correlate terminal-8 slave 1 against the ASW `46401–46412` mirror over a
  sustained import/export transition.
- Correlate terminal-8 slave 2 against direct Solis active power across low,
  high and rapidly changing production.
- Confirm CT direction labels and persist orientation explicitly rather than
  embedding assumptions in the decoder.
- Verify cumulative Eastron counters across midnight and daemon restart.
- Extend the successful phase-4–6 canary into a multi-day soak and retain only
  privacy-reviewed health summaries.
- Accumulate enough authoritative Eastron production history to quantify
  Forecast.Solar error and shadow-plan replay performance.
- Determine and safely test the exact ASW control surface before phase 7.
- Validate any future tariff revision as a new version rather than mutating the
  archived pre-July 2026 plan.

The first combined daemon validation has already confirmed 62 successful ASW
reads without failures or reconnects, 47 clean passive Eastron transactions,
zero persistence failures and exact reconciliation of the instantaneous plant
flow equation. Full evidence and the two resulting data-model corrections are
documented in [Live system discovery](live-system-discovery.md#first-live-daemon-validation).

Remote development against this plant must follow the
[safe remote development workflow](remote-development-workflow.md). That
workflow grants broad autonomy inside the current read-only boundary while
reserving all device writes, resets, firmware operations and physical changes
for explicit review and approval.
