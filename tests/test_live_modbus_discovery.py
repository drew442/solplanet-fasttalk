import os
import pty
import threading
import unittest

from tools.live_modbus_discovery import (
    ASW_PROFILE,
    READ_FUNCTIONS,
    SOLIS_PROFILE,
    SerialRTU,
    append_crc,
    build_read_request,
    crc16_modbus,
    decode_fields,
    parse_read_response,
    selected_groups,
    DiscoveryError,
)


class ModbusCodecTests(unittest.TestCase):
    def test_known_modbus_crc(self) -> None:
        frame_without_crc = bytes.fromhex("01 03 00 00 00 0a")
        self.assertEqual(crc16_modbus(frame_without_crc), 0xCDC5)
        self.assertEqual(
            append_crc(frame_without_crc),
            bytes.fromhex("01 03 00 00 00 0a c5 cd"),
        )

    def test_solis_request_uses_documented_offset(self) -> None:
        request = build_read_request(1, 0x04, 2999, 9)
        self.assertEqual(request[:6], bytes.fromhex("01 04 0b b7 00 09"))

    def test_asw_request_uses_documented_offset(self) -> None:
        request = build_read_request(3, 0x04, 1000, 2)
        self.assertEqual(request[:6], bytes.fromhex("03 04 03 e8 00 02"))

    def test_write_function_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_read_request(1, 0x06, 0, 1)

    def test_read_response_decoding(self) -> None:
        request = build_read_request(1, 0x04, 2999, 2)
        response = append_crc(bytes.fromhex("01 04 04 12 34 ab cd"))
        self.assertEqual(
            parse_read_response(request, response, 2),
            [0x1234, 0xABCD],
        )

    def test_bad_crc_is_rejected(self) -> None:
        request = build_read_request(1, 0x04, 2999, 2)
        response = bytes.fromhex("01 04 04 12 34 ab cd 00 00")
        with self.assertRaises(DiscoveryError):
            parse_read_response(request, response, 2)

    def test_serial_exchange_over_pseudo_terminal(self) -> None:
        master_fd, slave_fd = pty.openpty()
        slave_path = os.ttyname(slave_fd)
        os.close(slave_fd)
        request = build_read_request(1, 0x04, 2999, 2)
        response = append_crc(bytes.fromhex("01 04 04 12 34 ab cd"))
        responder_error = []

        def respond() -> None:
            try:
                received = os.read(master_fd, len(request))
                self.assertEqual(received, request)
                os.write(master_fd, response)
            except BaseException as exc:  # propagate thread failures below
                responder_error.append(exc)

        try:
            with SerialRTU(slave_path, 9600, 1.0) as serial_port:
                thread = threading.Thread(target=respond)
                thread.start()
                wire, received_response, echo_removed = serial_port.exchange(request)
        finally:
            if "thread" in locals():
                thread.join(timeout=2)
            os.close(master_fd)
        if responder_error:
            raise responder_error[0]
        self.assertEqual(wire, response)
        self.assertEqual(received_response, response)
        self.assertFalse(echo_removed)


class ProfileTests(unittest.TestCase):
    def test_asw_profile_skips_inverter_serial_number(self) -> None:
        queried = set()
        for group in ASW_PROFILE.groups:
            queried.update(
                range(
                    group.reference_start,
                    group.reference_start + group.count,
                )
            )
        self.assertTrue(set(range(31003, 31019)).isdisjoint(queried))

    def test_extended_ct_group_is_opt_in(self) -> None:
        default_names = {group.name for group in selected_groups(ASW_PROFILE, False)}
        extended_names = {group.name for group in selected_groups(ASW_PROFILE, True)}
        self.assertNotIn("ct_data_experimental", default_names)
        self.assertIn("ct_data_experimental", extended_names)

    def test_solis_u32_and_scaled_fields_decode(self) -> None:
        group = SOLIS_PROFILE.groups[0]
        registers = [0, 0, 0, 0, 0, 0x0001, 0x0002, 0x0003, 0x0004]
        decoded = decode_fields(group, registers)
        self.assertEqual(decoded["active_power"]["value"], 65538)
        self.assertEqual(decoded["total_dc_power"]["value"], 196612)

    def test_all_profile_operations_are_bounded_reads(self) -> None:
        for profile in (SOLIS_PROFILE, ASW_PROFILE):
            for group in profile.groups:
                self.assertIn(group.function, READ_FUNCTIONS)
                self.assertGreaterEqual(group.count, 1)
                self.assertLessEqual(group.count, 125)
                for field in group.fields:
                    self.assertGreaterEqual(field.reference, group.reference_start)
                    self.assertLess(
                        field.reference,
                        group.reference_start + group.count,
                    )


if __name__ == "__main__":
    unittest.main()
