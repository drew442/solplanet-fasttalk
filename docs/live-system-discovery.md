# Live system discovery

This runbook gathers read-only Modbus RTU evidence from the initial test plant:

- Solis-10K through its dedicated Waveshare USB-RS485 adapter; and
- Solplanet ASW12kH-T3 through the MONITOR port and its dedicated Waveshare
  USB-RS485 adapter. Discovery began on pins 7–8; pins 1–2 are now the
  preferred third-party connection; and
- Eastron SEM3-M-2L-CT terminal-8 traffic through the receive terminals of an
  SH-U11F operating as a passive RS-422 listener.

The baseline and CT-data stages do **not** query the Eastron meter directly.
Stage 6 adds a narrowly scoped read-only meter-route hypothesis recovered from
the Ai dongle firmware. It uses the existing MONITOR-port connection and does
not require touching the live terminal-8 wiring. Stage 7 reads the dongle's
configured meter address and performs a bounded, read-only MONITOR-bus slave
scan.

## Safety properties

[`tools/live_modbus_discovery.py`](../tools/live_modbus_discovery.py) has a
deliberately narrow protocol surface:

- it implements only function `0x04` (read input registers) and function
  `0x03` (read holding registers);
- it contains no write-register, write-coil, arbitrary-frame, or generic
  register-read command;
- its device profiles and addresses are fixed in source;
- its optional slave scan uses only two fixed two-register function-`0x04`
  signatures, defaults to addresses `1`–`16`, and never writes;
- the ASW inverter serial-number range `31003`–`31018` is not queried;
- the experimental ASW `CT Data` ranges are disabled unless `--extended` is
  supplied; and
- the first validation read must succeed before the rest of a profile is sent.

Reading an RW holding register with function `0x03` does not write it. The ASW
smart-meter and control-state groups use `0x03` only to observe their current
values.

Do not run this tool while another process is using the same USB adapter. Do
not connect either Waveshare adapter to the live ASW–Eastron terminal-8 bus.
Two physical masters on that bus could collide and interfere with export
control. The meter-tunnel profile instead sends the same bounded read frames
used by the Ai dongle through the existing MONITOR-port adapter.

## Requirements

- A Linux machine with Python 3.9 or newer.
- A local checkout or copy of this repository.
- Read/write permission for the relevant serial device.
- The existing dedicated Waveshare connections to the inverter ports.

No Python packages are required; the discovery tool uses only the standard
library.

Check Python and locate the adapters:

```console
python3 --version
python3 tools/live_modbus_discovery.py list
```

Prefer paths under `/dev/serial/by-id/` because `/dev/ttyUSB0` numbering can
change after a reboot. If no by-ID paths exist, `/dev/ttyUSB0` is acceptable
for the initial one-adapter-at-a-time identification.

If opening a port reports `Permission denied`, add the operator to the serial
device's group (commonly `dialout`) and then log out and back in:

```console
ls -l /dev/ttyUSB0
sudo usermod -aG dialout YOUR_LOGIN_NAME
```

Do not solve a permission problem by running an unreviewed third-party Modbus
tool as root.

## Stage 1: identify the adapters

If the adapters are not already unambiguously labelled, leave only the Solis
adapter connected to USB and run:

```console
python3 tools/live_modbus_discovery.py list
```

Record its `/dev/serial/by-id/...` path and label the physical adapter. Repeat
with only the ASW adapter connected. Once identified, both may remain
connected.

Before probing, check for likely serial-port users:

```console
ps -ef | grep -E 'mbpoll|modpoll|pymodbus|ttyUSB|ttyACM' | grep -v grep
```

Stop only the known application or service that owns the relevant adapter.
Do not terminate an unfamiliar process merely because it appears in this
list.

## Stage 2: Solis baseline

The supplied Solis protocol documents function `0x04`, slave address `1`,
9600 baud, 8 data bits, no parity and one stop bit. Replace the example device
path with the path found above:

