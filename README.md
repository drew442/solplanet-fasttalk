# solplanet-fasttalk

Fast, local-first monitoring and control for Solplanet hybrid inverters and
energy storage systems.

> [!IMPORTANT]
> This project is at an early stage. The architecture and interfaces described
> below are goals, not a declaration of current hardware support or API
> stability.

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
integrations.

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
- a replacement user interface for every deployment; or
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
 Solplanet inverter / ESS ── direct Modbus ──┐
 External inverter / meter ─ integration ────┼── Device and data model
 Forecast / tariff service ─ integration ────┘            │
                                                          ├── Telemetry
                                                          ├── Optimisation
                                                          ├── Control and safety
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

The broad delivery sequence is:

1. Document supported hardware and Solplanet Modbus register maps.
2. Build a read-only daemon with device discovery, polling, health reporting,
   and live telemetry.
3. Define the common plant model and versioned API.
4. Add safe, bounded control for supported Solplanet devices.
5. Introduce the external integration interface and reference integrations.
6. Add tariff and forecast models.
7. Implement self-consumption optimisation, followed by cost and arbitrage
   strategies.
8. Add persistence, packaging, Linux service files, and upgrade guidance.

Compatibility will be tracked by exact inverter, battery, firmware, connection
method, and tested feature set rather than by broad product-family claims.

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
physical equipment. Never include credentials, serial numbers, Wi-Fi details,
or other private site information in an issue or capture.

Development, testing, and contribution instructions will be added alongside the
first implementation.

## Project status

Design and initial development. Do not use this software to control production
equipment until the relevant hardware and safety paths are explicitly marked as
tested.

`solplanet-fasttalk` is an independent project and is not affiliated with or
endorsed by Solplanet.
