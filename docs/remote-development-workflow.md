# Safe remote development workflow

This workflow permits rapid remote development against the HAOS test plant
while keeping live hardware access inside a deliberately narrow safety
envelope. It applies to the current read-only daemon milestone.

Remote root access is useful for initial setup, recovery and inspecting the
HAOS SSH add-on container. It is not the normal privilege level for the
daemon, and it does not authorize inverter control.

## Safety claim and boundary

No remote workflow connected to physical equipment can honestly promise
absolute zero risk. A process can still consume CPU, fill storage, hold a
serial port or generate too many otherwise valid read requests. The current
boundary instead makes equipment-setting changes structurally unavailable and
keeps the remaining availability risks bounded and observable:

- the terminal-8 connection is physically receive-only;
- the Eastron descriptor is opened `O_RDONLY` and the integration has no
  transmit path;
- ASW MONITOR permits only Modbus functions `0x03` and `0x04`;
- the optional direct Solis plugin permits only Modbus function `0x04`;
- no daemon control endpoint or Modbus write builder exists;
- the ASW polling schedule, retry timeout and backoff are bounded;
- every serial port is exclusively locked to prevent two daemon owners;
- the API binds to loopback and reports that control is unavailable; and
- stopping the daemon leaves the inverter, meter and native Solplanet system
  operating independently.

The Forecast.Solar worker adds a bounded outbound HTTPS dependency, but only
to forecasting. Failure or stale cache state produces a conservative
no-action shadow plan and cannot stop either serial worker.

Within this boundary, development, deployment, read-only diagnostics and soak
testing may proceed autonomously. Future writes are governed by the
[Modbus write safety policy](modbus-write-safety.md): permanently prohibited
operations cannot be approved, and controlled operations require a complete
risk assessment and explicit approval. Firmware operations, device
configuration outside that policy, USB resets, wiring changes and host-level
HAOS changes also require a separate reviewed procedure.

## Access architecture

Use this path:

```text
development workspace
        │
        │ SSH over an existing private VPN
        v
HAOS SSH add-on container
        │
        ├── untracked runtime configuration and history
        ├── receive-only SH-U11F access to terminal 8
        └── read-functions-only Waveshare access to ASW MONITOR
```

Do not expose SSH or the unauthenticated API directly to the public internet.
Do not grant access to the HAOS host shell, Docker socket or Supervisor token
merely to run this daemon. Root inside the SSH add-on container is already
more authority than the runtime needs and should not be expanded.

### Dedicated SSH identity

Use a dedicated Ed25519 key that can be revoked without affecting the owner's
normal administration access. Do not send a password or private key through
chat and do not store either key in this repository.

The operator installs only the public key on the target. Where the SSH server
supports it, apply at least:

```text
restrict,no-agent-forwarding,no-X11-forwarding,no-port-forwarding
```

Pin the target host key and place the target hostname, VPN address, username
and private-key path in the development machine's `~/.ssh/config`, for example:

