from contextlib import redirect_stdout
import io
import os
import pty
import struct
import threading
import unittest

from tools.live_modbus_discovery import (
    ASW_PROFILE,
    EASTRON_TUNNEL_PROFILE,
    READ_FUNCTIONS,
    SOLIS_PROFILE,
    SerialRTU,
    append_crc,
    build_read_request,
    classify_scan_response,
    crc16_modbus,
    decode_fields,
    parse_read_response,
    perform_slave_scan,
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

    def test_scan_treats_modbus_exception_as_a_present_slave(self) -> None:
        request = build_read_request(7, 0x04, 52, 2)
        response = append_crc(bytes.fromhex("07 84 02"))
        self.assertEqual(
            classify_scan_response(request, response, 2),
            {
                "kind": "exception",
                "exception_code": "0x02",
            },
        )

    def test_scan_classifies_normal_data_response(self) -> None:
        request = build_read_request(3, 0x04, 1000, 2)
        response = append_crc(bytes.fromhex("03 04 04 00 33 00 03"))
        self.assertEqual(
            classify_scan_response(request, response, 2),
            {
                "kind": "data",
                "registers": ["0x0033", "0x0003"],
            },
        )

    def test_bounded_scan_uses_fallback_signature(self) -> None:
        requests = []

        class FakeSerial:
            def exchange(self, request):
                requests.append(request)
                slave = request[0]
                pdu_start = (request[2] << 8) | request[3]
                if slave == 2 and pdu_start == 52:
                    response = append_crc(bytes.fromhex("02 84 02"))
                    return response, response, False
                if slave == 3 and pdu_start == 1000:
                    response = append_crc(
                        bytes.fromhex("03 04 04 00 33 00 03")
                    )
                    return response, response, False
                raise DiscoveryError("no response within 0.01s")

        with redirect_stdout(io.StringIO()):
            results = perform_slave_scan(
                FakeSerial(),
                start_slave=1,
                end_slave=3,
                request_gap=0,
                verbose=False,
            )
        self.assertEqual(
            [entry["status"] for entry in results],
            ["silent", "present", "present"],
        )
        self.assertEqual(
            [len(entry["probes"]) for entry in results],
            [2, 1, 2],
        )
        self.assertEqual(results[1]["response_kind"], "exception")
        self.assertEqual(
            results[2]["matched_probe"],
            "asw_device_header_signature",
        )
        self.assertEqual(len(requests), 5)

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
    def test_confirmed_profile_serial_defaults(self) -> None:
        self.assertEqual(
            (SOLIS_PROFILE.slave, SOLIS_PROFILE.default_baud),
            (1, 9600),
        )
        self.assertEqual(
            (ASW_PROFILE.slave, ASW_PROFILE.default_baud),
            (3, 9600),
        )
        self.assertEqual(
            (
                EASTRON_TUNNEL_PROFILE.slave,
                EASTRON_TUNNEL_PROFILE.default_baud,
            ),
            (1, 9600),
        )

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
        ct_names = {
            group.name for group in ASW_PROFILE.groups if group.extended
        }
        self.assertEqual(len(ct_names), 8)
        self.assertTrue(ct_names.isdisjoint(default_names))
        self.assertTrue(ct_names.issubset(extended_names))

    def test_ct_ranges_are_split_at_semantic_boundaries(self) -> None:
        ct_groups = [group for group in ASW_PROFILE.groups if group.extended]
        ct_ranges = [
            (group.reference_start, group.count) for group in ct_groups
        ]
        self.assertEqual(
            ct_ranges,
            [
                (46401, 6),
                (46407, 6),
                (46413, 6),
                (46419, 6),
                (46425, 9),
                (46434, 9),
                (46443, 8),
                (46451, 1),
            ],
        )
        for group in ct_groups:
            self.assertEqual(group.pdu_start, group.reference_start - 40001)

    def test_solis_u32_and_scaled_fields_decode(self) -> None:
        group = SOLIS_PROFILE.groups[0]
        registers = [0, 0, 0, 0, 0, 0x0001, 0x0002, 0x0003, 0x0004]
        decoded = decode_fields(group, registers)
        self.assertEqual(decoded["active_power"]["value"], 65538)
        self.assertEqual(decoded["total_dc_power"]["value"], 196612)

    def test_asw_firmware_specific_decoding(self) -> None:
        groups = {group.name: group for group in ASW_PROFILE.groups}

        header = decode_fields(groups["device_header"], [0x0033, 0x0003])
        self.assertEqual(header["device_type"]["value"], "3")

        inverter_status = decode_fields(
            groups["inverter_status"],
            [
                2300,
                5000,
                0xFFFF,
                0xFF8C,
                0xFFFF,
                0xEF9A,
                0,
                2295,
                1,
            ],
        )
        self.assertEqual(inverter_status["inverter_energy_today"]["value"], -11.6)
        self.assertEqual(inverter_status["inverter_energy_total"]["value"], -419.8)

        storage_registers = [0] * 33
        storage_registers[20] = 0x8000  # 31621 battery temperature
        storage_registers[21] = 83  # 31622 battery SOC
        storage_registers[22] = 0xFFFF  # 31623 battery SOH
        storage = decode_fields(
            groups["storage_and_battery"],
            storage_registers,
        )
        self.assertIsNone(storage["battery_temperature"]["value"])
        self.assertEqual(
            storage["battery_temperature"]["quality"],
            "documented_nan",
        )
        self.assertEqual(storage["battery_soc"]["value"], 83)
        self.assertIsNone(storage["battery_soh"]["value"])

        grid_registers = [0] * 19
        grid_registers[0:2] = [0xFFFF, 0xF000]
        grid = decode_fields(groups["grid"], grid_registers)
        self.assertEqual(grid["grid_phase_1_active_power"]["value"], -4096)

        meter_registers = [0] * 8
        meter_registers[2:4] = [0x8000, 0x0000]
        meter = decode_fields(groups["smart_meter_state"], meter_registers)
        self.assertIsNone(meter["smart_meter_target_power"]["value"])

    def test_eastron_float_and_channel_2_candidate(self) -> None:
        groups = {
            group.name: group for group in EASTRON_TUNNEL_PROFILE.groups
        }
        words = struct.unpack(">HH", struct.pack(">f", -4321.25))
        decoded = decode_fields(
            groups["channel_1_total_active_power"],
            words,
        )
        self.assertEqual(
            decoded["channel_1_total_active_power"]["value"],
            -4321.25,
        )

        candidate = groups[
            "channel_2_segmented_total_active_power_candidate"
        ]
        self.assertTrue(candidate.extended)
        self.assertEqual(candidate.pdu_start, 52 + 3000)
        self.assertEqual(candidate.function, 0x04)

    def test_all_profile_operations_are_bounded_reads(self) -> None:
        for profile in (
            SOLIS_PROFILE,
            ASW_PROFILE,
            EASTRON_TUNNEL_PROFILE,
        ):
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
                decoded = decode_fields(group, [0] * group.count)
                self.assertFalse(
                    any("error" in entry for entry in decoded.values()),
                    group.name,
                )


if __name__ == "__main__":
    unittest.main()
