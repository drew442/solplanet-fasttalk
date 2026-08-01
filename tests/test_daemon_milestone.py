import inspect
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from urllib.request import urlopen

import solplanet_fasttalk.eastron as eastron_module
import solplanet_fasttalk.serial_readonly as readonly_module
from solplanet_fasttalk.api import API
from solplanet_fasttalk.asw import IDENTITY_GROUPS, POLL_GROUPS, decode_group
from solplanet_fasttalk.config import (
    APIConfig,
    ASWConfig,
    ConfigError,
    DaemonConfig,
    EastronConfig,
    load_config,
)
from solplanet_fasttalk.daemon import Daemon
from solplanet_fasttalk.eastron import EastronDecoder
from solplanet_fasttalk.model import Measurement, MeasurementQueue, PlantState
from solplanet_fasttalk.modbus import (
    RTUStreamDecoder,
    Transaction,
    TransactionMatcher,
    append_crc,
    build_read_request,
)
from solplanet_fasttalk.storage import (
    HistoryReader,
    HistoryWriter,
    initialize_database,
)


REPOSITORY = Path(__file__).resolve().parents[1]
LIVE_CAPTURE = (
    REPOSITORY / "discovery-output" / "eastron-terminal8-sniff-9600.json"
)
ASW_CAPTURE = (
    REPOSITORY / "discovery-output" / "asw-pin1-2-baseline.json"
)


def measurement(name, value, now, max_age=10):
    return Measurement(
        name,
        value,
        "W",
        "test",
        "authoritative",
        "test",
        "2026-01-01T00:00:00+00:00",
        now,
        max_age,
    )


class MilestoneModbusTests(unittest.TestCase):
    def test_active_builder_rejects_write_functions(self):
        with self.assertRaises(ValueError):
            build_read_request(3, 0x06, 0, 1)

    def test_incremental_decoder_handles_split_frames(self):
        request = append_crc(bytes.fromhex("01 04 00 0c 00 06"))
        response = append_crc(
            bytes.fromhex(
                "01 04 0c 44 de 7d e8 c4 4f ef b6 c4 80 97 99"
            )
        )
        decoder = RTUStreamDecoder()
        frames = []
        stream = b"\x00\xf5" + request + response
        for byte in stream:
            frames.extend(decoder.feed(bytes((byte,))))
        self.assertEqual([frame.kind for frame in frames], ["request", "response"])
        self.assertEqual(decoder.discarded_bytes, 2)

    @unittest.skipUnless(LIVE_CAPTURE.exists(), "private live capture unavailable")
    def test_complete_live_capture_replay(self):
        capture = json.loads(LIVE_CAPTURE.read_text(encoding="utf-8"))
        raw = bytes.fromhex(capture["raw_stream_hex"])
        stream = RTUStreamDecoder()
        matcher = TransactionMatcher()
        state = PlantState()
        meter = EastronDecoder(
            EastronConfig(enabled=False, grid_slave=1, external_pv_slave=2)
        )
        for start in range(0, len(raw), 37):
            for frame in stream.feed(raw[start : start + 37]):
                transaction = matcher.accept(frame)
                if transaction:
                    state.publish_many(meter.decode(transaction))

        self.assertEqual(stream.frames, 302)
        self.assertEqual(stream.discarded_bytes, 2)
        self.assertEqual(matcher.matched, 151)
        self.assertEqual(matcher.unmatched_responses, 0)
        current = state.current()
        self.assertIn("grid.active_power", current)
        self.assertIn("external_pv.active_power", current)
        self.assertEqual(
            current["external_pv.active_power"]["authority"],
            "authoritative",
        )
        self.assertAlmostEqual(current["grid.active_power"]["value"], -31.2099, 3)
        self.assertAlmostEqual(
            current["external_pv.active_power"]["value"],
            589.061844,
            3,
        )
        self.assertAlmostEqual(
            current["external_pv.energy.generated"]["value"],
            2484.784912,
            3,
        )

    def test_passive_integration_has_no_transmit_call(self):
        self.assertNotIn("os.write(", inspect.getsource(eastron_module))
        self.assertNotIn("os.write(", inspect.getsource(readonly_module))

    def test_external_pv_aggregate_clamps_reverse_standby_power(self):
        decoder = EastronDecoder(
            EastronConfig(enabled=False, grid_slave=1, external_pv_slave=2)
        )
        transaction = Transaction(
            slave=2,
            function=0x04,
            pdu_start=12,
            count=6,
            request=b"",
            response=b"",
            data=struct.pack(">fff", -8.0, -9.0, -7.0),
        )
        decoded = {item.name: item for item in decoder.decode(transaction)}
        aggregate = decoded["external_pv.active_power"]
        self.assertEqual(aggregate.value, 0)
        self.assertEqual(aggregate.metadata["unclamped_value_w"], -24)
        self.assertEqual(decoded["external_pv.phase.l1.active_power"].value, -8)


