import fcntl
import inspect
import os
import pty
import unittest

import tools.passive_modbus_capture as passive_capture
from tools.passive_modbus_capture import (
    ReadOnlySerial,
    crc16_modbus,
    recover_frames,
)


def append_crc(payload: bytes) -> bytes:
    crc = crc16_modbus(payload)
    return payload + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


class PassiveFrameRecoveryTests(unittest.TestCase):
    def test_recovers_read_request_response_and_exception(self) -> None:
        request = append_crc(bytes.fromhex("01 04 00 34 00 02"))
        response = append_crc(bytes.fromhex("01 04 04 44 9a 50 00"))
        exception = append_crc(bytes.fromhex("01 84 02"))
        stream = b"\xaa" + request + response + exception + b"\x55"

        frames, unparsed = recover_frames(stream)

        self.assertEqual(
            [(frame["kind"], frame["offset"]) for frame in frames],
            [("request", 1), ("response", 9), ("exception", 18)],
        )
        self.assertEqual(frames[0]["pdu_start"], 52)
        self.assertEqual(frames[0]["count"], 2)
        self.assertEqual(frames[1]["byte_count"], 4)
        self.assertEqual(frames[2]["exception_code"], "0x02")
        self.assertEqual(
            [(span["offset"], span["data_hex"]) for span in unparsed],
            [(0, "aa"), (23, "55")],
        )

    def test_bad_crc_is_left_unparsed(self) -> None:
        bad = bytes.fromhex("01 04 00 34 00 02 00 00")
        frames, unparsed = recover_frames(bad)
        self.assertEqual(frames, [])
        self.assertEqual(unparsed[0]["length"], len(bad))


class ReadOnlySerialTests(unittest.TestCase):
    def test_descriptor_is_read_only_and_receives_bytes(self) -> None:
        master_fd, slave_fd = pty.openpty()
        slave_path = os.ttyname(slave_fd)
        os.close(slave_fd)
        payload = append_crc(bytes.fromhex("01 04 00 34 00 02"))
        try:
            with ReadOnlySerial(slave_path, 9600) as serial_port:
                self.assertIsNotNone(serial_port.fd)
                flags = fcntl.fcntl(serial_port.fd, fcntl.F_GETFL)
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                os.write(master_fd, payload)
                self.assertEqual(serial_port.read(1.0), payload)
        finally:
            os.close(master_fd)

    def test_capture_module_has_no_serial_write_call(self) -> None:
        source = inspect.getsource(passive_capture)
        self.assertNotIn("os.write(", source)


if __name__ == "__main__":
    unittest.main()
