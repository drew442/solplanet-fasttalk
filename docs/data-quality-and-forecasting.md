# Data quality and forecasting

The daemon treats forecast accuracy as a measured safety property, not an
assumption. Forecasting and optimisation remain read-only shadow functions.
Passing the forecast gate does not make Modbus writes available and cannot
bypass the separate phase-7 risk assessment and explicit approval process.

## Initial collected-data audit

The first audit covered the available SQLite history through 1 August 2026.
It found two plant-model defects and no evidence that the authoritative PV
meter reported production at night.

| Check | Result | Interpretation |
| --- | ---: | --- |
| Authoritative external-PV samples | 13,065 | Enough to validate sign, night behaviour and the direct-inverter comparison; not enough for seasonal forecasting |
| External PV during local 18:00–05:59 | exactly 0 W | The Eastron aggregate was not the source of the night-PV display |
| ASW dedicated PV register | 0 W throughout the audited data | Consistent with no PV connected to the ASW |
| Common Eastron/Solis daylight minutes | 515 | Independent comparison set |
| Eastron versus direct Solis correlation | 0.99998 | Excellent tracking |
| Eastron versus direct Solis mean absolute difference | about 47 W | About 1.9% median absolute percentage difference over the comparison set |

The false night production came from `site.generation_power`: it added positive
ASW AC power even though that signal includes battery discharge. Production is
now strictly the non-negative external-PV aggregate plus the dedicated ASW PV
register. Battery discharge is instead represented as local supply.

A second defect calculated a non-negative site load but persisted the
unclipped intermediate value. The persisted value is now the clipped physical
result. Historic negative `site.load_power` records remain in SQLite as an
auditable record, but the load-learning model rejects them.

Small signed reverse power values can occur on an inverter feeder around dusk
because of standby consumption, CT uncertainty or meter noise. Signed phase
and meter-total registers remain available for diagnostics. Only the
plant-facing `external_pv.active_power` aggregate is bounded at zero, so these
readings cannot train a negative-generation model.

The initial raw Forecast.Solar comparison had approximately 1–2 days of data.
Across 1,283 available daylight issue/target pairs its mean error
(`actual - forecast`) was about -743 W, MAE about 1,088 W and RMSE about
1,248 W. Those figures show an initial high provider bias, but repeated
issuances for the same target are not independent and the period is much too
short to certify accuracy. They are a starting observation only.

## PV forecast model

Forecast.Solar remains the base PV model because it already combines array
geometry and weather. Fasttalk applies a causal correction using only actuals
that existed when the correction was issued:

1. The astronomical solar elevation is calculated for every target time.
   Forecast power is forced to zero at or below -0.833 degrees, independently
   of provider timestamps or browser display logic.
2. A robust long-term ratio compares authoritative Eastron AC production with
   the base forecast over a rolling 60-day window. Ratios are bounded before a
   median is taken, limiting the effect of outages, clipping and bad points.
3. The global long-term factor is used until at least 250 usable samples across
   14 distinct days exist. Once mature, two-hour local-time factors are learned
   where at least 40 samples across seven days exist. This captures different
   east-array and west-array bias while retaining a global fallback.
4. A separate short-term residual uses the preceding 120 minutes. An
   exponentially weighted median requires at least four usable points and is
   bounded to avoid reacting violently to one transient.
5. The short-term residual decays over a two-hour horizon. Open-Meteo cloud and
   irradiance context reduces its persistence when the forecast weather differs
   from current conditions.
6. Output is bounded by zero and 105% of configured aggregate array nameplate.
   Every point exposes its base power, corrected power, long and short factors,
   daylight decision and weather context.

Open-Meteo is also stored as an independent theoretical tilted-irradiance PV
potential. It is displayed alongside the corrected/provider forecast, but is
not blended into dispatch merely because it is available. A future ensemble
should earn its weight through out-of-sample accuracy rather than assume a
second cloud model is better.

## Load forecast model

Site consumption uses a separate two-timescale model:

- the long-term component is a median of valid 15-minute
  `site.load_power` observations for the matching local weekday and hour over
  the preceding 35 days;
- a bucket needs at least three observations or the fresh current load is used;
- negative values and values above the configured physical audit ceiling are
  excluded;
- the short-term factor compares the most recent 30 minutes with the applicable
  long-term bucket, requires three samples and is bounded to 0.5–1.5; and
- the short-term factor decays over four hours.

The long-term key is now the local weekday and exact 15-minute position rather
than a whole-hour average. A bucket requires four historical observations. Its
median is the point forecast and its 10th/90th percentiles form a visible
uncertainty interval. Up to 730 days are considered when retained data exists.

This is intentionally simple and inspectable. More features should be added
only after their out-of-sample contribution is measured.

## Site-load and battery-SOC prediction records

Every successful shadow-plan run now persists versioned prediction vintages
for each target interval:

