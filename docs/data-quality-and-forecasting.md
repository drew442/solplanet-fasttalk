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

This is intentionally simple and inspectable. More features should be added
only after their out-of-sample contribution is measured.

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
