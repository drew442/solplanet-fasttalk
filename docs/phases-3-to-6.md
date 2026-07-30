# Phases 3–6 implementation

The daemon now implements the plant-model, optional diagnostics, tariff,
forecasting and shadow-planning scope through phase 6. It remains incapable of
sending a Modbus write. Phase 6 produces recommendations and simulations only.

## Plant model and persistence

The terminal-8 Eastron remains authoritative for grid and aggregate external-PV
AC accounting. Direct Solis values use the `solis.*` namespace and diagnostic
authority; a sustained difference raises an event without replacing Eastron.

Fresh grid, external-PV and ASW measurements derive:

- `site.load_power`;
- `site.generation_power`;
- `site.self_consumption_power`;
- `site.self_consumption_ratio`; and
- `site.self_sufficiency_ratio`.

All derived values are withdrawn when a required input is missing, invalid or
stale. SQLite uses WAL mode and stores raw measurements, events, forecast
points, hourly rollups and daily rollups. Retention is independently
configurable. The latest cumulative energy counters remain available as restart
baselines.

History accepts `resolution=raw`, `resolution=hourly` or `resolution=daily`.
Service metrics are exposed in Prometheus text format at `/metrics`.

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
- configured inverter charge/discharge power limits;
- usable capacity, reserve SOC and maximum SOC;
- charge and discharge efficiency; and
- configured site import/export boundaries.

Observed BMS limits can only reduce configured power limits. Missing or stale
SOC, authoritative grid power, derived site load or PV forecast produces an
empty `no_action` plan. The optimiser has no serial transport reference and
reports `control_commands_sent: 0` and `execution_available: false`.

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
GET /v1/forecasts/pv
GET /v1/forecasts/pv?since=...&until=...
GET /v1/plans/current
GET /metrics
```

All `POST` requests remain rejected. Phase 7 cannot begin until its separate
hardware-specific write verification, risk assessment and explicit approval
gates have been completed.