| Signal | Scenario | Scoreable against current actuals? |
| --- | --- | --- |
| `site.load_power` | `expected` | yes |
| `battery.soc` | `native_no_change` | yes |
| `battery.soc` | `shadow_counterfactual` | no, because the proposed policy was not executed |

The native SOC forecast continues the inverter's observed native command until
its observed SOC bound. Its subsequent error measures how adequate that
no-change assumption is. The shadow SOC forecast uses planned battery power,
capacity, efficiency, reserve, maximum SOC, manufacturer limits and live BMS
limits. It is retained for simulation and later policy evaluation, but is
explicitly protected from invalid comparison with native actual operation.

The shadow SOC trace includes a conservative accumulating lower/upper range
derived from the site's P10–P90 load uncertainty and charge/discharge
efficiency. It is labelled as load-driven uncertainty: PV forecast uncertainty
is not yet included, so this range must not be interpreted as a calibrated SOC
confidence interval. Persisted outcomes will allow that interval to be
calibrated later.

Each prediction point records the model and version, issue and target times,
lead time, point value, optional uncertainty interval, scenario and unit. Its
causal feature snapshot includes:

- current SOC and grid state at issue time;
- forecast load, load interval and historical sample count;
- corrected and provider PV forecasts;
- sanitized cloud, irradiance, temperature and precipitation context;
- applicable import/export prices and local calendar position;
- proposed action and battery/site constraints; and
- native operating-mode assumption and forecast-quality state.

No future actual value is copied into a feature record. Actuals are joined only
when accuracy is queried, preventing training leakage.

## Model-ready historical retention

High-frequency raw measurements remain bounded to 14 days. A selected set of
plant, battery, BMS-limit and operating-mode signals is additionally retained
at 15-minute resolution for 800 days. Rollups retain sample count, mean,
minimum, maximum, last value, unit, quality, source and authority. This provides
seasonal history without retaining every one-second Modbus observation.

Sanitized Open-Meteo forecast vintages are retained separately for 800 days,
including the weather prediction as it existed at each hourly archive time for
the operational -1 to +72-hour window. Load and SOC prediction vintages are
retained for 800 days. Grid/battery observations, tariff intervals, complete
plan decisions, PV forecast vintages and observed outcomes remain joinable by
aware UTC timestamps.

Complete shadow plans are still generated on their operational schedule. Plan
history retains every status/current-action transition plus an unchanged
three-hour checkpoint. The separate load/native-SOC/shadow-SOC prediction
vintages are also archived every three hours. With 15-minute targets this still
yields dense samples in every decision horizon without duplicating every
unchanged five-minute plan into hundreds of millions of rows. Both archives
have an 800-day retention boundary.

The coverage API reports counts and date spans without returning private values:

```text
GET /v1/training/coverage
GET /v1/predictions/history?signal=site.load_power&scenario=expected
GET /v1/predictions/quality?signal=site.load_power&scenario=expected
GET /v1/predictions/quality?signal=battery.soc&scenario=native_no_change
```

Prediction quality includes lead-time-bucket sample count, MAE, RMSE, bias,
weighted absolute percentage error and uncertainty-interval coverage. Dataset
readiness requires 28 distinct days and 300 matched samples in each of the
0–2, 2–8 and 8–24-hour bands. Readiness means there is enough coverage to
evaluate a model; it does not mean the model is accurate or safe for control.

This dataset supports future regression, probabilistic forecasting or ML, but
the daemon does not assume that an AI model will outperform the transparent
baseline. Candidate models must use chronological train/validation/test splits,
preserve forecast issue time, compare against the baseline by horizon and
weather regime, and pass the same independent safety gates.

## Independent accuracy gate

Every base and corrected forecast issuance is retained, rather than retaining
only the latest forecast for a target. Corrected forecasts are scored against
authoritative actual PV in four lead-time bands:

- 0–2 hours;
- 2–8 hours;
- 8–24 hours; and
- more than 24 hours (reported, but not currently required by the 36-hour
  planner gate).

The forecast is marked `control_ready` only after all of the following hold:

- at least 28 distinct scored days;
- at least 300 daylight samples in each of the first three lead-time bands;
- normalized MAE no greater than 15% of array nameplate in every required
  band; and
- absolute normalized bias no greater than 8% in every required band.

This gate is deliberately stricter than the calibration readiness threshold.
It prevents the data used to begin learning from being mistaken for evidence
that the corrected model generalizes. The diagnostics UI shows the learning
state, correction factors, provider and corrected traces, weather, horizon
metrics and whether the gate has passed.

## Private location handling

The location is target-only sensitive data. The repository contains no plant
coordinates. Forecast and weather workers read latitude/longitude from a
private runtime file into memory. They never put them in TOML, SQLite, cache
files, health/events, logs, API responses or diagnostics payloads.

Both Forecast.Solar and Open-Meteo necessarily receive the location in an HTTPS
request in order to produce site-specific forecasts. Enabling either service
is therefore an explicit disclosure to that provider. Disabling the weather
worker removes the Open-Meteo disclosure without affecting local measurement
collection; cached weather contains only timestamps and forecast values.