```console
mkdir -p discovery-output
python3 tools/live_modbus_discovery.py probe solis \
  --device /dev/serial/by-id/REPLACE_WITH_SOLIS_ADAPTER \
  --output discovery-output/solis-baseline.json \
  --verbose
```

A successful first group should report product, DSP and LCD codes plus current
active/DC power. The remaining groups capture energy, DC inputs, per-phase AC
measurements, temperature, frequency and inverter state.

If the identity read fails, stop and retain the JSON output. Check the adapter
mapping, device permissions and A/B wiring before trying other addresses or
serial settings.

## Stage 3: ASW baseline

Live discovery confirmed that the test ASW uses slave address `3` at
9600-8-N-1. The test connection is the working MONITOR-port pins 7 and 8
connection, not terminal 8 and not the smart-meter cable.

Run the baseline at the confirmed baud:

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --output discovery-output/asw-baseline.json \
  --verbose
```

Earlier tests at 38400 and 19200 produced no response. Do not repeat them
unless the inverter communication configuration or firmware has changed. Do
not scan arbitrary baud rates.

The successful ASW baseline captures:

- model, firmware and rated power without reading the inverter serial number;
- inverter state, power, energy and fault words;
- aggregate battery voltage, current, power, temperature, SOC, SOH and BMS
  current limits for the three parallel Ai-HB G2 stacks;
- the documented per-phase grid values;
- smart-meter online state and current smart-meter power; and
- the configured charge/discharge state and limits, using reads only.

The live firmware differs from the V2.1.4 register document in several places.
The discovery profile incorporates the observed behavior:

- device type is a one-register ASCII string (`"3"` for three-phase);
- inverter energy today and total are signed net-energy counters;
- battery SOC is expressed in whole percent;
- battery SOH is also observed as a whole-percent value (`100` for the test
  battery), despite the document's `0.01` multiplier;
- grid-side phase active power is signed; and
- `0x8000`/`0x80000000` signed NaN values are reported as unavailable.

## Stage 4: ASW mirrored meter/CT-data read

Only after the normal ASW profile succeeds, repeat it once with the V2.1.4
`CT Data` ranges enabled:

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --extended \
  --output discovery-output/asw-ct-split.json \
  --verbose
```

These registers are marked RW in the vendor document, but the tool only reads
them with function `0x03`. The original single 51-register request returned
exception `0x02`, which could have been caused by any one unsupported address.
Version 0.2 therefore uses eight semantic ranges:

- `46401–46406`: phase voltage and current;
- `46407–46412`: phase active power;
- `46413–46418`: phase apparent power;
- `46419–46424`: phase reactive power;
- `46425–46433`: phase factor, angle and averages;
- `46434–46442`: system totals and frequency;
- `46443–46450`: energy; and
- `46451`: an individually isolated, model-dependent register.

Live captures on both MONITOR pin pairs established the support boundary:
`46401–46450` can be read, while `46451` consistently returns exception
`0x02`. More importantly, `46401–46412` contain live per-phase grid-meter
voltage, current and active power. The phase active-power sum closely follows
the normal smart-meter aggregate:

| Capture | Phase-power sum | Smart-meter power | Difference |
| --- | ---: | ---: | ---: |
| Pins 7–8, importing | 7428 W | 7517 W | -89 W |
| Pins 1–2, exporting | -2297 W | -2276 W | -21 W |

The direction change and close agreement cannot come from the ASW's own AC
port, which was approximately idle, or from terminal-10 CTs, which are not
installed. These registers therefore mirror the terminal-8 Eastron's grid
measurement through the ASW. Positive is observed during grid import and
negative during export.

The remaining fields are not all useful on this model. Apparent/reactive
power and energy were zero; several factor/angle values were NaN or invalid;
and the reported total system power did not track the live phase sum. Treat
only the independently correlated fields as confirmed. There is still no
evidence that this block exposes the Eastron's second, Solis-connected
measurement channel. Do not attempt to populate any RW register.

