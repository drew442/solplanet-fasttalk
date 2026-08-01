import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from solplanet_fasttalk.asw import POLL_GROUPS, decode_group as decode_asw
from solplanet_fasttalk.accounting import FinancialAccountingWorker
from solplanet_fasttalk.config import (
    ForecastPlane,
    ForecastSolarConfig,
    OptimisationConfig,
    StorageConfig,
    TariffConfig,
    WeatherConfig,
    load_config,
)
from solplanet_fasttalk.forecast import (
    ForecastSolarWorker,
    ForecastStore,
    ForecastCorrector,
    _endpoint,
    solar_elevation_degrees,
)
from solplanet_fasttalk.model import Measurement, PlantState
from solplanet_fasttalk.optimisation import (
    ForecastSlot,
    NativeBaseline,
    OptimisationWorker,
    PlanStore,
    simulate_plan,
)
from solplanet_fasttalk.plugins import PluginRegistry
from solplanet_fasttalk.solis import GROUPS, SolisPlugin, decode_group
from solplanet_fasttalk.storage import (
    HistoryReader,
    StorageMaintainer,
    initialize_database,
)
from solplanet_fasttalk.tariff import PLAN_ID, ZeroHeroTariff
from solplanet_fasttalk.weather import OpenMeteoWorker, WeatherStore


REPOSITORY = Path(__file__).resolve().parents[1]


def measurement(name, value, *, age=0, max_age=10, unit="W"):
    return Measurement(
        name,
        value,
        unit,
        "test",
        "authoritative",
        "test",
        "2026-01-01T00:00:00+00:00",
        time.monotonic() - age,
        max_age,
    )


