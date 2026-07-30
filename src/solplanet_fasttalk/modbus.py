"""Small, guarded Modbus RTU codec used by the daemon.

Active requests are deliberately restricted to read holding registers (0x03)
and read input registers (0x04).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


READ_FUNCTIONS = frozenset((0x03, 0x04))


class ModbusError(Exception):
    """Invalid, missing, or exceptional Modbus data."""


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def append_crc(payload: bytes) -> bytes:
    value = crc16(payload)
    return payload + bytes((value & 0xFF, value >> 8))


def valid_crc(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    return crc16(frame[:-2]) == frame[-2] | (frame[-1] << 8)


def build_read_request(
    slave: int,
    function: int,
    pdu_start: int,
    count: int,
) -> bytes:
    if not 1 <= slave <= 247:
        raise ValueError("slave must be between 1 and 247")
    if function not in READ_FUNCTIONS:
        raise ValueError("only Modbus functions 0x03 and 0x04 are allowed")
    if not 0 <= pdu_start <= 0xFFFF:
        raise ValueError("PDU start is outside the 16-bit range")
    if not 1 <= count <= 125:
        raise ValueError("register count must be between 1 and 125")
    return append_crc(
        bytes(
            (
                slave,
                function,
                pdu_start >> 8,
                pdu_start & 0xFF,
                count >> 8,
                count & 0xFF,
            )
        )
    )


def parse_read_response(
    request: bytes,
    response: bytes,
    count: int,
) -> list[int]:
    if len(response) < 5 or not valid_crc(response):
        raise ModbusError("invalid response length or CRC")
    if response[0] != request[0]:
        raise ModbusError("response slave does not match request")
    if response[1] == request[1] | 0x80:
        raise ModbusError(f"Modbus exception 0x{response[2]:02x}")
    if response[1] != request[1]:
        raise ModbusError("response function does not match request")
    expected_bytes = count * 2
    if response[2] != expected_bytes or len(response) != expected_bytes + 5:
        raise ModbusError("unexpected response byte count")
    payload = response[3:-2]
    return [
        (payload[index] << 8) | payload[index + 1]
        for index in range(0, len(payload), 2)
    ]


@dataclass(frozen=True)
class Frame:
    slave: int
    function: int
    kind: str
    raw: bytes
    pdu_start: int | None = None
    count: int | None = None
    data: bytes = b""
    exception_code: int | None = None


@dataclass(frozen=True)
class Transaction:
    slave: int
    function: int
    pdu_start: int
    count: int
    request: bytes
    response: bytes
    data: bytes


def _u16(data: bytes) -> int:
    return (data[0] << 8) | data[1]


def _candidate_lengths(data: bytes) -> Iterable[tuple[int, str]]:
    if len(data) < 2:
        return
    function = data[1]
    if function & 0x80:
        yield 5, "exception"
        return
    if function in READ_FUNCTIONS:
        if len(data) >= 6:
            count = _u16(data[4:6])
            if 1 <= count <= 125:
                yield 8, "request"
        if len(data) >= 3:
            byte_count = data[2]
            if 2 <= byte_count <= 250 and byte_count % 2 == 0:
                yield 5 + byte_count, "response"


def _decode_frame(raw: bytes, kind: str) -> Frame:
    function = raw[1]
    if kind == "request":
        return Frame(
            raw[0],
            function,
            kind,
            raw,
            pdu_start=_u16(raw[2:4]),
            count=_u16(raw[4:6]),
        )
    if kind == "response":
        return Frame(raw[0], function, kind, raw, data=raw[3:-2])
    return Frame(
        raw[0],
        function & 0x7F,
        kind,
        raw,
        exception_code=raw[2],
    )


class RTUStreamDecoder:
    """Incrementally recover CRC-valid read frames from arbitrary chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.frames = 0
        self.discarded_bytes = 0
        self.suspect_crc_frames = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buffer.extend(chunk)
        result: list[Frame] = []
        while len(self._buffer) >= 2:
            if not 1 <= self._buffer[0] <= 247:
                self._discard()
                continue
            if self._buffer[1] not in READ_FUNCTIONS and not (
                self._buffer[1] & 0x80
                and self._buffer[1] & 0x7F in READ_FUNCTIONS
            ):
                self._discard()
                continue

            candidates = list(_candidate_lengths(bytes(self._buffer)))
            if (
                not candidates
                and self._buffer[1] in READ_FUNCTIONS
                and len(self._buffer) < 6
            ):
                # A read request cannot be classified until its quantity is
                # present. USB commonly splits the fixed eight-byte request.
                break
            complete = [
                (length, kind)
                for length, kind in candidates
                if length <= len(self._buffer)
            ]
            valid = [
                (length, kind)
                for length, kind in complete
                if valid_crc(bytes(self._buffer[:length]))
            ]
            if valid:
                length, kind = max(valid, key=lambda item: item[0])
                raw = bytes(self._buffer[:length])
                del self._buffer[:length]
                result.append(_decode_frame(raw, kind))
                self.frames += 1
                continue
            if any(length > len(self._buffer) for length, _ in candidates):
                break
            if complete:
                self.suspect_crc_frames += 1
            self._discard()
        return result

    def _discard(self) -> None:
        del self._buffer[0]
        self.discarded_bytes += 1

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)


class TransactionMatcher:
    """Match the single-master request/response sequence observed on RTU."""

    def __init__(self) -> None:
        self.pending: dict[tuple[int, int], Frame] = {}
        self.matched = 0
        self.unmatched_responses = 0
        self.missing_responses = 0
        self.exceptions = 0

    def accept(self, frame: Frame) -> Transaction | None:
        key = (frame.slave, frame.function)
        if frame.kind == "request":
            if key in self.pending:
                self.missing_responses += 1
            self.pending[key] = frame
            return None
        if frame.kind == "exception":
            self.pending.pop(key, None)
            self.exceptions += 1
            return None
        request = self.pending.pop(key, None)
        if request is None or request.pdu_start is None or request.count is None:
            self.unmatched_responses += 1
            return None
        if len(frame.data) != request.count * 2:
            self.unmatched_responses += 1
            return None
        self.matched += 1
        return Transaction(
            frame.slave,
            frame.function,
            request.pdu_start,
            request.count,
            request.raw,
            frame.raw,
            frame.data,
        )