## Stage 5: correlated plant capture

Run the two commands in separate terminals so their UTC timestamps overlap.
The following collects 60 samples from each device at approximately
five-second intervals:

```console
python3 tools/live_modbus_discovery.py probe solis \
  --device /dev/serial/by-id/REPLACE_WITH_SOLIS_ADAPTER \
  --samples 60 \
  --interval 5 \
  --output discovery-output/solis-correlation.json
```

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --samples 60 \
  --interval 5 \
  --output discovery-output/asw-correlation.json
```

During the capture, make a separate note of:

- local start and finish times, including the time-zone offset;
- weather/PV conditions;
- whether the battery is charging, discharging or idle;
- any deliberate major load transition and its approximate time; and
- the Eastron front-panel power/current values for both measurement channels,
  if those values are available without changing configuration.

Do not disclose the site address, coordinates, Forecast.Solar key, Wi-Fi
credentials or inverter serial number. The tool itself does not request those
items.

## Returning the evidence

Return these files for analysis:

1. the successful Solis baseline JSON;
2. the ASW baseline JSON;
3. the split ASW CT-data JSON;
4. both correlation JSON files; and
5. the short timestamped observation note.

Before sharing, a quick review for accidental private notes is sufficient:

```console
python3 -m json.tool discovery-output/solis-baseline.json | less
python3 -m json.tool discovery-output/asw-ct-split.json | less
```

Raw request and response frames are intentionally retained. They let us
distinguish a register-map error, byte/word-order issue, Modbus exception and
plausible decoded value without repeating the live test.

## Interim conclusion after stages 1–5

Before the terminal-8 capture, the returned data established that:

1. the ASW mirrors the Eastron grid channel's live per-phase measurements, but
   no second-channel data had yet been identified;
2. plant power flow can be reconstructed from ASW grid data plus direct Solis
   data; and
3. part of the V2.1.4 `CT Data` range represents real measurements, while
   other fields are unsupported, invalid or not populated on this model.

The later passive capture in stage 8 supersedes the first conclusion: it
identified both logical meter channels directly. This interim section is
retained to show why the firmware and bus-discovery stages were performed.

## Stage 6: Ai-dongle meter route

Static analysis of firmware `610-50017-05` recovered the Ai dongle's meter
poll. It sends an ordinary Eastron Modbus RTU function-`0x04` frame on its
inverter-facing UART and receives an ordinary meter response. See
[`ai-dongle-firmware.md`](ai-dongle-firmware.md) for the evidence.

First reproduce only the dongle's confirmed channel-1 addresses:

```console
python3 tools/live_modbus_discovery.py probe asw-meter-tunnel \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --output discovery-output/asw-meter-tunnel.json \
  --verbose
```

This profile used meter slave address `1`, not inverter slave address `3`. Its
first frame reads input-register offset `52`, count `2`, which is the exact
fast total-active-power request recovered from the firmware.

The returned `discovery-output/asw-meter-tunnel.json` is silent: there was no
response, exception, or adapter echo within 1.5 seconds. This means there is
no evidence for a slave-1 meter route on MONITOR. It does not distinguish an
absent slave 1 from a non-transparent terminal-8 bus.

Do not run the extended channel-2 candidates unless a later test first
produces a valid channel-1 response. If that prerequisite is met, the command
is:

```console
python3 tools/live_modbus_discovery.py probe asw-meter-tunnel \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --extended \
  --output discovery-output/asw-meter-tunnel-extended.json \
  --verbose
