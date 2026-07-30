"""Exclusively owned active RTU serial connection for read-only requests."""

from __future__ import annotations

import fcntl
import os
import select
import termios
import time

from .modbus import ModbusError


class SerialRTU:
    def __init__(self, device: str, baud: int, timeout: float) -> None:
        self.device = device
        self.baud = baud
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "SerialRTU":
        if self.baud != 9600:
            raise ModbusError("only the confirmed 9600 baud is supported")
        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise ModbusError(f"cannot open {self.device}: {exc}") from exc
        self.fd = fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(fd, termios.TIOCEXCL)
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attrs[3] = 0
            attrs[4] = termios.B9600
            attrs[5] = termios.B9600
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def exchange(self, request: bytes) -> bytes:
        if self.fd is None:
            raise RuntimeError("serial port is not open")
        if request[1] not in (0x03, 0x04):
            raise ModbusError("active serial transport rejected a non-read request")
        termios.tcflush(self.fd, termios.TCIFLUSH)
        written = os.write(self.fd, request)
        if written != len(request):
            raise ModbusError("short serial write")
        termios.tcdrain(self.fd)

        wire = bytearray()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select(
                [self.fd], [], [], max(0.0, deadline - time.monotonic())
            )
            if not readable:
                break
            chunk = os.read(self.fd, 256)
            if not chunk:
                continue
            wire.extend(chunk)
            candidate = bytes(wire)
            if candidate.startswith(request):
                candidate = candidate[len(request) :]
            if len(candidate) >= 3:
                expected = 5 if candidate[1] & 0x80 else 5 + candidate[2]
                if len(candidate) >= expected:
                    return candidate[:expected]
        candidate = bytes(wire)
        if candidate.startswith(request):
            candidate = candidate[len(request) :]
        if not candidate:
            raise ModbusError(f"no response within {self.timeout:.2f}s")
        return candidate

