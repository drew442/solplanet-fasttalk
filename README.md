# solplanet-fasttalk

Fast, local-first monitoring and control for Solplanet hybrid inverters and
energy storage systems.

> [!IMPORTANT]
> This project is at an early stage. Phases 0–6 now provide read-only live
> monitoring, persistence, optional Solis diagnostics, tariffs, forecasting and
> shadow optimisation. The API is not yet stable and hardware control remains
> unavailable.

## Overview

`solplanet-fasttalk` is a Linux daemon for operating a photovoltaic (PV) and
energy storage system (ESS) without placing a vendor cloud service in the
control loop.

The daemon communicates directly with on-premises Solplanet hardware, collects
high-frequency telemetry, and exposes monitoring and control through a
documented API. It is intended to provide the local capabilities normally
associated with the Solplanet app, communications dongle, and cloud monitoring
service while keeping system operation fast, private, and resilient to an
internet outage.

Solplanet hybrid inverters and batteries are the core of the system. Relevant
third-party equipment—such as additional PV inverters, energy meters, weather
services, and PV forecasting providers—can be added through modular
integrations. Where a site meter measures an external inverter's AC feeder,
that meter can provide the authoritative plant-accounting measurement without
requiring a vendor-specific inverter driver.

## Goals

- Communicate with supported Solplanet equipment directly over Modbus.
- Provide low-latency telemetry and control on the local network.
- Monitor the PV and ESS plant as a whole, including relevant third-party
  devices and data sources.
- Replace the local operational dependency on the Solplanet app, dongle, and
  cloud service where the hardware permits it.
- Expose all telemetry, configuration, status, and supported controls through
  an API.
- Optimise grid import and export to maximise self-consumption.
- Use the user's electricity plan, import tariffs, export tariffs, and other
  constraints to reduce cost or increase arbitrage revenue.
- Continue safe, useful operation when internet or external services are
  unavailable.
- Make new device and service integrations quick to develop, test, and deploy.

## Non-goals

The daemon will not initially provide:

- a mobile application;
- a hosted cloud monitoring platform;
- a full plant-configuration or control interface; or
- support for unrelated home-automation devices.

Those capabilities can be built separately using the daemon's API.

## Design principles

### Local first

Plant operation must not depend on an internet connection. Local measurements,
local decisions, and local control remain available when cloud services fail.

### Direct device access

On-premises hardware should be accessed using Modbus directly wherever
possible. Device access is ranked in this order:

1. **Direct wired Modbus** — preferred for predictable latency, reliability,
   and control.
2. **Direct local-network Modbus** — suitable when the device exposes Modbus
   TCP without an external service.
3. **Local vendor gateway or dongle** — tolerated only when direct access is
   unavailable and the gateway provides a dependable local interface.
4. **Vendor cloud API** — discouraged and used only as an explicitly enabled,
   last-resort integration.

An integration should expose its access mode so operators can understand its
latency, dependencies, and failure characteristics.

### Solplanet centric, plant aware

Solplanet devices remain first-class components, while optimisation decisions
use all relevant plant data. A non-Solplanet source should integrate through a
stable boundary rather than leak vendor-specific behaviour into the core.

### Safe control

Control is conservative by default. The daemon should validate commands,
enforce device and site limits, handle stale or missing measurements, avoid
rapid command oscillation, and fail into a documented safe state. Operators
must be able to disable automation and retain manual control.

### Observable and explainable

Telemetry should carry timestamps, units, quality, and source information.
Automated actions should be auditable: an operator should be able to determine
what changed, when it changed, and why.

## Proposed architecture

```text
 Solplanet inverter / ESS ── direct Modbus ─────────┐
 Grid / external-PV meter ── passive Modbus ────────┼── Plant data model
 Optional device diagnostics ─ direct integration ─┤          │
 Forecast / tariff service ── integration ─────────┘          ├── Telemetry
                                                              ├── Optimisation
                                                              ├── Control/safety
                                                              └── Public API
                                                                      │
                                                  dashboards / automation / apps
```

The daemon is expected to be divided into a small set of clear subsystems:

- **Device drivers** implement register maps, discovery, polling, decoding, and
  supported controls.
- **Integrations** connect external devices or services and translate them into
  a common plant model.
- **Telemetry** normalises measurements and makes current and historical data
  available to API consumers.
- **Tariffs and forecasts** represent time-varying import/export prices and
  expected production or demand.
- **Optimisation** plans battery charging, discharging, import, and export
  within user, site, and hardware constraints.
- **Control and safety** validates and applies the plan while detecting stale
  data, communications failures, and conflicting commands.
- **API** provides a stable interface for monitoring, configuration, control,
  and future user interfaces.

## Integration model

