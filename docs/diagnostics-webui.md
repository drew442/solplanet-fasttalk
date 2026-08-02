# Diagnostics web UI

The daemon includes a responsive, read-only diagnostics UI at
`/diagnostics/`. It is designed to answer one operational question:

> What does the daemon know, and what is it doing across the past, present and
> future?

The UI is served by the daemon itself and has no CDN, hosted font, analytics or
other browser-side internet dependency. It works in current phone and desktop
browsers.

All full history and forecast graphs use a consistent, readable height. Hover
the mouse anywhere over a graph, or touch it on a phone, to snap a crosshair to
the nearest recorded or forecast sample. The readout shows that sample's local
timestamp and the actual value for every metric on the graph. If series have
different sampling intervals, the readout identifies a metric whose nearest
sample has a different timestamp. On a touch screen the readout remains open
after a tap; tap another point to move it or tap outside the graph to close it.
The left and right arrow keys move the crosshair between samples when a graph
has keyboard focus, and Escape closes it.

## Information shown

### Present

- authoritative grid import or export;
- derived site consumption, PV generation and self-sufficiency;
- Solplanet battery power and aggregate state of charge;
- a live energy-flow view with freshness and quality;
- the current shadow recommendation, its explanation, expected grid flow and
  expected SOC; and
- the simulated cost improvement over the no-schedule baseline.

### Past

- selectable 6-hour, 24-hour, 7-day and 30-day power charts;
- grid power with positive import and negative export;
- site consumption, external-PV AC and Solplanet AC power;
- battery SOC;
- actual import cost, export revenue and net cost history;
- today and month-to-date energy/cost totals;
- historical PV forecast-versus-actual graphs;
- separate provider and corrected PV forecast traces;
- persisted optimiser decisions and their modelled improvements; and
- recent daemon events.

The browser requests time-bucketed averages from SQLite. It does not transfer
every high-rate raw observation. Each returned bucket also carries its sample
count, minimum and maximum for diagnostics.

### Future

- forecast load and combined east/west PV;
- provider PV, corrected PV and the current long/short correction factors;
- cloud cover, precipitation probability, temperature and independent
  tilted-irradiance PV potential from Open-Meteo;
- expected grid power with the shadow schedule;
- grid and SOC baselines using the inverter's high-level run mode, including
  owner-confirmed recurring native Custom windows and load-following
  self-consumption outside them;
- forecast battery SOC;
- native/no-change and shadow-counterfactual battery SOC trajectories;
- historical load and native-SOC forecast accuracy as actuals accumulate;
- the load forecast's historical 10th–90th percentile range; and
- retained 15-minute training-data coverage;
- future import and export tariff prices; and
- upcoming no-window self-consumption, grid-charge-window and
  export-discharge-window recommendations with their interval prices.

### Workings

The decision pipeline exposes the optimiser's observations, forecast method,
freshness, BMS-derived power limits, configured SOC/site constraints, tariff
period and forecast-versus-authoritative-actual comparison. A missing or stale
required input is shown as a no-action reason rather than hidden.

The forecast panel also shows whether the independent accuracy gate has passed.
Shadow recommendations remain available while the model learns, but this state
is visibly distinct from control readiness. Details are in
[Data quality and forecasting](data-quality-and-forecasting.md).

The baseline is explicitly an estimate, not a claim about hidden inverter
logic. It uses register 41104 for the high-level mode and, in Custom mode,
models configured recurring native windows plus the documented no-window
self-consumption behavior. The workings show whether the native schedule was
confirmed and whether an active window matches 41152/41153 readback. Hardware-limit evidence distinguishes
the ASW12kH-T3 12 kW battery rating from its inapplicable 10-second EPS overload
rating. Detailed evidence is in
[ASW battery operating modes](asw-operating-modes.md).

The UI never implies that a shadow recommendation has been executed. It
repeats that command execution is unavailable and the daemon has sent zero
control commands.

## Local access

Loopback remains the default:

```toml
[api]
host = "127.0.0.1"
port = 8765
auth_token_file = ""
```

Open <http://127.0.0.1:8765/diagnostics/> on the daemon machine. From a
development desktop, retain the loopback bind and use an SSH tunnel:

```console
ssh -L 8765:127.0.0.1:8765 YOUR_PRIVATE_TARGET_ALIAS
```

Then open <http://127.0.0.1:8765/diagnostics/> locally. The tunnel is the
preferred remote-development path.

### Local operator script

From a repository checkout:

```console
./scripts/fasttalk-local.sh start /PRIVATE_CONFIG_DIRECTORY/runtime.toml
./scripts/fasttalk-local.sh status
./scripts/fasttalk-local.sh stop
```

`start` runs the daemon in the background and forces the API to
`127.0.0.1`, even if the supplied configuration names a token. `restart` is
also available. The script records the PID, Linux process-start identity and
access mode in a private runtime directory. It will not start a second daemon,
stop LAN mode through the local script, or force-kill a process that does not
shut down within 30 seconds.