class Phase3Tests(unittest.TestCase):
    def test_derived_self_consumption_has_provenance(self):
        state = PlantState()
        state.publish_many(
            [
                measurement("grid.active_power", -500),
                measurement("external_pv.active_power", 3000),
                measurement("asw.active_power", 0),
                measurement("asw.pv.active_power", 0),
            ]
        )
        current = state.current()
        self.assertEqual(current["site.load_power"]["value"], 2500)
        self.assertEqual(current["site.self_consumption_power"]["value"], 2500)
        self.assertAlmostEqual(
            current["site.self_consumption_ratio"]["value"], 5 / 6, places=6
        )
        self.assertEqual(
            current["site.self_consumption_power"]["authority"], "derived"
        )
        self.assertIn(
            "grid.active_power",
            current["site.self_consumption_power"]["metadata"]["inputs"],
        )

    def test_battery_discharge_is_not_reported_as_pv_generation(self):
        state = PlantState()
        state.publish_many(
            [
                measurement("grid.active_power", -500),
                measurement("external_pv.active_power", 0),
                measurement("asw.active_power", 1500),
                measurement("asw.pv.active_power", 0),
            ]
        )
        current = state.current()
        self.assertEqual(current["site.load_power"]["value"], 1000)
        self.assertEqual(current["site.generation_power"]["value"], 0)
        self.assertEqual(current["site.pv_generation_power"]["value"], 0)
        self.assertEqual(current["site.self_sufficiency_ratio"]["value"], 1)

    def test_physically_negative_derived_load_is_clipped(self):
        state = PlantState()
        state.publish_many(
            [
                measurement("grid.active_power", 100),
                measurement("external_pv.active_power", 0),
                measurement("asw.active_power", -1000),
                measurement("asw.pv.active_power", 0),
            ]
        )
        self.assertEqual(state.current()["site.load_power"]["value"], 0)

    def test_rollups_and_counter_baselines_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.sqlite3")
            initialize_database(database)
            observed = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
            ).replace(minute=10, second=0, microsecond=0)
            with sqlite3.connect(database) as connection:
                for offset, value in enumerate((100.0, 300.0)):
                    connection.execute(
                        """
                        INSERT INTO measurements (
                            observed_at, name, value_num, value_text, unit,
                            quality, source, authority, access_mode, metadata_json
                        ) VALUES (?, ?, ?, NULL, 'W', 'good', 'test',
                                  'authoritative', 'test', '{}')
                        """,
                        (
                            (
                                observed + dt.timedelta(minutes=offset * 10)
                            ).isoformat(),
                            "grid.active_power",
                            value,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO measurements (
                        observed_at, name, value_num, value_text, unit,
                        quality, source, authority, access_mode, metadata_json
                    ) VALUES (?, 'grid.energy.import', 123.4, NULL, 'kWh',
                              'good', 'eastron.grid', 'authoritative',
                              'passive_bus', '{}')
                    """,
                    (observed.isoformat(),),
                )
                connection.commit()
            StorageMaintainer(
                database,
                StorageConfig(
                    raw_retention_days=10000,
                    hourly_retention_days=10000,
                    daily_retention_days=10000,
                ),
            ).run_once()
            reader = HistoryReader(database)
            hourly = reader.measurements(
                "grid.active_power", resolution="hourly"
            )
            self.assertEqual(hourly[0]["value"], 200)
            self.assertEqual(hourly[0]["metadata"]["samples"], 2)
            restarted_reader = HistoryReader(database)
            self.assertEqual(
                restarted_reader.counter_baselines()["grid.energy.import"][
                    "value"
                ],
                123.4,
            )

    def test_persisted_forecast_compares_with_authoritative_actual(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.sqlite3")
            initialize_database(database)
            reader = HistoryReader(database)
            forecast_at = dt.datetime.now(dt.timezone.utc).replace(
                microsecond=0
            )
            reader.record_forecast(
                "forecast.solar",
                (forecast_at - dt.timedelta(hours=1)).isoformat(),
                [{"timestamp": forecast_at.isoformat(), "power_w": 1000}],
                {"scope": "combined"},
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    INSERT INTO measurements (
                        observed_at, name, value_num, value_text, unit,
                        quality, source, authority, access_mode, metadata_json
                    ) VALUES (?, 'external_pv.active_power', 1100, NULL, 'W',
                              'good', 'eastron.external_pv', 'authoritative',
                              'passive_bus', '{}')
                    """,
                    ((forecast_at + dt.timedelta(seconds=30)).isoformat(),),
                )
                connection.commit()
            comparison = reader.forecast_comparison()
            self.assertEqual(comparison[0]["error_w"], 100)
            self.assertEqual(comparison[0]["actual_authority"], "authoritative")


class Phase4Tests(unittest.TestCase):
    def test_solis_plugin_is_diagnostic_and_optional(self):
        group = next(item for item in GROUPS if item.name == "identity_and_power")
        registers = [0] * group.count
        registers[5:7] = [0, 4321]
        decoded = {
            item.name: item
            for item in decode_group(
                group,
                registers,
                "2026-01-01T00:00:00+00:00",
                time.monotonic(),
            )
        }
        self.assertEqual(decoded["solis.active_power"].value, 4321)
        self.assertEqual(decoded["solis.active_power"].authority, "diagnostic")
        self.assertNotIn("external_pv.active_power", decoded)
        registry = PluginRegistry()
        registry.register(SolisPlugin.descriptor)
        self.assertEqual(registry.descriptors()[0]["version"], 1)

    def test_asw_documented_sentinel_is_unavailable(self):
        group = next(item for item in POLL_GROUPS if item.name == "storage_battery")
        registers = [0] * group.count
        registers[21] = 0xFFFF
        decoded = {
            item.name: item
            for item in decode_asw(
                group,
                registers,
                "2026-01-01T00:00:00+00:00",
                time.monotonic(),
            )
        }
        self.assertIsNone(decoded["battery.soc"].value)
        self.assertEqual(decoded["battery.soc"].quality, "unavailable")
        self.assertIn("asw.reported_site.energy.consumption_today", decoded)
        self.assertNotIn("site.energy.consumption_today", decoded)


class Phase5Tests(unittest.TestCase):
    def setUp(self):
        self.tariff = ZeroHeroTariff(TariffConfig())
        self.sydney = ZoneInfo("Australia/Sydney")

    def test_pre_july_zerohero_rates(self):
        off_peak = self.tariff.quote(
            dt.datetime(2026, 6, 1, 12, 0, tzinfo=self.sydney)
        )
        peak = self.tariff.quote(
            dt.datetime(2026, 6, 1, 19, 0, tzinfo=self.sydney)
        )
        shoulder = self.tariff.quote(
            dt.datetime(2026, 6, 1, 9, 0, tzinfo=self.sydney)
        )
        self.assertEqual(off_peak.plan_id, PLAN_ID)
        self.assertEqual(off_peak.import_price_per_kwh, 0)
        self.assertEqual(peak.import_price_per_kwh, 0.572)
        self.assertEqual(peak.export_price_per_kwh, 0.15)
        self.assertEqual(peak.super_export_daily_cap_kwh, 15)
        self.assertEqual(shoulder.import_price_per_kwh, 0.462)
        self.assertEqual(peak.zerohero_hourly_import_threshold_kwh, 0.03)

    def test_dst_fold_is_deterministic(self):
        first = dt.datetime(2026, 4, 5, 2, 30, tzinfo=self.sydney, fold=0)
        second = dt.datetime(2026, 4, 5, 2, 30, tzinfo=self.sydney, fold=1)
        first_quote = self.tariff.quote(first)
        second_quote = self.tariff.quote(second)
        self.assertNotEqual(first_quote.timestamp, second_quote.timestamp)
        self.assertEqual(
            first_quote.import_price_per_kwh,
            second_quote.import_price_per_kwh,
        )

    def test_forecast_state_never_exposes_secret_or_location(self):
        config = ForecastSolarConfig(
            enabled=True,
            api_key_file="/private/key",
            location_file="/private/location",
            cache_file="/tmp/cache",
            planes=(
                ForecastPlane("east", 25, -90, 6.2),
                ForecastPlane("west", 25, 90, 6.2),
            ),
        )
        url = _endpoint(
            config,
            key="DO_NOT_EXPOSE_12",
            latitude=0,
            longitude=0,
        )
        self.assertIn("/25/-90/6.2/25/90/6.2", url)
        snapshot = ForecastStore(config, "Australia/Sydney").snapshot()
        serialized = json.dumps(snapshot)
        self.assertNotIn("DO_NOT_EXPOSE", serialized)
        self.assertNotIn("latitude", serialized)
        self.assertNotIn("longitude", serialized)

    def test_weather_cache_and_snapshot_omit_location(self):
        class Response:
            def __init__(self, hourly):
                self.hourly = hourly

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return json.dumps({"hourly": self.hourly}).encode()

        times = ["2026-06-01T00:00", "2026-06-01T01:00"]
        context = {
            "time": times,
            "temperature_2m": [10, 11],
            "cloud_cover": [20, 30],
            "cloud_cover_low": [10, 20],
            "cloud_cover_mid": [5, 5],
            "cloud_cover_high": [5, 5],
            "precipitation_probability": [0, 10],
            "weather_code": [0, 1],
            "shortwave_radiation": [0, 100],
            "terrestrial_radiation": [0, 200],
            "is_day": [0, 1],
            "global_tilted_irradiance": [0, 150],
        }
        west = {"time": times, "global_tilted_irradiance": [0, 120]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            location = root / "location.json"
            cache = root / "weather-cache.json"
            location.write_text(
                json.dumps({"latitude": 1.25, "longitude": 2.5}),
                encoding="utf-8",
            )
            config = WeatherConfig(
                enabled=True,
                location_file=str(location),
                cache_file=str(cache),
            )
            store = WeatherStore(config)
            worker = OpenMeteoWorker(
                config,
                (
                    ForecastPlane("east", 25, -90, 6.2),
                    ForecastPlane("west", 25, 90, 6.2),
                ),
                store,
                PlantState(),
            )
            with patch(
                "solplanet_fasttalk.weather.urlopen",
                side_effect=[
                    Response(context),
                    Response(west),
                ],
            ):
                worker._fetch()
            serialized = json.dumps(store.snapshot()) + cache.read_text(
                encoding="utf-8"
            )
        self.assertNotIn("latitude", serialized)
        self.assertNotIn("longitude", serialized)
        self.assertEqual(store.snapshot()["points"][1]["pv_potential_w"], 1674)

    def test_solar_elevation_daylight_gate(self):
        noon = solar_elevation_degrees(
            dt.datetime(2026, 3, 20, 12, tzinfo=dt.timezone.utc),
            0,
            0,
        )
        midnight = solar_elevation_degrees(
            dt.datetime(2026, 3, 20, 0, tzinfo=dt.timezone.utc),
            0,
            0,
        )
        self.assertGreater(noon, 80)
        self.assertLess(midnight, -80)

    def test_correction_uses_mature_long_factor_and_forces_night_zero(self):
        issued = dt.datetime(2026, 3, 20, 12, tzinfo=dt.timezone.utc)

        class History:
            def forecast_comparison(self, **_):
                return [
                    {
                        "forecast_at": (
                            issued
                            - dt.timedelta(days=index % 20, hours=3)
                        ).isoformat(),
                        "forecast_power_w": 1000,
                        "actual_power_w": 800,
                    }
                    for index in range(420)
                ]

            def forecast_samples(self, **_):
                return []

        config = ForecastSolarConfig(
            enabled=True,
            planes=(ForecastPlane("array", 25, 0, 10),),
        )
        corrected, summary = ForecastCorrector(
            config,
            "UTC",
            History(),
        ).correct(
            [
                {"timestamp": issued.isoformat(), "power_w": 5000},
                {
                    "timestamp": issued.replace(hour=0).isoformat(),
                    "power_w": 500,
                },
            ],
            issued_at=issued.isoformat(),
            latitude=0,
            longitude=0,
        )
        self.assertTrue(summary["long_term_ready"])
        self.assertAlmostEqual(summary["long_term_factor"], 0.8)
        self.assertEqual(corrected[0]["power_w"], 4000)
        self.assertEqual(corrected[1]["power_w"], 0)
        self.assertFalse(corrected[1]["daylight_gate"])

    def test_correction_learns_separate_morning_and_afternoon_bias(self):
        issued = dt.datetime(2026, 4, 1, 0, tzinfo=dt.timezone.utc)

        class History:
            def forecast_comparison(self, **_):
                samples = []
                for index in range(300):
                    day = issued - dt.timedelta(days=index % 30 + 1)
                    for hour, ratio in ((8, 0.7), (16, 0.9)):
                        samples.append(
                            {
                                "forecast_at": day.replace(hour=hour).isoformat(),
                                "forecast_power_w": 1000,
                                "actual_power_w": 1000 * ratio,
                            }
                        )
                return samples

            def forecast_samples(self, **_):
                return []

        config = ForecastSolarConfig(
            enabled=True,
            planes=(ForecastPlane("array", 25, 0, 10),),
        )
        corrected, summary = ForecastCorrector(
            config,
            "UTC",
            History(),
        ).correct(
            [
                {
                    "timestamp": issued.replace(hour=8).isoformat(),
                    "power_w": 5000,
                },
                {
                    "timestamp": issued.replace(hour=16).isoformat(),
                    "power_w": 5000,
                },
            ],
            issued_at=issued.isoformat(),
            latitude=0,
            longitude=0,
        )
        self.assertTrue(summary["long_term_ready"])
        self.assertEqual(corrected[0]["power_w"], 3500)
        self.assertEqual(corrected[1]["power_w"], 4500)
        self.assertEqual(len(summary["long_term_time_bucket_factors"]), 2)

    def test_forecast_control_gate_requires_independent_horizon_history(self):
        now = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)

        class History:
            def forecast_samples(self, **_):
                samples = []
                for index in range(300):
                    forecast_at = now - dt.timedelta(days=index % 28 + 1)
                    for horizon in (1, 4, 12):
                        samples.append(
                            {
                                "forecast_at": forecast_at.isoformat(),
                                "horizon_hours": horizon,
                                "forecast_power_w": 5000,
                                "actual_power_w": 5000,
                                "error_w": 0,
                            }
                        )
                return samples

        config = ForecastSolarConfig(
            enabled=True,
            planes=(ForecastPlane("array", 25, 0, 10),),
        )
        validation = ForecastCorrector(config, "UTC", History())._validation(now)
        self.assertTrue(validation["passed"])
        self.assertEqual(validation["required_days"], 28)
        self.assertEqual(
            validation["required_samples_per_scored_horizon"],
            300,
        )

    def test_complete_example_configuration_loads(self):
        config = load_config(
            REPOSITORY / "config" / "solplanet-fasttalk.example.toml"
        )
        self.assertEqual(len(config.forecast_solar.planes), 2)
        self.assertEqual(config.forecast_solar.planes[0].peak_power_kw, 6.2)
        self.assertTrue(config.optimisation.enabled)

    def test_forecast_persistence_failure_keeps_live_cache_available(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return json.dumps(
                    {
                        "result": {
                            "watts": {"2026-06-01 12:00:00": 1234},
                            "watt_hours_day": {"2026-06-01": 4321},
                        }
                    }
                ).encode()

        class FailingHistory:
            def record_forecast(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("database is busy")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key"
            location = root / "location.json"
            cache = root / "cache.json"
            key.write_text("0123456789ABCDEF", encoding="utf-8")
            location.write_text(
                json.dumps({"latitude": 0, "longitude": 0}),
                encoding="utf-8",
            )
            config = ForecastSolarConfig(
                enabled=True,
                api_key_file=str(key),
                location_file=str(location),
                cache_file=str(cache),
                planes=(ForecastPlane("east", 25, -90, 6.2),),
            )
            state = PlantState()
            store = ForecastStore(config, "Australia/Sydney")
            worker = ForecastSolarWorker(
                config,
                "Australia/Sydney",
                store,
                state,
                FailingHistory(),
            )
            with patch(
                "solplanet_fasttalk.forecast.urlopen",
                return_value=Response(),
            ):
                worker._fetch()
            self.assertEqual(store.snapshot()["status"], "ok")
            self.assertEqual(worker.persistence_failures, 1)
            self.assertTrue(cache.is_file())

    def test_financial_ledger_applies_daily_and_earned_zerohero_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.sqlite3")
            initialize_database(database)
            local_start = dt.datetime(
                2026, 6, 1, 18, 0, tzinfo=self.sydney
            )
            with sqlite3.connect(database) as connection:
                connection.executemany(
                    """
                    INSERT INTO measurements (
                        observed_at, name, value_num, value_text, unit,
                        quality, source, authority, access_mode, metadata_json
                    ) VALUES (?, 'grid.active_power', 0, NULL, 'W', 'good',
                              'eastron.grid', 'authoritative', 'passive_bus', '{}')
                    """,
                    (
                        (
                            (
                                local_start
                                + dt.timedelta(minutes=minute)
                            ).astimezone(dt.timezone.utc).isoformat(),
                        )
                        for minute in range(180)
                    )
                )
                connection.commit()
            history = HistoryReader(database)
            worker = FinancialAccountingWorker(
                history,
                self.tariff,
                PlantState(),
                raw_retention_days=14,
            )
            written = worker.run_once(
                dt.datetime(2026, 6, 1, 21, 1, tzinfo=self.sydney)
            )
            summary = history.financial_summary()
            day = history.financial_day_state("2026-06-01")

        self.assertEqual(written, 180)
        self.assertEqual(day["adjustments"]["daily_supply"], 1.65)
        self.assertEqual(day["adjustments"]["zerohero_credit"], -1.0)
        self.assertAlmostEqual(summary["net_cost"], 0.65)


class Phase6Tests(unittest.TestCase):
    def test_shadow_simulation_is_constrained_and_improves_cost(self):
        config = OptimisationConfig(
            enabled=True,
            battery_capacity_kwh=10,
            reserve_soc_percent=10,
            maximum_soc_percent=90,
            max_charge_watts=3000,
            max_discharge_watts=3000,
            site_import_limit_watts=6000,
            site_export_limit_watts=6000,
        )
        tariff = ZeroHeroTariff(TariffConfig())
        timezone = ZoneInfo("Australia/Sydney")
        slots = [
            ForecastSlot(
                dt.datetime(2026, 6, 1, 12, 0, tzinfo=timezone),
                2000,
                5000,
            ),
            ForecastSlot(
                dt.datetime(2026, 6, 1, 17, 0, tzinfo=timezone),
                5000,
                0,
            ),
        ]
        result = simulate_plan(
            config,
            tariff,
            slots,
            initial_soc_percent=20,
            charge_limit_w=2500,
            discharge_limit_w=2000,
        )
        self.assertGreater(
            result["simulation"]["estimated_cost_improvement"], 0
        )
        for recommendation in result["recommendations"]:
            self.assertLessEqual(abs(recommendation["battery_power_w"]), 2500)
            self.assertLessEqual(
                recommendation["expected_grid_power_w"],
                config.site_import_limit_watts,
            )
            self.assertGreaterEqual(
                recommendation["expected_soc_percent"],
                config.reserve_soc_percent,
            )
            self.assertTrue(recommendation["explanation"])

    def test_stale_required_input_produces_no_action(self):
        config = OptimisationConfig(enabled=True)
        state = PlantState()
        state.publish_many(
            [
                measurement("battery.soc", 50, age=20, max_age=1, unit="%"),
                measurement("grid.active_power", 100),
                measurement("external_pv.active_power", 500),
                measurement("asw.active_power", 0),
                measurement("asw.pv.active_power", 0),
            ]
        )
        forecast_config = ForecastSolarConfig(
            enabled=True,
            planes=(ForecastPlane("east", 25, -90, 6.2),),
        )
        plans = PlanStore()
        worker = OptimisationWorker(
            config,
            state,
            ForecastStore(forecast_config, "Australia/Sydney"),
            ZeroHeroTariff(TariffConfig()),
            plans,
        )
        plan = worker.plan_once()
        self.assertEqual(plan["status"], "no_action")
        self.assertEqual(plan["control_commands_sent"], 0)
        self.assertIn("battery.soc", plan["reason"])

    def test_native_charge_command_is_the_no_change_baseline(self):
        tariff = ZeroHeroTariff(TariffConfig())
        config = OptimisationConfig(
            enabled=True,
            battery_capacity_kwh=10,
            reserve_soc_percent=10,
            maximum_soc_percent=90,
        )
        slot = ForecastSlot(
            dt.datetime(2026, 6, 1, 12, 0, tzinfo=ZoneInfo("Australia/Sydney")),
            1000,
            1000,
        )
        result = simulate_plan(
            config,
            tariff,
            [slot],
            initial_soc_percent=50,
            charge_limit_w=50000,
            discharge_limit_w=50000,
            native_baseline=NativeBaseline(
                mode="charge",
                requested_power_w=12000,
                minimum_soc_percent=10,
                maximum_soc_percent=100,
                source="test",
                assumption="persist",
            ),
        )
        recommendation = result["recommendations"][0]
        self.assertEqual(recommendation["baseline_battery_power_w"], -12000)
        self.assertEqual(recommendation["baseline_grid_power_w"], 12000)
        self.assertEqual(
            recommendation["constraints"]["charge_limit_w"],
            12000,
        )
        self.assertEqual(
            result["simulation"]["baseline"]["policy"]["mode"],
            "charge",
        )

    def test_plan_history_persists_summary_and_full_workings(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.sqlite3")
            initialize_database(database)
            history = HistoryReader(database)
            plan = {
                "generated_at": "2026-06-01T00:00:00+00:00",
                "status": "ready",
                "mode": "shadow",
                "recommendations": [{"action": "hold"}],
                "simulation": {
                    "baseline": {"cost": 2.0},
                    "optimized": {"cost": 1.5},
                    "estimated_cost_improvement": 0.5,
                },
            }
            history.record_plan(plan)
            stored = history.plans(include_plan=True)[0]

        self.assertEqual(stored["estimated_cost_improvement"], 0.5)
        self.assertEqual(stored["plan"]["recommendations"][0]["action"], "hold")


if __name__ == "__main__":
    unittest.main()
