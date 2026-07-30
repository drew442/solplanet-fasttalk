"""Serial receiver that is structurally incapable of transmitting."""

from __future__ import annotations

import fcntl
import os
import select
import termios


BAUD_RATES = {
    9600: termios.B9600,
}


class ReadOnlySerialError(Exception):
    pass


class ReadOnlySerial:
    def __init__(self, device: str, baud: int = 9600) -> None:
        self.device = device
        self.baud = baud
        self.fd: int | None = None

    def __enter__(self) -> "ReadOnlySerial":
        if self.baud not in BAUD_RATES:
            raise ReadOnlySerialError(f"unsupported receive baud: {self.baud}")
        try:
            fd = os.open(self.device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise ReadOnlySerialError(f"cannot open {self.device}: {exc}") from exc
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
            attrs[4] = BAUD_RATES[self.baud]
            attrs[5] = BAUD_RATES[self.baud]
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
        except Exception:
            os.close(fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read(self, timeout: float = 0.5) -> bytes:
        if self.fd is None:
            raise RuntimeError("serial receiver is not open")
        readable, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 4096) if readable else b""

