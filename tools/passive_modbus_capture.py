#!/usr/bin/env python3
"""Strictly receive-only Modbus RTU capture for an isolated serial receiver.

The serial device is opened O_RDONLY and this module contains no serial write
operation. It records the raw byte stream before attempting offline, CRC-based
frame recovery. This is intended for a hardware receive-only connection such
as the SH-U11F RXD+/RXD- pair with its TXD pair physically disconnected.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import select
import sys
import termios
import time
from typing import Any, Iterable


TOOL_VERSION = "0.1"
BAUD_RATES = {
    2400: termios.B2400,
    4800: termios.B4800,
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


class CaptureError(Exception):
    """Expected capture or configuration failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def crc_is_valid(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    expected = crc16_modbus(frame[:-2])
    received = frame[-2] | (frame[-1] << 8)
    return expected == received


def _u16(data: bytes) -> int:
    return (data[0] << 8) | data[1]


def _candidate_lengths(data: bytes, offset: int) -> Iterable[tuple[int, str]]:
    remaining = len(data) - offset
    if remaining < 5:
        return

    function = data[offset + 1]
    if function & 0x80:
        yield 5, "exception"
        return

    if function in (0x01, 0x02):
        if remaining >= 8:
            quantity = _u16(data[offset + 4 : offset + 6])
            if 1 <= quantity <= 2000:
                yield 8, "request"
        byte_count = data[offset + 2]
        if 1 <= byte_count <= 250:
            yield 5 + byte_count, "response"
        return

    if function in (0x03, 0x04):
        if remaining >= 8:
            quantity = _u16(data[offset + 4 : offset + 6])
            if 1 <= quantity <= 125:
                yield 8, "request"
        byte_count = data[offset + 2]
        if 2 <= byte_count <= 250 and byte_count % 2 == 0:
            yield 5 + byte_count, "response"
        return

    if function in (0x05, 0x06):
        yield 8, "request_or_response"
        return

    if function in (0x0F, 0x10):
        if remaining >= 7:
            byte_count = data[offset + 6]
            if 1 <= byte_count <= 246:
                yield 9 + byte_count, "request"
        yield 8, "response"
        return

    if function == 0x16:
        yield 10, "request_or_response"
        return

    if function == 0x17:
        if remaining >= 11:
            byte_count = data[offset + 10]
            if 1 <= byte_count <= 242:
                yield 13 + byte_count, "request"
        byte_count = data[offset + 2]
        if 2 <= byte_count <= 250 and byte_count % 2 == 0:
            yield 5 + byte_count, "response"


def _frame_details(frame: bytes, kind: str, offset: int) -> dict[str, Any]:
    function = frame[1]
    result: dict[str, Any] = {
        "offset": offset,
        "length": len(frame),
        "slave": frame[0],
        "function": f"0x{function:02x}",
        "kind": kind,
        "frame_hex": frame.hex(" "),
        "crc_valid": True,
    }
    if kind == "exception":
        result["base_function"] = f"0x{function & 0x7f:02x}"
        result["exception_code"] = f"0x{frame[2]:02x}"
    elif function in (0x01, 0x02, 0x03, 0x04) and kind == "request":
        result["pdu_start"] = _u16(frame[2:4])
        result["count"] = _u16(frame[4:6])
    elif function in (0x01, 0x02, 0x03, 0x04) and kind == "response":
        result["byte_count"] = frame[2]
        result["data_hex"] = frame[3:-2].hex(" ")
    return result


def recover_frames(data: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recover non-overlapping, known-shape RTU frames from a raw byte stream."""
    frames: list[dict[str, Any]] = []
    unparsed: list[dict[str, Any]] = []
    offset = 0
    unparsed_start: int | None = None

    while offset < len(data):
        candidates: list[tuple[int, str]] = []
        if data[offset] <= 247 and offset + 1 < len(data):
            for length, kind in _candidate_lengths(data, offset):
                if offset + length > len(data):
                    continue
                frame = data[offset : offset + length]
                if crc_is_valid(frame):
                    candidates.append((length, kind))

        if candidates:
            if unparsed_start is not None:
                junk = data[unparsed_start:offset]
                unparsed.append(
                    {
                        "offset": unparsed_start,
                        "length": len(junk),
                        "data_hex": junk.hex(" "),
                    }
                )
                unparsed_start = None
            length, kind = max(candidates, key=lambda item: item[0])
            frame = data[offset : offset + length]
            frames.append(_frame_details(frame, kind, offset))
            offset += length
            continue

        if unparsed_start is None:
            unparsed_start = offset
        offset += 1

    if unparsed_start is not None:
        junk = data[unparsed_start:]
        unparsed.append(
            {
                "offset": unparsed_start,
                "length": len(junk),
                "data_hex": junk.hex(" "),
            }
        )
    return frames, unparsed


def _configure_read_only_serial(fd: int, baud: int) -> None:
    try:
        baud_constant = BAUD_RATES[baud]
    except KeyError as exc:
        supported = ", ".join(str(value) for value in sorted(BAUD_RATES))
        raise CaptureError(
            f"unsupported baud {baud}; supported values: {supported}"
        ) from exc

    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = baud_constant
    attrs[5] = baud_constant
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)


class ReadOnlySerial:
    """An exclusively locked serial descriptor that cannot be written."""

    def __init__(self, device: str, baud: int) -> None:
        self.device = device
        self.baud = baud
        self.fd: int | None = None

    def __enter__(self) -> "ReadOnlySerial":
        try:
            fd = os.open(
                self.device,
                os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK,
            )
        except OSError as exc:
            raise CaptureError(f"cannot open {self.device}: {exc}") from exc
        self.fd = fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(fd, termios.TIOCEXCL)
            _configure_read_only_serial(fd, self.baud)
        except Exception:
            os.close(fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read(self, timeout: float) -> bytes:
        if self.fd is None:
            raise RuntimeError("serial port is not open")
        readable, _, _ = select.select([self.fd], [], [], timeout)
        if not readable:
            return b""
        return os.read(self.fd, 4096)


def _attach_frame_timestamps(
    frames: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> None:
    chunk_index = 0
    for frame in frames:
        while (
            chunk_index + 1 < len(chunks)
            and chunks[chunk_index + 1]["offset"] <= frame["offset"]
        ):
            chunk_index += 1
        if chunks and chunks[chunk_index]["offset"] <= frame["offset"]:
            frame["first_chunk_sequence"] = chunks[chunk_index]["sequence"]
            frame["first_chunk_timestamp_utc"] = chunks[chunk_index][
                "timestamp_utc"
            ]
            frame["first_chunk_elapsed_seconds"] = chunks[chunk_index][
                "elapsed_seconds"
            ]


def _frame_summary(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[int, str, str], int] = {}
    for frame in frames:
        key = (frame["slave"], frame["function"], frame["kind"])
        counts[key] = counts.get(key, 0) + 1
    return [
        {
            "slave": slave,
            "function": function,
            "kind": kind,
            "count": count,
        }
        for (slave, function, kind), count in sorted(counts.items())
    ]


def write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def capture(args: argparse.Namespace) -> int:
    if args.duration <= 0:
        raise CaptureError("--duration must be greater than zero")
    if args.status_interval <= 0:
        raise CaptureError("--status-interval must be greater than zero")
    if args.baud not in BAUD_RATES:
        raise CaptureError(f"unsupported baud rate: {args.baud}")

    started_at_utc = utc_now()
    started_monotonic_ns = time.monotonic_ns()
    raw = bytearray()
    chunks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "schema_version": 1,
        "tool": "solplanet-fasttalk-passive-modbus-capture",
        "tool_version": TOOL_VERSION,
        "started_at_utc": started_at_utc,
        "serial": {
            "device": args.device,
            "resolved_device": os.path.realpath(args.device),
            "baud": args.baud,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
            "open_mode": "read_only",
        },
        "requested_duration_seconds": args.duration,
        "safety": {
            "serial_descriptor_opened_read_only": True,
            "serial_write_capability_present": False,
            "requires_hardware_transmit_pair_disconnected": True,
            "termination_must_be_disabled": True,
        },
        "chunks": chunks,
    }

    print(
        f"Opening {args.device} read-only at {args.baud}-8-N-1",
        flush=True,
    )
    print(
        "The capture process has no serial transmit operation. Press Ctrl-C "
        "to stop early and save.",
        flush=True,
    )

    deadline = time.monotonic() + args.duration
    next_status = time.monotonic() + args.status_interval
    interrupted = False
    try:
        with ReadOnlySerial(args.device, args.baud) as serial_port:
            while time.monotonic() < deadline:
                now = time.monotonic()
                timeout = min(0.25, max(0.0, deadline - now))
                chunk = serial_port.read(timeout)
                if chunk:
                    chunk_monotonic_ns = time.monotonic_ns()
                    offset = len(raw)
                    raw.extend(chunk)
                    chunks.append(
                        {
                            "sequence": len(chunks) + 1,
                            "offset": offset,
                            "length": len(chunk),
                            "timestamp_utc": utc_now(),
                            "monotonic_ns": chunk_monotonic_ns,
                            "elapsed_seconds": round(
                                (
                                    chunk_monotonic_ns
                                    - started_monotonic_ns
                                )
                                / 1_000_000_000,
                                6,
                            ),
                            "data_hex": chunk.hex(" "),
                        }
                    )
                if time.monotonic() >= next_status:
                    elapsed = (
                        time.monotonic_ns() - started_monotonic_ns
                    ) / 1_000_000_000
                    print(
                        f"  {elapsed:7.1f}s: {len(raw)} bytes in "
                        f"{len(chunks)} USB reads",
                        flush=True,
                    )
                    next_status += args.status_interval
    except KeyboardInterrupt:
        interrupted = True
        print("Capture interrupted; saving received data.", flush=True)
    except (CaptureError, OSError) as exc:
        result["fatal_error"] = str(exc)
        print(f"[fatal] {exc}", file=sys.stderr)

    frames, unparsed = recover_frames(bytes(raw))
    _attach_frame_timestamps(frames, chunks)
    result["finished_at_utc"] = utc_now()
    result["interrupted"] = interrupted
    result["captured_bytes"] = len(raw)
    result["raw_stream_hex"] = raw.hex(" ")
    result["recovered_frames"] = frames
    result["unparsed_spans"] = unparsed
    result["summary"] = {
        "usb_read_chunks": len(chunks),
        "crc_valid_frames": len(frames),
        "parsed_bytes": sum(frame["length"] for frame in frames),
        "unparsed_bytes": sum(span["length"] for span in unparsed),
        "frames_by_slave_function_and_kind": _frame_summary(frames),
    }
    if "fatal_error" in result:
        result["status"] = "failed"
    elif not raw:
        result["status"] = "no_data"
    elif not frames:
        result["status"] = "raw_data_only"
    else:
        result["status"] = "ok"

    output = Path(args.output)
    write_json(output, result)
    print(f"Saved passive capture to {output}")
    print(
        f"Capture status: {result['status']}; {len(raw)} bytes; "
        f"{len(frames)} CRC-valid frames; "
        f"{result['summary']['unparsed_bytes']} unparsed bytes"
    )
    return 0 if result["status"] in ("ok", "raw_data_only") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strictly receive-only raw and CRC-decoded Modbus RTU capture."
        )
    )
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    parser.add_argument(
        "--device",
        required=True,
        help="serial device, preferably a /dev/serial/by-id path",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=9600,
        choices=sorted(BAUD_RATES),
        help="serial baud rate (default: 9600)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="capture duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=5.0,
        help="progress-report interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="write raw chunks and recovered frames to this JSON file",
    )
    parser.set_defaults(handler=capture)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except CaptureError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