class PlantModelTests(unittest.TestCase):
    def test_derives_site_load_from_fresh_authoritative_inputs(self):
        now = time.monotonic()
        state = PlantState()
        persisted = []
        state.add_sink(persisted.append)
        state.publish_many(
            [
                measurement("grid.active_power", -1000, now),
                measurement("external_pv.active_power", 2500, now),
                measurement("asw.active_power", -500, now),
                measurement("asw.pv.active_power", 0, now),
            ]
        )
        load = state.current()["site.load_power"]
        self.assertEqual(load["value"], 1000)
        self.assertEqual(load["authority"], "derived")
        self.assertIn("site.load_power", [item.name for item in persisted])

    def test_refuses_to_derive_from_stale_input(self):
        now = time.monotonic()
        state = PlantState()
        state.publish_many(
            [
                measurement("grid.active_power", 100, now - 20, 2),
                measurement("external_pv.active_power", 200, now, 10),
                measurement("asw.active_power", 300, now, 10),
                measurement("asw.pv.active_power", 0, now, 10),
            ]
        )
        self.assertNotIn("site.load_power", state.current())


class ASWDecodeTests(unittest.TestCase):
    def test_battery_group_decodes_confirmed_soc_and_power(self):
        group = next(group for group in POLL_GROUPS if group.name == "storage_battery")
        registers = [0] * group.count
        registers[18:20] = [0xFFFF, 0xFC18]  # -1000 W
        registers[21] = 83
        decoded = {
            item.name: item
            for item in decode_group(
                group,
                registers,
                "2026-01-01T00:00:00+00:00",
                time.monotonic(),
            )
        }
        self.assertEqual(decoded["battery.power"].value, -1000)
        self.assertEqual(decoded["battery.soc"].value, 83)

    @unittest.skipUnless(ASW_CAPTURE.exists(), "private ASW capture unavailable")
    def test_replays_all_daemon_groups_from_live_baseline(self):
        capture = json.loads(ASW_CAPTURE.read_text(encoding="utf-8"))
        reads = {
            read["name"]: read
            for read in capture["samples"][0]["reads"]
            if read["status"] == "ok"
        }
        capture_names = {
            "device_header": "device_header",
            "machine_identity": "machine_identity",
            "inverter_power": "inverter_power_and_faults",
            "storage_battery": "storage_and_battery",
            "meter_state": "smart_meter_state",
            "control_state": "storage_control_state",
            "inverter_status": "inverter_status",
            "grid_port": "grid",
        }
        decoded = {}
        for group in (*IDENTITY_GROUPS, *POLL_GROUPS):
            read = reads[capture_names[group.name]]
            registers = [entry["unsigned"] for entry in read["registers"]]
            decoded.update(
                {
                    item.name: item.value
                    for item in decode_group(
                        group,
                        registers,
                        read["timestamp_utc"],
                        time.monotonic(),
                    )
                }
            )
        self.assertEqual(decoded["asw.model"], "ASW12kH-T3")
        self.assertGreaterEqual(decoded["battery.soc"], 0)
        self.assertLessEqual(decoded["battery.soc"], 100)
        self.assertGreaterEqual(decoded["battery.soh"], 0)
        self.assertLessEqual(decoded["battery.soh"], 100)
        self.assertIsInstance(decoded["asw.active_power"], (int, float))
        self.assertIsInstance(
            decoded["asw.smart_meter.active_power"],
            (int, float),
        )