## Confidence-scaled export reserve

Forecast maturity is no longer treated as an all-or-nothing waiting period.
The shadow planner calculates an inspectable confidence score from independent
day diversity, sample coverage in each lead-time band, 8–24-hour load error,
load interval calibration, and PV error/bias. The weaker of load and PV
confidence controls the result.

At zero confidence, daemon-proposed export discharge preserves the configured
`untrusted_reserve_soc_percent` (85% by default). As evidence improves, the
effective reserve continuously falls toward `reserve_soc_percent`. This
reserve applies only to the proposed shadow trajectory: it neither changes the
owner's native inverter configuration nor changes the no-daemon baseline.

The initial economic stage is cost-neutral-first. Under the archived tariff,
earning the daily ZEROHERO credit leaves $0.65 of the $1.65 supply charge to
cover, equivalent to about 4.33 kWh at the 15-cent premium export rate before
any import cost. The optimiser may release only the energy permitted by the
confidence-scaled reserve. Increasing confidence therefore creates a gradual
path from covering fixed daily cost toward profit rather than a sudden control
threshold.

`forecast_confidence_full_days` defaults to 84. Repeated forecast vintages do
not substitute for independent days, and accuracy evidence still affects the
score throughout this period. The plan exposes the component scores, effective
reserve, economic stage and cost-neutral reference calculation.

## Required shadow validation before writes are considered

Keep the daemon read-only until at least:

1. the 28-day forecast gate passes across all required horizons;
2. night-time PV remains zero after the daylight and aggregate bounds;
3. Eastron/Solis agreement remains within the configured sustained tolerance;
4. forecast error is reviewed across clear, cloudy, rainy and mixed days;
5. load error is separately scored for ordinary weekdays and weekends;
6. missing provider/weather data is shown to degrade gracefully without
   changing authoritative current measurements; and
7. replayed shadow recommendations remain safe and economically useful under
   pessimistic PV and load error scenarios.

Passing these checks is evidence for starting a separate control design
review. It is not approval to write any inverter register.

## Version 0.5.0 live canary

The HAOS read-only canary on 1 August 2026 confirmed:

- Eastron, ASW, Solis, weather, Forecast.Solar, optimisation, accounting,
  storage and API components all reached `ok`;
- Open-Meteo returned 264 sanitized hourly/past points and Forecast.Solar
  returned 180 corrected points;
- weather context was active in the correction model after deterministic cache
  startup ordering;
- no forecast point below the astronomical daylight threshold had non-zero PV;
- live ASW PV remained 0 W and `site.pv_generation_power` matched the
  authoritative external-PV aggregate rather than battery discharge;
- the model correctly reported only three long-term learning days and refused
  control readiness for insufficient independent history;
- the optimizer produced a ready shadow plan while still reporting zero
  control commands, no execution capability and a failed forecast control
  gate; and
- the weather cache contained no location fields and persisted forecast
  metadata contained no latitude/longitude terms.

The first, aborted canary attempt also exposed SQLite maintenance rebuilding
the entire raw window inside a write transaction. The previous writer discarded
eight individual telemetry insert attempts while that lock was held; this is a
known short gap and those records cannot be reconstructed. Version 0.5.0 was
corrected so maintenance
starts after initialization, processes only the incomplete rollup boundary and
new periods, and measurement writes use atomic batches with bounded retry. The
catch-up maintenance pass caused one successfully retried lock collision; the
queue drained to zero with no dropped records, no maintenance failure and
healthy storage. Subsequent steady-state maintenance should touch only the
latest boundary and is retained as a soak-test observation.

## Version 0.6.0 load/SOC canary

The HAOS read-only canary on 1 August 2026 confirmed that the live plan exposed
site-load point/P10/P90 values, native SOC, shadow SOC and an accumulating
load-driven shadow-SOC range for every one of its 47 available target slots.
It archived 141 prediction rows: 47 each for load, native SOC and shadow SOC.
The first actual targets subsequently produced matched, scoreable load and
native-SOC errors, while the API correctly refused to score counterfactual
shadow SOC.

The first 15-minute maintenance pass created 1,762 model-ready rollups across
17 selected signals and archived 73 sanitized -1 to +72-hour weather-context
points. Privacy inspection found zero latitude/longitude terms in prediction
features, prediction metadata or weather-context rows.

An initial implementation performed the historical aggregation while holding
SQLite's write lock. The canary was stopped before the bounded queue filled,
but timestamp continuity shows an approximately 200-second gap in core signals
from that aborted run. The aggregation was changed to compute under a WAL read
snapshot, then insert in 200-row committed slices. The repeated live pass
completed with all components healthy, 11,941 new measurements written, zero
write failures, zero queue drops, an empty queue and one successful maintenance
run. The authenticated daemon was left running with the bounded implementation.