```

The extended reads test input-register offsets `3052` and `3012`. This is a
specific hypothesis based on the documented `+3000` channel segmentation used
by other Eastron multi-channel meters; the available SEM3-M-2L manual does not
publish its second-channel register map. These requests use function `0x04`
only and cannot alter meter or inverter state.

## Stage 7: read the configured address and scan the MONITOR bus

The dongle firmware registers `GET /paraget.cgi`. Its JSON includes
`meter_en`, `meter_add`, and `meter_mod`, but also device identifiers and a
possible cloud credential in `key`. The safe collector never saves the raw
response or the sensitive values:

```console
python3 tools/ai_dongle_discovery.py \
  --base-url https://REPLACE_WITH_AI_DONGLE_IP \
  --output discovery-output/ai-dongle-parameters.json
```

Use the Ai dongle's current LAN address. Supplying an IP without a scheme is
also accepted and defaults to `https://`. The dongle's certificate cannot be
validated normally, so the collector disables certificate and hostname
verification only for this request. The request is read-only and fixed to
`/paraget.cgi`; the tool cannot invoke any setter CGI. Return
`ai-dongle-parameters.json`.

This endpoint is not a complete backup or restore facility. It is a
human-readable snapshot of the dongle's main parameters. Do not manually
query `/wlanget.cgi` or `/ifconfig.cgi`, because those responses may contain
WLAN credentials. Never call `/paraset.cgi`, `/setting.cgi`,
`/nvs_clear.cgi`, or another setter during discovery.

Next scan only the usual low address range on the existing ASW MONITOR
adapter:

Do not run the scan while an Ai dongle is simultaneously attached and active
on the same inverter communication segment. Collect the CGI snapshot first,
then restore the already-tested Waveshare-only MONITOR topology before
scanning. Two active Modbus masters can collide.

```console
python3 tools/live_modbus_discovery.py scan \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --start-slave 1 \
  --end-slave 16 \
  --output discovery-output/asw-slave-scan-1-16.json \
  --verbose
```

For each address, the scan sends the recovered Eastron input-register
offset-52/count-2 request. If that is silent, it sends the confirmed ASW
device-header offset-1000/count-2 request. Both use function `0x04`. A valid
normal reply or a valid Modbus exception proves that the address is occupied.
The second signature ensures that the known ASW at address 3 remains
discoverable even if it silently discards the Eastron register address.

The default 0.35-second timeout makes the 1–16 pass take at most roughly
13 seconds including gaps when most addresses are silent. Do not expand the
range until the safe CGI result and this first scan have been analysed.

Return `asw-slave-scan-1-16.json`. If `meter_add` identifies an address outside
1–16, test only that single address with equal start and end values rather
than scanning the rest of the Modbus range.

### Stage 7 results

The live dongle reported:

```text
model       BA1300-30
hardware    M11
software    V610-09578-02.013
meter_en    0
meter_add   0
meter_mod   0
```

This live firmware is not the `22602-005R` application in the analyzed
`610-50017-05` image. More importantly, its optional dongle-level meter
controller is disabled. The ASW's own terminal-8 meter polling remains active
and is visible indirectly through the inverter's smart-meter registers.

The two scan files show:

| MONITOR pins | Responding slaves | Slave-3 response to offset 52 |
| --- | --- | --- |
| 7–8 | 3 only | No response; the subsequent ASW offset-1000 read succeeded |
| 1–2 | 3 only | Modbus exception `0x02` (illegal data address) |

Slaves 1–2 and 4–16 were silent on both pin pairs. The official ASW manual
assigns both pins 1–2 and 7–8 as RS-485 A/B under MONITOR/COM2, and
specifically describes pins 1–2 as the third-party monitor connection. It
does not document a separate meter route on either pair, and the live results
do not reveal one.
See the communication-interface table in
[`UM0035_ASW05-12KH-T2-T3_EN_V05_0225.pdf`](../reference/UM0035_ASW05-12KH-T2-T3_EN_V05_0225.pdf).

Do not expand the address scan. There is no evidence that the terminal-8
Eastron is addressable through MONITOR.

