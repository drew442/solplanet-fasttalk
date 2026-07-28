# Live system discovery

This runbook gathers read-only Modbus RTU evidence from the initial test plant:

- Solis-10K through its dedicated Waveshare USB-RS485 adapter; and
- Solplanet ASW12kH-T3 through MONITOR-port pins 7 and 8 and its dedicated
  Waveshare USB-RS485 adapter.

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
- grid-side phase active power is signed; and
- `0x8000`/`0x80000000` signed NaN values are reported as unavailable.

## Stage 4: experimental ASW CT-data read

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
them with function `0x03`. Their relationship to the Eastron terminal-8 meter
is unknown. The original single 51-register request returned exception `0x02`,
which could have been caused by any one unsupported address. Version 0.2
therefore uses eight semantic ranges:

- `46401–46406`: phase voltage and current;
- `46407–46412`: phase active power;
- `46413–46418`: phase apparent power;
- `46419–46424`: phase reactive power;
- `46425–46433`: phase factor, angle and averages;
- `46434–46442`: system totals and frequency;
- `46443–46450`: energy; and
- `46451`: an individually isolated, model-dependent register.

All-zero, all-NaN, exception, stale, or plausible live values are each useful
results—do not attempt to populate the registers.

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

## What happens next

The returned data will determine whether:

1. the ASW already exposes both useful Eastron measurement channels;
2. plant state can be reconstructed from ASW grid data plus direct Solis data;
3. the V2.1.4 `CT Data` range represents real measurements or an injection
   interface; or
4. a passive, receive-only RS485 capture of the ASW–Eastron bus is necessary.

The original work deliberately deferred direct Eastron queries until these
alternatives had been exhausted. The Ai-dongle analysis below now provides a
specific, bounded route to test.

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

If this matches the pins-7–8 baseline, use pins 1–2 as the daemon's preferred
MONITOR connection and retain pins 7–8 for vendor-dongle compatibility.

## Stage 8: passive terminal-8 capture

The remaining direct discovery method is a parallel, receive-only tap on the
existing ASW–Eastron terminal-8 RS-485 pair. It must add no termination or
bias, must never enable a transmitter, and must leave the inverter and meter
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

Capture raw data as well as any built-in Modbus decoding. The raw bytes and
inter-frame timing are required to validate CRCs and recover undocumented
requests. A few minutes covering ordinary import/export changes should reveal
the meter slave address, function codes, register ranges, and whether the ASW
polls a second SEM3 channel.
