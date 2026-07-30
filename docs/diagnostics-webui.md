# Diagnostics web UI

The daemon includes a responsive, read-only diagnostics UI at
`/diagnostics/`. It is designed to answer one operational question:

> What does the daemon know, and what is it doing across the past, present and
> future?

The UI is served by the daemon itself and has no CDN, hosted font, analytics or
other browser-side internet dependency. It works in current phone and desktop
browsers.

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
- battery SOC; and
- recent daemon events.

The browser requests time-bucketed averages from SQLite. It does not transfer
every high-rate raw observation. Each returned bucket also carries its sample
count, minimum and maximum for diagnostics.

### Future

- forecast load and combined east/west PV;
- expected grid power with the shadow schedule;
- the grid baseline without that schedule;
- forecast battery SOC; and
- the upcoming charge, discharge and hold recommendations.

### Workings

The decision pipeline exposes the optimiser's observations, forecast method,
freshness, BMS-derived power limits, configured SOC/site constraints, tariff
period and forecast-versus-authoritative-actual comparison. A missing or stale
required input is shown as a no-action reason rather than hidden.

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

## Read-only API additions

```text
GET /v1/diagnostics
GET /v1/measurements/history?name=...&since=...&until=...&bucket_seconds=...
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