One final MONITOR check is useful for daemon-interface selection rather than
meter discovery: run the complete fixed ASW profile on the officially
designated third-party pair, pins 1–2.

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --output discovery-output/asw-pin1-2-baseline.json \
  --verbose
```

The returned `asw-pin1-2-baseline.json` has overall status `ok`. All nine
normal profile groups succeeded, including both function-`0x04` input
registers and function-`0x03` holding registers. The model, address, firmware,
rated power and manufacturer match the pins-7–8 baseline; live power, energy
and battery values changed only as expected with time. This confirms pins 1–2
as the daemon's preferred MONITOR connection. Retain pins 7–8 for
vendor-dongle compatibility.

The split CT profile was also repeated on pins 1–2:

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --extended \
  --output discovery-output/asw-pin1-2-ct-split.json \
  --verbose
```

The result matches pins 7–8: every range through `46450` responded and the
isolated register `46451` returned exception `0x02`. The two MONITOR pin pairs
therefore expose the same useful ASW register map. The earlier difference in
how offset 52 was rejected does not reveal additional data on pins 1–2.

Do not repeat the old monolithic 51-register CT request or the 60-sample
correlation capture merely to compare the pins: both used a superseded profile
whose inclusion of unsupported register `46451` guaranteed exception `0x02`.
Do not repeat the 19200/38400-baud probes. The pins-1–2 slave scan already
repeated the exact slave-1 Eastron-signature request used by
`asw-meter-tunnel`, and it was silent, so repeating that profile would add no
evidence.

## Stage 8: passive terminal-8 capture

The final direct discovery method is a parallel, receive-only tap on the
existing ASW–Eastron terminal-8 RS-485 pair. It must add no termination, must
never connect an additional transmitter, and must leave the inverter and meter
as the only active bus participants.

