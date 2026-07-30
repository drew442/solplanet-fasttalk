import datetime as dt
from importlib import resources
import sqlite3
from pathlib import Path
import tempfile
import unittest

from solplanet_fasttalk.config import ConfigError, load_config
from solplanet_fasttalk.storage import HistoryReader, initialize_database


class DiagnosticsHistoryTests(unittest.TestCase):
    def test_graph_series_buckets_numeric_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.sqlite3")
            initialize_database(database)
            with sqlite3.connect(database) as connection:
                for second, value in ((1, 100), (8, 300), (12, 500)):
                    connection.execute(
                        """
                        INSERT INTO measurements (
                            observed_at, name, value_num, value_text, unit,
                            quality, source, authority, access_mode, metadata_json
                        ) VALUES (?, 'grid.active_power', ?, NULL, 'W',
                                  'good', 'meter', 'authoritative',
                                  'passive_bus', '{}')
                        """,
                        (
                            dt.datetime(
                                2026, 1, 1, 0, 0, second,
                                tzinfo=dt.timezone.utc,
                            ).isoformat(),
                            value,
                        ),
                    )
            series = HistoryReader(database).series(
                "grid.active_power",
                bucket_seconds=10,
            )

        self.assertEqual(len(series), 2)
        newest, oldest = series
        self.assertEqual(newest["value"], 500)
        self.assertEqual(oldest["value"], 200)
        self.assertEqual(oldest["metadata"]["samples"], 2)
        self.assertEqual(oldest["metadata"]["minimum"], 100)
        self.assertEqual(oldest["metadata"]["maximum"], 300)
        self.assertEqual(oldest["access_mode"], "time_bucket")

    def test_graph_series_rejects_unbounded_bucket_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.sqlite3")
            initialize_database(database)
            with self.assertRaises(ValueError):
                HistoryReader(database).series(
                    "grid.active_power",
                    bucket_seconds=1,
                )


class DiagnosticsSecurityTests(unittest.TestCase):
    def _config(self, directory: str, api: str) -> Path:
        path = Path(directory) / "config.toml"
        path.write_text(
            f"""
[daemon]
database = "history.sqlite3"
[api]
{api}
[eastron]
enabled = false
[asw]
enabled = false
""",
            encoding="utf-8",
        )
        return path

    def test_non_loopback_api_accepts_private_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "diagnostics.token"
            token.write_text("a" * 48, encoding="utf-8")
            token.chmod(0o600)
            path = self._config(
                directory,
                f'host = "0.0.0.0"\nauth_token_file = "{token}"',
            )
            config = load_config(path)
        self.assertEqual(config.api.host, "0.0.0.0")

    def test_api_rejects_token_file_readable_by_other_users(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "diagnostics.token"
            token.write_text("b" * 48, encoding="utf-8")
            token.chmod(0o644)
            path = self._config(
                directory,
                f'host = "0.0.0.0"\nauth_token_file = "{token}"',
            )
            with self.assertRaisesRegex(ConfigError, "0600"):
                load_config(path)

    def test_api_rejects_short_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "diagnostics.token"
            token.write_text("too-short", encoding="utf-8")
            token.chmod(0o600)
            path = self._config(
                directory,
                f'host = "0.0.0.0"\nauth_token_file = "{token}"',
            )
            with self.assertRaisesRegex(ConfigError, "at least 32"):
                load_config(path)


class DiagnosticsAssetTests(unittest.TestCase):
    def test_dashboard_assets_are_package_resources(self):
        package = resources.files("solplanet_fasttalk.webui")
        for name in ("index.html", "app.css", "app.js"):
            payload = package.joinpath(name).read_text(encoding="utf-8")
            self.assertGreater(len(payload), 100)

    def test_dashboard_has_no_external_runtime_dependencies(self):
        package = resources.files("solplanet_fasttalk.webui")
        html = package.joinpath("index.html").read_text(encoding="utf-8")
        javascript = package.joinpath("app.js").read_text(encoding="utf-8")
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("innerHTML", javascript)


if __name__ == "__main__":
    unittest.main()
