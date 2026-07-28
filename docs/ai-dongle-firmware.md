# Ai dongle firmware analysis

This note records the static analysis of the Solplanet/Aiswei Ai dongle
firmware in `reference/Ai Dongle Software/610-50017-05`. The firmware was
treated as untrusted data: it was inspected and disassembled, not executed or
flashed.

## Image identity

| File | Size | SHA-256 |
| --- | ---: | --- |
| `update.bin` | 1,047,344 bytes | `08297abd628acfb8b8829e5e32c92d551f4f6ffb315f79e72648b00d2480d265` |
| `target.bin` | 11,729,712 bytes | `bf91ecb83dacca7321a763383311ca9468a440f17dbbec0041e2f21e18332fd2` |

`update.bin` is a valid ESP32 application image:

- project: `LanStick`;
- version: `22602-005R-1-g391105e`;
- build time: 2023-07-06 15:54:24;
- SDK: ESP-IDF `v4.4.1-dirty`; and
- entry point: `0x40081980`.

`target.bin` is a sparse merged flash image. It contains a bootloader, a
partition table, blank data partitions, and an OTA application at flash offset
`0x00a30000`. That OTA application is byte-for-byte identical to `update.bin`.
There is no captured device configuration or meter data in the image.

The partition table describes:

| Partition | Offset | Size |
| --- | ---: | ---: |
| `nvs` | `0x009000` | `0x019000` |
| `my_nvs` | `0x022000` | `0x00f000` |
| `otadata` | `0x031000` | `0x002000` |
| `phy_init` | `0x033000` | `0x002000` |
| `invdata` | `0x035000` | `0x7e9000` |
| `storage` | `0x81e000` | `0x20d000` |
| `ota_0` | `0xa30000` | `0x20d000` |
| `ota_1` | `0xc40000` | `0x20d000` |

The accompanying `.xlsx` file is not a normal ZIP-based XLSX workbook. Its
header identifies an `E-SafeNet`/`LOCK` wrapper, consistent with an encrypted
document. It was not decrypted or used for the findings below.

## Meter implementation

The application contains a dedicated `meter.c` module and NVS keys
`meter_en`, `meter_add`, and `meter_mod`. Its built-in model labels are:

- `EASTRON`;
- `SDM630CT`;
- `SDM630DC`;
- `SDM230`;
- `SDM220`; and
- `SDM120`.

There is no `SEM3`, `SEM3-M-2L`, or explicit second-channel label. This
firmware predates the supplied 2026 SEM3-M-2L documentation.

The recovered request builder at IROM address `0x400e6c58` writes:

```text
byte 0    configured meter address
byte 1    Modbus function
byte 2-3  start address, big endian
byte 4-5  register count, big endian
byte 6-7  Modbus CRC16, low byte first
```

The read path uses function `0x04`. For the three-phase meter cases it issues:

- start `12` (`0x000c`), count `70` for the full measurement poll; and
- start `52` (`0x0034`), count `2` for the fast total-active-power poll.

These addresses match the conventional Eastron input-register map:

- offset 12: phase 1 active power;
- offsets 14 and 16: phase 2 and phase 3 active power;
- offset 52: total active power;
- offset 70: frequency; and
- offsets 72 and 74: import and export active energy.

The response parser checks meter address, function, byte count, and CRC before
decoding Eastron's big-endian IEEE-754 values.

Most importantly, the send path calls the ESP-IDF `uart_write_bytes`-shaped
routine with UART number `1`, the raw Modbus frame, and its length. The nearby
diagnostics say `send to inv meter` and `receive from inv meter`. There is no
cloud request or vendor envelope around the meter frame.

This proves that the dongle itself emits an ordinary meter frame on its
inverter-facing UART. It does **not** prove that an independent Modbus master
on the ASW MONITOR pins can ask the inverter to relay that frame to terminal
8. The `asw-meter-tunnel` profile tested that narrower hypothesis through the
existing Waveshare connection.

The live slave-1 test sent:

```text
01 04 00 34 00 02 30 05
```

The adapter received no bytes within 1.5 seconds: no data, Modbus exception,
or adapter echo. The result is consistent with slave 1 being absent from the
MONITOR bus, but is also consistent with the inverter not exposing a
transparent terminal-8 route. It does not establish which explanation is
correct.

## Local HTTP/CGI interface

The firmware registers a local HTTP interface containing separate read and
write handlers. The most useful read-only handler for current discovery is:

```text
GET /paraget.cgi
```

Its JSON builder includes these fields:

```text
psn, key, typ, nam, mod, muf, brd, hw, sw, wsw, tim, pdk, ser,
status, ali_ip, ali_port, meter_en, meter_add, meter_mod, elink
```

The three meter values are numeric. `meter_en` is the enable setting,
`meter_add` is the configured Modbus slave address, and `meter_mod` selects
the dongle's built-in meter decoder. This is the fastest way to test the
slave-address hypothesis without probing every address.

`/paraget.cgi` is a logical parameter snapshot, not a byte-for-byte NVS
backup. It does not include every setting stored by the dongle and the
firmware exposes no dedicated configuration-export or restore handler.
Several returned fields can identify the device or contain cloud
credentials. The repository tool saves only a privacy-safe subset:

```console
python3 tools/ai_dongle_discovery.py \
  --base-url http://REPLACE_WITH_AI_DONGLE_IP \
  --output discovery-output/ai-dongle-parameters.json
```

The firmware also registers read handlers such as `/ifconfig.cgi`,
`/wlanget.cgi`, `/getdev.cgi`, and `/getdevdata.cgi`. The network handlers may
expose WLAN credentials, while the device-data handlers require additional
request parameters and are not needed to learn the meter address. Static
analysis of the `/getdevdata.cgi` JSON builder finds one aggregate
`meter_pac` field and no channel-2 meter value. It therefore does not solve
the SEM3 second-channel problem.

Do not call `/paraset.cgi`, `/setting.cgi`, `/wlanset.cgi`, `/reboot.cgi`, or
`/nvs_clear.cgi` during discovery. They are mutation handlers; in particular,
`/nvs_clear.cgi` clears persistent configuration.

## What the firmware does not reveal

The dongle uploads one aggregate meter data object (`UpdateMeterData`) and its
meter decoder only implements the older single-channel Eastron map. It contains
no SEM3-M-2L-specific register table and does not reveal the second channel's
address.

Other Eastron multi-channel products support a single-address mode in which
channel 2 repeats the channel-1 register map at an offset of 3000 registers.
That makes input-register offset `3052` a credible candidate for channel 2
total active power, but it remains an inference rather than a documented SEM3
fact. The discovery profile includes this hypothesis only behind
`--extended`.

The slave-1 route did not respond, so the next live tests are:

1. read `meter_add` from `/paraget.cgi`;
2. run the bounded read-only MONITOR-bus address scan to distinguish the
   known inverter at address 3 from any other responding slave; and
3. repeat the recovered Eastron read only if either source identifies a
   different plausible meter address.

If no meter address responds on MONITOR, the working conclusion will be that
terminal 8 is a separate electrical bus and the dongle has privileged access
to it through the inverter connector or internal routing. At that point,
passive capture becomes more useful than further active guesses.
