import unittest

from solplanet_fasttalk.modbus_safety import (
    SafetyDisposition,
    assess_modbus_command,
)


class ModbusSafetyPolicyTests(unittest.TestCase):
    def assess(
        self,
        bus: str,
        function: int,
        address: int | None = None,
        count: int = 1,
        slave: int = 3,
    ):
        return assess_modbus_command(
            bus=bus,
            slave=slave,
            function=function,
            documented_address=address,
            count=count,
        )

    def test_bounded_asw_read_is_allowed(self):
        result = self.assess("asw_monitor", 0x03, 41152)
        self.assertEqual(result.disposition, SafetyDisposition.READ_ONLY_ALLOWED)
        self.assertTrue(result.may_transmit_now)

    def test_terminal8_transmit_is_always_prohibited(self):
        result = self.assess("eastron_terminal8", 0x04, 30013, slave=1)
        self.assertEqual(
            result.disposition,
            SafetyDisposition.PERMANENTLY_PROHIBITED,
        )

    def test_broadcast_write_is_permanently_prohibited(self):
        result = self.assess("asw_monitor", 0x06, 41153, slave=0)
        self.assertEqual(
            result.disposition,
            SafetyDisposition.PERMANENTLY_PROHIBITED,
        )

    def test_non_reviewable_write_function_is_permanently_prohibited(self):
        result = self.assess("asw_monitor", 0x05, 41153)
        self.assertEqual(
            result.disposition,
            SafetyDisposition.PERMANENTLY_PROHIBITED,
        )

    def test_asw_protection_and_meter_writes_are_prohibited(self):
        for address in (41112, 44010, 45201, 45253, 45606, 46407, 46523):
            with self.subTest(address=address):
                result = self.assess("asw_monitor", 0x06, address)
                self.assertEqual(
                    result.disposition,
                    SafetyDisposition.PERMANENTLY_PROHIBITED,
                )
                self.assertFalse(result.may_transmit_now)

    def test_asw_plant_controls_require_approval(self):
        for address in (
            40201,
            41104,
            41110,
            41114,
            41115,
            41152,
            41153,
            41154,
            44006,
            45403,
            45503,
        ):
            with self.subTest(address=address):
                result = self.assess("asw_monitor", 0x06, address)
                self.assertEqual(
                    result.disposition,
                    SafetyDisposition.APPROVAL_REQUIRED,
                )
                self.assertFalse(result.may_transmit_now)

    def test_multi_register_asw_power_command_requires_approval(self):
        result = self.assess("asw_monitor", 0x10, 41152, count=2)
        self.assertEqual(
            result.disposition,
            SafetyDisposition.APPROVAL_REQUIRED,
        )

    def test_range_crossing_unreviewed_address_is_denied(self):
        result = self.assess("asw_monitor", 0x10, 41155, count=3)
        self.assertEqual(result.disposition, SafetyDisposition.UNREVIEWED_DENY)

    def test_solis_flash_and_protection_writes_are_prohibited(self):
        for address in (3055, 3069, 3077, 3080, 3084, 3085, 3304):
            with self.subTest(address=address):
                result = self.assess("solis_rs485", 0x06, address, slave=1)
                self.assertEqual(
                    result.disposition,
                    SafetyDisposition.PERMANENTLY_PROHIBITED,
                )

    def test_solis_active_power_controls_require_approval(self):
        for address in (3007, 3051, 3052, 3070, 3071, 3073, 3081, 3083):
            with self.subTest(address=address):
                result = self.assess("solis_rs485", 0x06, address, slave=1)
                self.assertEqual(
                    result.disposition,
                    SafetyDisposition.APPROVAL_REQUIRED,
                )

    def test_solis_grid_coil_requires_approval(self):
        result = self.assess("solis_rs485", 0x05, 5000, slave=1)
        self.assertEqual(
            result.disposition,
            SafetyDisposition.APPROVAL_REQUIRED,
        )

    def test_unknown_write_is_denied(self):
        result = self.assess("asw_monitor", 0x06, 49999)
        self.assertEqual(result.disposition, SafetyDisposition.UNREVIEWED_DENY)


if __name__ == "__main__":
    unittest.main()
