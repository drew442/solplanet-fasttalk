# Live system discovery

This runbook gathers read-only Modbus RTU evidence from the initial test plant:

- Solis-10K through its dedicated Waveshare USB-RS485 adapter; and
- Solplanet ASW12kH-T3 through MONITOR-port pins 7 and 8 and its dedicated
  Waveshare USB-RS485 adapter.

It does **not** query the Eastron meter directly. The meter must remain under
the exclusive control of the ASW inverter while following this runbook.

## Safety properties

[`tools/live_modbus_discovery.py`](../tools/live_modbus_discovery.py) has a
deliberately narrow protocol surface:

- it implements only function `0x04` (read input registers) and function
  `0x03` (read holding registers);
- it contains no write-register, write-coil, arbitrary-frame, slave-address
  scan, or generic register-read command;
- its device profiles and addresses are fixed in source;
- the ASW inverter serial-number range `31003`–`31018` is not queried;
- the experimental ASW `CT Data` range is disabled unless `--extended` is
  supplied; and
- the first identity read must succeed before the rest of a profile is sent.

Reading an RW holding register with function `0x03` does not write it. The ASW
smart-meter and control-state groups use `0x03` only to observe their current
values.

Do not run this tool while another process is using the same USB adapter. Do
not connect either Waveshare adapter to the live ASW–Eastron terminal-8 bus.
Two active Modbus masters on that bus could collide and interfere with export
control.

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

The ASW Modbus profile defaults to slave address `3`. The test connection is
the working MONITOR-port pins 7 and 8 connection, not terminal 8 and not the
smart-meter cable.

Start at 38400 baud:

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 38400 \
  --output discovery-output/asw-38400-baseline.json \
  --verbose
```

If and only if the first identity read receives no valid response, repeat at
9600:

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 9600 \
  --output discovery-output/asw-9600-baseline.json \
  --verbose
```

If both fail, one final 19200-baud identity attempt is reasonable:

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud 19200 \
  --output discovery-output/asw-19200-baseline.json \
  --verbose
```

Stop after these attempts and review the raw responses. Do not scan all slave
addresses or continue through arbitrary baud rates.

The successful ASW baseline captures:

- model, firmware and rated power without reading the inverter serial number;
- inverter state, power, energy and fault words;
- aggregate battery voltage, current, power, temperature, SOC, SOH and BMS
  current limits for the three parallel Ai-HB G2 stacks;
- the documented per-phase grid values;
- smart-meter online state and current smart-meter power; and
- the configured charge/discharge state and limits, using reads only.

## Stage 4: experimental ASW CT-data read

Only after the normal ASW profile succeeds, repeat it once with the V2.1.4
`CT Data` range enabled:

```console
python3 tools/live_modbus_discovery.py probe asw \
  --device /dev/serial/by-id/REPLACE_WITH_ASW_ADAPTER \
  --baud REPLACE_WITH_WORKING_BAUD \
  --extended \
  --output discovery-output/asw-extended.json \
  --verbose
```

These registers are marked RW in the vendor document, but the tool only reads
them with function `0x03`. Their relationship to the Eastron terminal-8 meter
is unknown. All-zero, all-`0xffff`, exception, stale, or plausible live values
are each useful results—do not attempt to populate the registers.

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
  --baud REPLACE_WITH_WORKING_BAUD \
  --extended \
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
2. every ASW baseline attempt, including failed-baud results;
3. the ASW extended JSON;
4. both correlation JSON files; and
5. the short timestamped observation note.

Before sharing, a quick review for accidental private notes is sufficient:

```console
python3 -m json.tool discovery-output/solis-baseline.json | less
python3 -m json.tool discovery-output/asw-extended.json | less
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

Direct Eastron queries are deliberately deferred until these alternatives have
been exhausted.