External integrations are pluggable and modular. Examples include:

- third-party PV inverters;
- site and circuit energy meters;
- battery or inverter accessories;
- PV production and weather forecasts;
- electricity tariff and market-price feeds; and
- demand-response or export-limit signals.

An integration should declare its capabilities and dependencies, publish data
using standard names and units, report data freshness and health, and remain
isolated from the core optimisation logic. Cloud-backed integrations must be
optional and must not prevent local hardware operation when unavailable.

### Measurement authority

The initial plant uses the terminal-8 Eastron SEM3-M-2L-CT as the
authoritative source for:

- utility-grid import/export and its per-phase electrical measurements; and
- the AC output delivered by the external Solis PV inverter.

The ASW MONITOR integration remains authoritative for Solplanet inverter,
aggregate Ai-HB battery, BMS, operating-state and control data. A direct Solis
or other third-party inverter integration is optional: it can add DC input
measurements, temperature, identity, faults, diagnostics or supported device
control, but its energy counters do not replace the Eastron measurement for
plant accounting.

This policy permits basic compatibility with any AC-coupled inverter measured
by the external-PV CTs. If several inverters share those CTs, their production
is authoritative only as an aggregate; individual diagnosis still requires
per-inverter integration or additional metering.

## Energy optimisation

Optimisation will consider, where available:

- current and forecast PV production;
- current and forecast site consumption;
- battery state of charge, power limits, reserve, and operating constraints;
- fixed, time-of-use, dynamic, and demand-based import tariffs;
- fixed, time-varying, or dynamic export tariffs;
- site import/export limits;
- user preferences and override schedules; and
- uncertainty, stale inputs, and communications failures.

The initial objective is to maximise self-consumption. Cost-aware scheduling and
energy arbitrage build on the same plant model. Financial optimisation must
remain subordinate to hardware limits, configured safety constraints, and the
user's minimum reserve requirements.

The current optimiser is deliberately shadow-only: it publishes an explained,
constrained recommendation schedule and replay comparison but cannot execute
the schedule. Missing or stale required data produces no action.

## API direction

All useful daemon functionality should be available without a bundled UI. The
API is expected to cover:

- live and historical measurements;
- device inventory, capabilities, and health;
- alarms and communications status;
- tariff, forecast, and optimisation inputs;
- schedules, decisions, and explanations;
- supported device controls and user overrides; and
- daemon and integration configuration.

The API protocol, authentication model, and versioning policy will be defined
before the first stable release. Network control endpoints must be
authenticated and should bind locally by default.

The implemented local API currently includes plant state, current/raw/rollup
history, devices, capabilities, health, events, server-sent events, the
pre-July 2026 ZEROHERO tariff, two-plane Forecast.Solar output,
forecast-versus-actual history, shadow plans and Prometheus-format service
metrics. It also persists actual tariff cost/revenue and optimiser decision
history, publishes future tariff prices, and compares each proposal with
continued operation of the inverter's current native mode. A responsive,
read-only [diagnostics web UI](docs/diagnostics-webui.md) shows current and
historical plant flow, financials, forecast accuracy, forecasts,
recommendations and the evidence behind the optimiser's decisions. See
[phases 3–6](docs/phases-3-to-6.md) for exact daemon behaviour and
[data quality and forecasting](docs/data-quality-and-forecasting.md) for the
measurement audit, weather-assisted correction, site-load/SOC prediction
datasets and independent accuracy gates.

## Reliability and security

Because this software can control grid-connected electrical equipment, the
project will favour explicit configuration and bounded behaviour:

- least-privilege Linux service operation;
- authenticated control access;
- validation against hardware and site limits;
- timeouts, retries, and backoff for device communication;
- clear handling of stale, missing, or invalid data;
- persistent audit records for control actions;
- deterministic fallback behaviour; and
- no mandatory outbound cloud connection.

This software must not be treated as a substitute for required electrical
protection, export limiting, or manufacturer safety mechanisms.

## Roadmap

The delivery status is:

1. Phases 0–2: foundations and live read-only Eastron/ASW integrations —
   implemented.
2. Phase 3: plant model, SQLite history/rollups and monitoring API —
   implemented.
3. Phase 4: optional plugin boundary and direct Solis diagnostics —
   implemented.
4. Phase 5: versioned ZEROHERO tariff and Forecast.Solar adapter —
   implemented.
5. Phase 6: constrained self-consumption/arbitrage planning in shadow mode —
   implemented.
6. Read-only diagnostics UI and authenticated opt-in LAN access — implemented.
7. Phase 7: separately approved, bounded ASW control — not started.
8. Phase 8: production packaging and remaining operational hardening — not
   complete.

Compatibility will be tracked by exact inverter, battery, firmware, connection
method, and tested feature set rather than by broad product-family claims.