```sshconfig
Host solplanet-fasttalk-test
    HostName PRIVATE_VPN_NAME_OR_ADDRESS
    User root
    IdentityFile PRIVATE_KEY_PATH
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

The real values remain outside the repository. A stable local alias lets
automation use `ssh solplanet-fasttalk-test ...` without disclosing private
network information in scripts, commits or logs.

Root access should initially be retained only because the present test
environment is an HAOS SSH add-on container. The next packaging step should
run the daemon as a dedicated unprivileged account with access only to its two
USB devices and persistent data directory.

## Target layout

Keep code, state and private configuration separate:

```text
checkout/                         disposable Git checkout
venv/                             disposable Python virtual environment
persistent-data/history.sqlite3   persistent measurements
private-config/runtime.toml       untracked, mode 0600
private-config/forecast-solar-api.key  untracked, mode 0600
private-logs/                     untracked live-test logs
```

The exact absolute directories are target-local configuration. Confirm which
HAOS add-on directories survive an add-on update before relying on them.
Never put the following in the checkout:

- site address, coordinates or identifiable plant name;
- VPN address, SSH private key or host inventory;
- Forecast.Solar or electricity-provider credentials;
- Wi-Fi, dongle, Home Assistant or Supervisor credentials;
- inverter, battery, storage-device or filesystem serials and UUIDs; or
- raw operational captures intended only for local analysis.

FTDI USB serial identifiers are approved for tracked configuration examples.
The ignored `discovery-output/` directory may hold private operating evidence
locally, but each file must still be reviewed before it is deliberately
shared.

## Permission envelope

Once the operator explicitly adopts this workflow, the following work is
pre-authorized within the read-only milestone:

- inspect processes, logs, disk space, USB enumeration and serial symlinks;
- fetch a reviewed Git revision into the daemon checkout;
- create or replace the project virtual environment;
- install the local checkout into that virtual environment;
- run unit, replay and configuration tests;
- start, stop and restart only the `solplanet-fasttalk` process;
- query the loopback read-only API;
- run passive Eastron capture and bounded ASW read-only discovery tools already
  reviewed in this repository;
- collect health, timing and data-quality statistics; and
- copy privacy-reviewed results into `discovery-output/`.

The following require explicit operator approval for each procedure:

- any operation classified `approval_required` by the Modbus safety policy;
- direct transmission on terminal 8;
- changing a baud rate, slave address, polling profile or sign convention;
- attaching, removing, rewiring or terminating an RS-485 connection;
- stopping native Solplanet, meter or Home Assistant services;
- rebooting HAOS or changing Supervisor, add-on, firewall, VPN or USB settings;
- installing target operating-system packages;
- deleting databases, captures or Home Assistant data; and
- enabling API access beyond loopback.

An unexpected need for one of these actions is a stop condition, not permission
to improvise.

Operations classified `permanently_prohibited` or `unreviewed_deny` cannot be
made permissible by a per-operation approval.

## Deployment loop

Each remotely deployed revision follows the same progression.

### 1. Prove it away from hardware

In the development workspace:

1. inspect the diff and ensure it contains no private data;
2. run the complete test suite;
3. replay saved, sanitised captures;
4. confirm the Eastron modules still contain no transmit path;
5. confirm the active RTU builder still rejects non-read functions; and
6. identify the exact Git commit intended for the target.

Do not edit live target code as the primary development method. The target
should run a reproducible Git revision so it can be rolled back.

### 2. Prepare the target without hardware

Use a virtual environment rather than Alpine's externally managed system
Python:

```console
python3 -m venv venv
venv/bin/python -m pip install .
venv/bin/python -m solplanet_fasttalk --version
venv/bin/python -m unittest discover -s tests -v
```

There is no command named `src`; `src/` is the package source directory.

First run `check-config`, then start once with both hardware integrations
disabled. Verify database creation, clean shutdown and loopback API behaviour.

### 3. Replay before live serial access

Replay the known Eastron capture and compare transaction count, decoded phase
totals and signs with the established fixture. A replay regression blocks the
live deployment.

### 4. Enable one physical boundary at a time

Use this order:

1. passive Eastron only;
2. ASW MONITOR only;
3. both authoritative integrations together;
4. optional direct Solis reads;
5. Forecast.Solar and shadow optimisation; and
6. a time-bounded canary before a longer soak.

Before each stage, verify that no other development process owns the relevant
adapter and that the configuration resolves to the approved by-ID path.
Starting a second daemon must fail on the exclusive serial lock.

### 5. Apply health gates

A live canary passes only when:

- the native Solplanet app and ASW show both meters online;
- native import/export behaviour remains normal;
- ASW reads succeed without reconnect loops;
- Eastron CRC, unmatched-response, missing-response and exception counts
  remain zero or at their documented stream-start baseline;
- no measurement required for plant flow is stale;
- grid, external-PV, ASW and derived site power reconcile physically; and
- storage queue depth, dropped measurements and write failures remain zero;
  and
- the shadow planner reports zero control commands and respects observed BMS
  and configured site limits.

Stop the new revision and return to the last known-good revision if a native
meter goes offline, ASW reads repeatedly fail, traffic shape changes
unexpectedly, error counters rise persistently, storage fills, CPU load
threatens HAOS, or plant values become physically implausible.

### 6. Preserve evidence and roll back atomically

Record the deployed Git commit, start/end time, daemon version and summarized
health counters. Do not record hostnames, addresses, secrets or non-approved
serial identifiers.

Keep the previous known-good environment until the canary passes. Rollback
means stopping the new process and starting the previous exact revision with
the same private configuration and database. Database migrations must not be
introduced until backup and backward-compatibility handling exist.

## Runtime supervision and emergency stop

Only one named daemon instance should exist. A supervisor must:

- send `SIGTERM` for normal shutdown;
- wait for serial descriptors and SQLite to close;
- avoid an unlimited rapid-restart loop;
- retain a small bounded log;
- start only after the configured USB by-ID paths exist; and
- leave the process stopped after repeated startup or communication failures.

The emergency action for the read-only milestone is simply to stop the daemon.
It must never require an inverter command to restore normal plant operation.
After stopping, verify the process has exited, the serial descriptors are
closed and the native ASW/meter display remains online.

## HAOS progression

The SSH add-on container is suitable for short canaries and discovery, but it
is not the final deployment architecture. Development should progress through:

1. the current virtual-environment foreground/canary workflow;
2. a supervised read-only process with persistent state and bounded logs;
3. a native Home Assistant add-on with explicit USB-device mapping,
   unprivileged execution and health checks; and
4. only after the read-only daemon and shadow optimiser are mature, a
   separately reviewed control-capable build.

The read-only and control-capable builds should remain separable. Installing a
new optimisation algorithm must not implicitly add Modbus write capability to
the live plant.

## Future control work

Root access is not permission to test control. Control development begins in
simulation and shadow mode. Physical commands must first pass the
[Modbus write safety policy](modbus-write-safety.md) and require all of the
following:

- a separately reviewed write allow-list for the exact ASW model and firmware;
- hard-coded power, SOC, rate and duration bounds;
- freshness interlocks for authoritative grid power, ASW state and SOC;
- read-after-write verification and a durable command audit;
- a local manual automation-disable control;
- deterministic loss-of-daemon and loss-of-communications behaviour;
- a supervised test window with the operator present; and
- explicit approval of the exact proposed command sequence.

Automatic control is not enabled merely because supervised control tests pass.