class ConfigurationTests(unittest.TestCase):
    def test_loads_read_only_example_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """
[daemon]
database = "history.sqlite3"
[api]
host = "127.0.0.1"
port = 8765
[eastron]
enabled = false
[asw]
enabled = false
""",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertFalse(config.eastron.enabled)
        self.assertFalse(config.asw.enabled)

    def test_rejects_same_adapter_for_two_enabled_integrations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """
[daemon]
database = "history.sqlite3"
[eastron]
device = "/dev/ttyUSB0"
[asw]
device = "/dev/ttyUSB0"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_non_loopback_unauthenticated_api(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """
[daemon]
database = "history.sqlite3"
[api]
host = "0.0.0.0"
[eastron]
enabled = false
[asw]
enabled = false
""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(path)


class StorageAndAPITests(unittest.TestCase):
    def test_persists_measurement_and_serves_local_api(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.sqlite3")
            initialize_database(database)
            queue = MeasurementQueue()
            stop = threading.Event()
            writer = HistoryWriter(database, queue)
            writer_thread = threading.Thread(target=writer.run, args=(stop,))
            writer_thread.start()

            state = PlantState()
            state.add_sink(queue.put)
            state.publish(
                measurement("grid.active_power", 1234, time.monotonic())
            )
            deadline = time.monotonic() + 2
            while writer.written < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            queue.queue.put(None)
            writer_thread.join(2)

            history = HistoryReader(database)
            self.assertEqual(
                history.measurements("grid.active_power")[0]["value"],
                1234,
            )

            config = DaemonConfig(
                database,
                APIConfig("127.0.0.1", 0),
                EastronConfig(enabled=False),
                ASWConfig(enabled=False),
            )
            try:
                api = API(config, state, history)
            except PermissionError:
                self.skipTest("local listening sockets are prohibited")
            server_thread = threading.Thread(target=api.serve)
            server_thread.start()
            try:
                host, port = api.address
                with urlopen(f"http://{host}:{port}/v1/health", timeout=2) as response:
                    payload = json.load(response)
                self.assertIn("status", payload)
                with urlopen(
                    f"http://{host}:{port}/v1/measurements/history"
                    "?name=grid.active_power",
                    timeout=2,
                ) as response:
                    payload = json.load(response)
                self.assertEqual(payload["measurements"][0]["value"], 1234)
                with urlopen(
                    f"http://{host}:{port}/v1/training/coverage",
                    timeout=2,
                ) as response:
                    coverage = json.load(response)
                self.assertFalse(coverage["location_included"])
                with urlopen(
                    f"http://{host}:{port}/v1/predictions/quality"
                    "?signal=site.load_power&scenario=expected",
                    timeout=2,
                ) as response:
                    quality = json.load(response)
                self.assertTrue(quality["scoreable"])
                self.assertEqual(quality["samples"], 0)
            finally:
                api.close()
                server_thread.join(2)

    def test_daemon_lifecycle_without_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            config = DaemonConfig(
                str(Path(directory) / "history.sqlite3"),
                APIConfig("127.0.0.1", 0),
                EastronConfig(enabled=False),
                ASWConfig(enabled=False),
            )
            try:
                daemon = Daemon(config)
            except PermissionError:
                self.skipTest("local listening sockets are prohibited")
            daemon.start()
            try:
                host, port = daemon.api.address
                with urlopen(f"http://{host}:{port}/v1/devices", timeout=2) as response:
                    payload = json.load(response)
                self.assertEqual(payload["devices"], [])
            finally:
                daemon.stop()
            self.assertTrue(
                all(not thread.is_alive() for thread in daemon.threads.values())
            )
            messages = [
                event["message"] for event in daemon.history.events(limit=10)
            ]
            self.assertIn("daemon started", messages)
            self.assertIn("daemon stopped", messages)


if __name__ == "__main__":
    unittest.main()
