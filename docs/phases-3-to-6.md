# Phases 3–6 implementation

The daemon now implements the plant-model, optional diagnostics, tariff,
forecasting and shadow-planning scope through phase 6. It remains incapable of
sending a Modbus write. Phase 6 produces recommendations and simulations only.

## Plant model and persistence

The terminal-8 Eastron remains authoritative for grid and aggregate external-PV
AC accounting. Direct Solis values use the `solis.*` namespace and diagnostic
authority; a sustained difference raises an event without replacing Eastron.

Fresh grid, external-PV, ASW AC and dedicated ASW PV measurements derive:

- `site.load_power`;
- `site.generation_power`;
- `site.pv_generation_power`;
- `site.local_supply_power`;
- `site.self_consumption_power`;
- `site.self_consumption_ratio`; and
- `site.self_sufficiency_ratio`.

All derived values are withdrawn when a required input is missing, invalid or
stale. SQLite uses WAL mode and stores raw measurements, events, forecast
points, hourly rollups, daily rollups, minute tariff-accounting intervals and
complete shadow-plan decisions. The latest cumulative energy counters remain
available as restart baselines.

History accepts `resolution=raw`, `resolution=hourly` or `resolution=daily`.
Service metrics are exposed in Prometheus text format at `/metrics`.
The diagnostics UI can instead request bounded time buckets from 10 seconds to
one day, allowing useful phone and desktop graphs without transferring raw
high-rate history.

## Optional Solis plugin

The first versioned device-plugin interface is implemented by the direct Solis
RS-485 reader. It issues only function-`0x04` requests and adds:

- DC input voltage and current;
- total DC and diagnostic AC power;
- inverter temperature, status and working mode;
- grid frequency; and
- device energy counters.

Removing or disabling this plugin does not remove any grid, external-PV,
self-consumption or optimisation input. The Eastron CT channel remains the
external-PV authority.

## GloBird ZEROHERO tariff

The built-in plan ID is
`globird-zerohero-vpp-ausgrid-pre-2026-07`. It is an archived interpretation of
the supplied Ausgrid price fact sheet effective 25 May 2026, not a dynamically
updated offer.

All prices include GST:

| Item | Local time | Price |
| --- | --- | ---: |
| Off-peak import | 11:00–13:59 every day | $0.000/kWh |
| Peak import | 16:00–22:59 every day | $0.572/kWh |
| Shoulder import | all other times | $0.462/kWh |
| Controlled load | plan rate | $0.319/kWh |
| Daily supply | per day | $1.650 |
| Base feed-in | 16:00–22:59 | $0.050/kWh |
| Super Export | 18:00–20:59, first 15 kWh/day | $0.150/kWh total |
| ZEROHERO | imports no more than 0.03 kWh/hour, 18:00–20:59 | $1/day credit |

Critical-peak events are not inferred and no VPP enrollment is assumed.
Manually confirmed events may be supplied in a target-only JSON file. Tariff
lookup uses `Australia/Sydney` and aware datetimes, including deterministic
handling of both folds when daylight saving ends.

Completed UTC-minute averages of authoritative `grid.active_power` are priced
into a persistent ledger. Positive power is import and negative power is
export. The ledger records applicable import/export periods and prices, energy,
import cost, export credit, net cost, sample coverage and tariff plan ID. It
also applies the daily supply charge once per observed local day, the 15 kWh
daily Super Export cap, and the $1 ZEROHERO credit only after all three local
hours have all 60 accounted minutes and each remains at or below the
0.03 kWh import threshold. Historical gaps remain gaps rather than being
interpolated.

## Forecast.Solar

The adapter uses a key file and a separate location file. Neither the key nor
the coordinates are accepted as ordinary TOML values, returned by the API,
written to the forecast cache, or included in error messages.

The confirmed initial planes are:

| Plane | Tilt | Forecast.Solar azimuth | Peak power |
| --- | ---: | ---: | ---: |
| East | 25° | -90° | 6.2 kWp |
| West | 25° | +90° | 6.2 kWp |

The two-plane request returns one combined forecast. That combined prediction
is compared with the Eastron's authoritative aggregate external-PV AC power;
the present CT arrangement cannot provide separate east-versus-west actual
production. Forecast points are persisted for historical comparison.

The base forecast now passes through astronomical night gating and robust
long/short-term correction against the authoritative Eastron actual. An
Open-Meteo integration supplies cloud, precipitation, temperature and tilted
irradiance context without exposing the private runtime location. Every base
and corrected issuance is retained and scored by lead-time band. Shadow plans
may continue learning, but the forecast cannot report control readiness until
the independent 28-day accuracy gate passes. See
[Data quality and forecasting](data-quality-and-forecasting.md).

The worker enforces a configurable refresh interval and retry backoff. Its
atomic local cache remains visibly marked as cached and aged. A provider or
internet failure degrades only this integration and cannot stop the serial
workers.

## Shadow optimisation

The optimiser uses 15-minute slots by default and applies objectives in this
order:

1. charge forecast PV surplus and discharge to serve forecast load;
2. move energy from lower import-price periods to later higher-price periods;
3. respect configured site import/export limits.

