import unittest

from tools.ai_dongle_discovery import (
    DongleDiscoveryError,
    endpoint_url,
    make_safe_snapshot,
)


class AiDongleDiscoveryTests(unittest.TestCase):
    def test_base_url_defaults_to_https_and_fixed_endpoint(self) -> None:
        self.assertEqual(
            endpoint_url("192.0.2.10"),
            "https://192.0.2.10/paraget.cgi",
        )
        self.assertEqual(
            endpoint_url("https://dongle.example:8443/"),
            "https://dongle.example:8443/paraget.cgi",
        )

    def test_base_url_rejects_paths_and_embedded_credentials(self) -> None:
        with self.assertRaises(DongleDiscoveryError):
            endpoint_url("https://192.0.2.10/paraset.cgi")
        with self.assertRaises(DongleDiscoveryError):
            endpoint_url("https://admin:secret@192.0.2.10")
        with self.assertRaises(DongleDiscoveryError):
            endpoint_url("http://192.0.2.10")

    def test_snapshot_keeps_meter_values_and_omits_secrets(self) -> None:
        snapshot = make_safe_snapshot(
            {
                "psn": "private-serial",
                "key": "private-key",
                "nam": "private-site-name",
                "pdk": "private-product-key",
                "ser": "private-service-value",
                "ali_ip": "private-cloud-or-ip",
                "ali_port": 1234,
                "typ": 4,
                "mod": "LAN",
                "hw": "ESP32-WROVER-IE",
                "sw": "22602-005R",
                "meter_en": 1,
                "meter_add": 7,
                "meter_mod": 0,
            }
        )
        self.assertEqual(
            snapshot["meter_configuration"],
            {
                "meter_en": 1,
                "meter_add": 7,
                "meter_mod": 0,
            },
        )
        self.assertFalse(snapshot["transport"]["certificate_verification"])
        rendered = str(snapshot)
        self.assertNotIn("private-", rendered)
        self.assertEqual(
            snapshot["privacy"]["omitted_sensitive_fields_present"],
            ["psn", "key", "nam", "pdk", "ser", "ali_ip", "ali_port"],
        )


if __name__ == "__main__":
    unittest.main()