## Opt-in phone or LAN access

Listening beyond loopback is an explicit deployment choice. Create a
target-only token with at least 32 random, non-whitespace characters:

```console
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
  > /PRIVATE_CONFIG_DIRECTORY/diagnostics-api.token
chmod 600 /PRIVATE_CONFIG_DIRECTORY/diagnostics-api.token
```

Do not put this token in the repository or the ordinary TOML file. Configure:

```toml
[api]
host = "0.0.0.0"
port = 8765
auth_token_file = "/PRIVATE_CONFIG_DIRECTORY/diagnostics-api.token"
```

After restart, open `http://DAEMON_LAN_ADDRESS:8765/diagnostics/`. The UI asks
for the token and keeps it in that browser tab's session storage. It does not
put the token in a URL, cookie, log or persistent local storage.

The bearer token protects access but plain HTTP does not encrypt it. Direct
LAN mode is appropriate only on a trusted, isolated network. Use a TLS reverse
proxy or private VPN for any untrusted network, and never expose this service
directly to the public internet. A Home Assistant OS add-on container may also
need an explicitly published port or ingress configuration; changing HAOS
network/add-on settings remains a separate reviewed operation.

Non-loopback startup fails closed when the token is absent, shorter than 32
characters, contains whitespace, is unreadable or is accessible to group/other
users.

### LAN and token operator scripts

The token script creates an atomic mode-`0600` secret. Creation reports only
the path; displaying the credential is a separate explicit action:

```console
./scripts/fasttalk-token.sh create
./scripts/fasttalk-token.sh status
./scripts/fasttalk-token.sh show
```

After copying the displayed token into the browser:

```console
./scripts/fasttalk-lan.sh start /PRIVATE_CONFIG_DIRECTORY/runtime.toml
./scripts/fasttalk-lan.sh status
./scripts/fasttalk-lan.sh stop
./scripts/fasttalk-token.sh destroy
```

LAN mode forces the API bind to `0.0.0.0`, validates the token through the
daemon's normal configuration safety checks, and refuses to start without it.
The token cannot be destroyed while a script-managed LAN daemon is running.
The scripts also refuse to interfere with a daemon started manually or by a
service manager.

Local and LAN scripts share one mode/PID state, so switching modes is explicit:
stop the current mode, then start the other. These scripts are an alternative
to a systemd or HAOS add-on service, not a wrapper around one.

Defaults can be changed without editing the scripts:

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `SOLPLANET_FASTTALK_CONFIG` | base daemon TOML | `/etc/solplanet-fasttalk.toml`, then the repository example |
| `SOLPLANET_FASTTALK_BIN` | installed daemon executable | discovered from `PATH`, the HAOS test venv or a checkout venv |
| `SOLPLANET_FASTTALK_TOKEN_FILE` | private token path | `$XDG_CONFIG_HOME/solplanet-fasttalk/diagnostics-api.token` |
| `SOLPLANET_FASTTALK_STATE_DIR` | PID/mode runtime state | `$XDG_RUNTIME_DIR/solplanet-fasttalk-run`, otherwise `/tmp/solplanet-fasttalk-run` |
| `SOLPLANET_FASTTALK_LOG_FILE` | background daemon log | `daemon.log` inside the runtime-state directory |
| `SOLPLANET_FASTTALK_API_PORT` | local/LAN HTTP port | `8765` |

An optional config path given after `start` or `restart` takes precedence over
`SOLPLANET_FASTTALK_CONFIG`. No token value, private path or runtime state is
committed to the repository.

## Read-only API additions

```text
GET /v1/diagnostics
GET /v1/weather
GET /v1/measurements/history?name=...&since=...&until=...&bucket_seconds=...
GET /v1/tariffs/forecast?hours=...&step_minutes=...
GET /v1/financials/history?since=...&until=...&bucket_seconds=...
GET /v1/financials/summary?since=...&until=...
GET /v1/plans/history?since=...&until=...
```

`/v1/diagnostics` returns one coherent browser refresh payload containing the
plant, current measurements, health, devices, capabilities, current tariff,
forecast, plan and recent events. It contains no Forecast.Solar API key or
plant coordinates.

When `auth_token_file` is configured, bearer authentication applies to the
versioned API, service root, event stream and Prometheus metrics. Static UI
assets remain retrievable so the browser can render the token prompt; plant
data remains inaccessible until authentication succeeds.

## Browser and accessibility behaviour

- layouts collapse to a compact single-column phone view;
- navigation, range controls and the token dialog are keyboard accessible;
- charts carry descriptive labels and the key values remain available as
  ordinary text;
- reduced-motion browser preferences disable non-essential animation; and
- a restrictive content security policy permits only same-origin assets and
  API calls.

The UI deliberately has no write controls. Every API `POST` request continues
to return `405 Method Not Allowed` in shadow mode.