Positive recommended battery power means discharge; negative means charge.
Every slot reports forecast load and PV, tariff prices, resulting grid power,
resulting SOC, hardware/configured limits and a human-readable explanation.

The constraints include:

- observed battery voltage and BMS charge/discharge current limits;
- the ASW12kH-T3 manufacturer battery charge/discharge rating of 12 kW;
- configured inverter charge/discharge power limits;
- usable capacity, reserve SOC and maximum SOC;
- charge and discharge efficiency; and
- configured site import/export boundaries.

The effective interval limit is the lowest of the 12 kW manufacturer rating,
the configured ceiling, and the live BMS voltage×current ceiling. Within that
limit the planner uses the required safe power over the short interval; it does
not spread an energy transfer across the entire horizon merely to reduce
instantaneous power. The documented 24 kVA EPS overload rating is explicitly
excluded because it applies for no more than 10 seconds and is not a battery
dispatch rating.

The no-daemon-change baseline is no longer an idle-battery assumption. When
fresh values are available, it projects the ASW's currently stored
charge/hold/discharge state and power command (Modbus registers 41152/41153)
until the inverter's native lower or upper SOC bound. The API labels the
assumption that this current command persists; it cannot predict an unknown
future native schedule change. If the native command is unavailable, the
baseline falls back visibly to hold.

Observed BMS limits can only reduce the applicable power ceiling. Missing or stale
SOC, authoritative grid power, derived site load or PV forecast produces an
empty `no_action` plan. The optimiser has no serial transport reference and
reports `control_commands_sent: 0` and `execution_available: false`.
Every ready, infeasible and no-action decision is stored with its inputs,
recommendations, explanations, native baseline, costs and constraint
provenance.

Historical data can be replayed without hardware:

```console
venv/bin/solplanet-fasttalk replay-optimisation \
  --config private-config/runtime.toml \
  --since 2026-06-01T00:00:00+00:00 \
  --until 2026-06-02T00:00:00+00:00
```

The result compares baseline and modelled cost, import and export. A positive
estimated improvement is evidence for the simulated interval, not permission
to control equipment.

## API additions

The phase-6 read-only API includes:

```text
GET /v1/capabilities
GET /v1/tariffs/current
GET /v1/tariffs/forecast
GET /v1/forecasts/pv
GET /v1/forecasts/pv?since=...&until=...
GET /v1/weather
GET /v1/plans/current
GET /v1/plans/history
GET /v1/financials/history
GET /v1/financials/summary
GET /v1/diagnostics
GET /v1/measurements/history?name=...&bucket_seconds=...
GET /metrics
```

The daemon serves the read-only diagnostics UI at `/diagnostics/`. It combines
past measurements, live authoritative state, forecast plans and the optimiser's
inputs/explanations. Loopback remains the default; an opt-in non-loopback bind
requires a private bearer-token file. See
[Diagnostics web UI](diagnostics-webui.md).

All `POST` requests remain rejected. Phase 7 cannot begin until its separate
hardware-specific write verification, risk assessment and explicit approval
gates have been completed.

## Initial live validation

The bounded HAOS canary for code commit `6cf2d43` passed the phase-6 runtime
invariants:

- Eastron, ASW and optional Solis workers remained healthy;
- required accounting, battery and derived measurements were fresh;
- Eastron retained grid and external-PV authority while Solis remained
  diagnostic;
- the two-plane provider response was cached and all forecast points were
  persisted after correcting a SQLite writer-lock starvation issue;
- forecast-versus-authoritative-actual comparison was populated;
- the optimiser produced a feasible 47-slot schedule;
- every recommendation contained explanations and remained inside observed
  BMS, configured SOC and configured site limits;
- the live replay estimated a positive tariff-cost improvement;
- the storage queue reported no dropped measurements or write failures;
- API mutation was rejected with HTTP 405; and
- the plan reported zero control commands and no execution capability.

The canary was stopped cleanly after validation. This result supports continued
shadow-mode soaking; it does not satisfy or bypass any phase-7 control gate.

A subsequent version-0.4.0 canary at code commit `06f40cd` validated the
completed diagnostics path against the same live plant:

- the native baseline read the stored ASW charge state, 12 kW command and
  native 10%–100% SOC bounds;
- the effective charge and discharge ceilings were 12 kW, with both
  manufacturer provenance and live-BMS/configured reductions exposed;
- a 59-interval shadow plan, future tariff series, financial ledger and plan
  history were available through their read-only endpoints;
- actual tariff accounting backfilled the available authoritative grid
  history and explicitly reported realized daemon savings as unavailable in
  shadow mode;
- an initial concurrent SQLite-write collision was reproduced, fixed with
  bounded low-frequency writer waits, and did not recur after redeployment;
- all forecast, accounting, optimisation, storage and hardware components
  reported healthy with no persistence failures, queue drops, serial failures,
  reconnects, CRC errors, missing responses or Modbus exceptions; and
- the plan continued to report zero control commands and no execution
  capability.

The LAN daemon was left running under its existing script-managed,
authenticated configuration after the successful read-only canary.