The detailed implementation sequence, subsystem boundaries and milestone
acceptance criteria are in the [daemon implementation plan](docs/daemon-plan.md).

## Contributing

Contributions will be especially valuable in these areas:

- verified Solplanet Modbus register documentation;
- read-only captures from specific hardware and firmware combinations;
- device drivers and external integrations;
- control safety and failure-mode testing;
- tariff and forecast adapters; and
- API clients and independent user interfaces.

When reporting hardware behaviour, include the exact model, firmware version,
connection method, and whether each register or control has been verified on
physical equipment. Never include credentials, device serial numbers, Wi-Fi
details, or other private site information in an issue or capture. The
documented FTDI USB adapter identifiers are the sole currently approved
exception because they are required for stable by-ID device assignment.

Development, testing, and contribution instructions will be added alongside the
first implementation.

## Live hardware discovery

The repository includes guarded, read-only Modbus RTU discovery utilities for
the initial Solis and Solplanet test hardware and a receive-only terminal-8
capture utility. See the
[live system discovery runbook](docs/live-system-discovery.md) before using
them. The Eastron bus must remain under the inverter's control: the project
observes it through physically disconnected transmit terminals and never adds
a second Modbus master.

## Project status

The daemon is implemented through phase 6: passive Eastron grid/external-PV
telemetry, direct read-only ASW inverter/battery telemetry, measurement
freshness, derived plant flow, SQLite history, optional Solis diagnostics,
tariffs, PV forecasting, shadow optimisation, a local API and a diagnostics
web UI. Live HAOS canaries completed without disrupting the ASW, either meter,
import/export behaviour or the native Solplanet app. Hardware control,
extended soak testing and production release hardening remain future
milestones.

Do not use this software to control production equipment until the relevant
hardware and safety paths are explicitly marked as tested.

Live-system findings are maintained in the
[discovery runbook](docs/live-system-discovery.md). Development against
physical equipment must follow the
[safe remote development workflow](docs/remote-development-workflow.md);
remote access does not authorize device control.

The [Modbus write safety policy](docs/modbus-write-safety.md) defines
machine-readable permanent prohibitions, approval-gated plant controls and the
risk assessment required before any physical write. The current daemon remains
read-only and rejects every write function.

The confirmed Custom-mode load-following and fixed-window power semantics used
by the shadow planner are documented in
[ASW battery operating modes](docs/asw-operating-modes.md).

`solplanet-fasttalk` is an independent project and is not affiliated with or
endorsed by Solplanet.

## Running the read-only daemon

Python 3.11 or newer is required. Install the checkout and create a local
configuration:

```console
python3 -m pip install .
sudo install -d -o solplanet-fasttalk -g solplanet-fasttalk \
  /var/lib/solplanet-fasttalk
sudo install -m 0640 config/solplanet-fasttalk.example.toml \
  /etc/solplanet-fasttalk.toml
solplanet-fasttalk check-config --config /etc/solplanet-fasttalk.toml
solplanet-fasttalk run --config /etc/solplanet-fasttalk.toml
```

Review the serial by-ID paths and sign multipliers before starting. The API
defaults to `127.0.0.1:8765`. Open the diagnostics UI at
<http://127.0.0.1:8765/diagnostics/>. A non-loopback bind fails unless a
private bearer-token file is configured; see the
[diagnostics web UI guide](docs/diagnostics-webui.md) before enabling LAN
access.

The repository includes background operator scripts for explicit local and
authenticated LAN modes:

```console
./scripts/fasttalk-local.sh start /path/to/runtime.toml
./scripts/fasttalk-local.sh stop

./scripts/fasttalk-token.sh create
./scripts/fasttalk-lan.sh start /path/to/runtime.toml
./scripts/fasttalk-lan.sh stop
./scripts/fasttalk-token.sh destroy
```

Use `status` with either daemon script and `show` with the token script when
the browser credential needs to be entered. The scripts prevent concurrent
local/LAN instances and do not manage daemons started through systemd or
another service supervisor.

Useful read-only endpoints include:

```text
GET /v1/plant
GET /v1/measurements/current
GET /v1/measurements/history?name=grid.active_power
GET /v1/diagnostics
GET /v1/devices
GET /v1/health
GET /v1/events
GET /v1/stream
```

The example unit in
[`packaging/systemd/solplanet-fasttalk.service`](packaging/systemd/solplanet-fasttalk.service)
can run the daemon continuously after the service user, serial permissions,
configuration and installation path have been prepared.

The terminal-8 capture can be replayed without hardware:

```console
solplanet-fasttalk replay-eastron \
  --capture discovery-output/eastron-terminal8-sniff-9600.json
```