One purpose-built candidate to investigate is the
[RSLogger 485](https://www.rslogger.com/en/rslogger-hardware-serial-rs232-logger-recorder-serialghost/rslogger-485-dual-channel-rs485-data-logger).
Its manufacturer describes both RS-485 channels as passive receive-only
inputs, states that they do not drive or modify the bus, provides functional
galvanic isolation, and supports both raw and decoded Modbus RTU recording. At
the time of investigation the manufacturer's store listed it as out of stock.

Before purchasing or attaching any logger, obtain written confirmation of:

1. receiver input impedance or RS-485 unit-load rating;
2. no fitted or automatically enabled 120-ohm termination;
3. no pull-up/pull-down/failsafe bias applied to A/B;
4. driver-disable being enforced in hardware, not merely by software; and
5. whether isolated RS-485 reference ground should remain disconnected for a
   two-wire parallel tap.

Capture raw data as well as any built-in Modbus decoding. The raw bytes are
required to validate CRCs and recover undocumented requests. USB-chunk arrival
times are useful supporting evidence but are not precise per-byte timestamps.
A few minutes covering ordinary import/export changes should reveal the meter
slave address, function codes, register ranges, and whether the ASW polls a
second SEM3 channel.

### SH-U11F receive-only deployment

The available DSD Tech SH-U11F has separate RS-422 `RXD+`/`RXD-` and
`TXD+`/`TXD-` terminals, so its receiver can be connected while its transmitter
remains physically disconnected. With its termination jumper removed,
resistance measurements were:

| Measurement | Resistance |
| --- | ---: |
| `RXD+` to `RXD-` | 98.6 kΩ |
| `RXD+` to isolated 5 V | 4.5 kΩ |
| `RXD-` to isolated 5 V | 96.7 kΩ |
| `RXD+` to isolated ground | 92.7 kΩ |
| `RXD-` to isolated ground | 4.4 kΩ |

This identifies a failsafe pull-up/pull-down network. It is not a strictly
unbiased receiver, but its approximately 8.9-kΩ bias path is far lighter than
a 120-Ω termination. The live installation uses approximately four metres of
CAT6 between terminal 8 and the unmodified SH-U11F and has shown clean
communication at 9600 baud. Continued daemon use is conditional on retaining
the receive-only wiring and termination setting and monitoring the CRC error
rate and ASW meter health.

Connect only the terminal-8 pair to `RXD+`/`RXD-`. Leave the SH-U11F
termination jumper removed and leave `TXD+(A+)`, `TXD-(B-)`, 5 V and ground
disconnected. Keep the stub short. The conductor that is more positive during
an idle interval belongs on `RXD+`; A/B labels are not consistent between all
RS-485 vendors.

[`tools/passive_modbus_capture.py`](../tools/passive_modbus_capture.py) opens
the serial descriptor `O_RDONLY` and has no serial transmit operation. It
records every received USB chunk and the complete raw stream, then performs
offline CRC-based frame recovery. This makes frame recovery less dependent on
the FTDI driver's USB latency.

Start with a 60-second validation capture at the expected 9600-8-N-1:

```console
python3 tools/passive_modbus_capture.py \
  --device /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG04Q3CR-if00-port0 \
  --baud 9600 \
  --duration 60 \
  --output discovery-output/eastron-terminal8-sniff-9600.json
```

During this short capture, confirm that the ASW continues to report its smart
meter online and that plant import/export control remains normal. Return the
JSON before attempting another baud rate or a longer capture.

- `ok` means at least one CRC-valid known-shape Modbus frame was recovered.
- `raw_data_only` means bytes arrived but no frames passed CRC validation.
  Disconnect the tap before changing polarity or other wiring.
- `no_data` means no serial bytes arrived at 9600 baud.

The JSON may contain raw device responses and plant operating data. Treat it
like the other ignored files under `discovery-output/`.

### Stage 8 results

`discovery-output/eastron-terminal8-sniff-9600.json` contains a clean
60-second capture:

- 5,241 bytes in 544 USB chunks;
- 302 CRC-valid frames forming 151 request/response transactions;
- all complete frames parsed without CRC errors or Modbus exceptions; and
- only the two bytes already in flight when capture began left unparsed.

All observed requests use function `0x04` (Read Input Registers). No write
operation appears on the bus. Two logical slave addresses expose the same
standard Eastron register layout:

| Slave | Physical measurement | Live evidence |
| --- | --- | --- |
| `1` | Utility-grid CTs | Mixed import/export phase powers with a near-zero aggregate during the capture |
| `2` | External-PV CTs around the Solis feeder | Three positive, similar phase powers declining with late-afternoon PV output |

The previously considered channel-2 `+3000` register-offset hypothesis is
incorrect for this installation. Channel separation is by slave address, not
by a second register segment under slave 1.

The observed request schedule is:

| Slave | PDU start | Count | Contents | Transactions in 60 s |
| ---: | ---: | ---: | --- | ---: |
| 1 | 12 | 6 | Three phase active powers | 128 |
| 1 | 0 | 90 | General electrical measurements | 1 |
| 1 | 200 | 10 | Line-to-line voltages | 1 |
| 1 | 342 | 40 | Per-phase and aggregate energy | 2 |
| 2 | 12 | 6 | Three phase active powers | 5 |
| 2 | 0 | 90 | General electrical measurements | 5 |
| 2 | 200 | 10 | Line-to-line voltages | 5 |
| 2 | 342 | 40 | Per-phase and aggregate energy | 4 |

Slave-1 phase power is sampled approximately every 0.45 seconds for the ASW's
grid-control loop. The slave-2 groups rotate, with each group recurring about
every 12.5 seconds. Because both the phase-power block and the general block
contain phase active power, usable external-PV samples arrive at alternating
intervals of approximately three and nine seconds.

The Eastron uses big-endian IEEE-754 floating-point values. The decoded capture
included:

- slave-1 aggregate grid power between approximately `-88 W` and `+107 W`;
- slave-2 aggregate external-PV power declining from approximately `669 W` to
  `576 W`;
- bidirectional cumulative grid energy on slave 1; and
- approximately `2484.77 kWh` forward and `2.36 kWh` reverse cumulative energy
  on the external-PV channel, consistent with its CT orientation.

### Source-of-truth decision

The daemon will treat the terminal-8 Eastron measurements as authoritative for
plant accounting:

- slave 1 is authoritative for utility-grid import/export; and
- slave 2 is authoritative for the AC production delivered by the external PV
  inverter or aggregate of inverters enclosed by those CTs.

The ASW MONITOR integration remains authoritative for inverter, battery, BMS,
operating-state and control data. Direct Solis and other external-inverter
drivers are optional diagnostic/control enhancements. They may supply DC
inputs, temperatures, identity, internal state and faults, but their power and
energy counters do not supersede Eastron data in the plant model.

This division provides brand-independent external-PV compatibility while
preserving the detailed Solplanet ESS data and control path required for
self-consumption optimisation and arbitrage. See
[`daemon-plan.md`](daemon-plan.md) for the implementation sequence.

## First live daemon validation

The first read-only daemon milestone was run on the HAOS test machine with both
live integrations enabled. During the run:

- the ASW continued operating normally;
- both Eastron channels remained online;
- grid import/export continued to work;
- the Solplanet app continued to show current plant data; and
- the passive terminal-8 listener did not disturb the ASW meter poller.

The daemon health snapshot recorded:

| Component | Result |
| --- | --- |
| ASW MONITOR | 62 successful reads, no failures or reconnects |
| Terminal-8 listener | 47 matched transactions from 94 frames |
| Terminal-8 errors | No CRC errors, missing responses, unmatched responses or Modbus exceptions |
| Persistence | 756 measurements written, with no write failures or queue drops |
| API and overall health | `ok`; loopback-only API; control unavailable |

The two bytes discarded by the terminal-8 decoder were the tail of a frame
already in progress when the listener started. This is expected stream-start
behaviour and is consistent with the earlier standalone capture.

The live plant snapshot gave the following simultaneous operating values:

| Measurement | Value |
| --- | ---: |
| Authoritative grid power | `+7290.148 W` import |
| Authoritative external-PV power | `+5857.555 W` generation |
| ASW AC power | `-11812 W` consumption/charging |
| Derived site load | `+1335.703 W` |

The plant equation reconciled exactly:

```text
site.load_power =
    grid.active_power
  + external_pv.active_power
  + asw.active_power

1335.703 W = 7290.148 W + 5857.555 W - 11812 W
```

The ASW was reporting a `-12000 W` command and approximately `-11257 W`
battery power, which is consistent with battery charging plus inverter
conversion and auxiliary consumption. Its smart-meter mirror was within
approximately `114 W` of the directly observed Eastron grid value even though
the observations were about 1.3 seconds apart. Per-phase values also summed
closely to their respective grid, external-PV and ASW aggregates.

The battery reported 67% SOC, 100% SOH, normal current limits and no active ASW
fault or warning words. The known `0xffff` and `0x80000000` sentinel values are
unsupported or unavailable fields rather than communication failures.

This test validates the connection topology, measurement signs, source
authority and instantaneous site-load calculation. It is a short functional
validation, not yet the required multi-day reliability soak.

### Follow-up data-model corrections

Two issues were identified without affecting the successful live operation:

1. The unpopulated ASW registers currently named
   `site.energy.consumption_today` and `site.energy.generation_today` return
   zero and must not be presented as authoritative plant totals. They should
   be suppressed or retained under an explicitly ASW-reported namespace.
   Authoritative site totals will be calculated from Eastron observations and
   persisted counter baselines.
2. Documented sentinel values currently have quality `invalid`. A distinct
   `unavailable` quality will better separate supported-but-unpopulated
   registers from malformed data.

The raw validation snapshots remain under the ignored `discovery-output/`
directory because they contain detailed plant operating data.
