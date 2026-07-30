"""Daemon lifecycle and worker orchestration."""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path

from .api import API
from .asw import ASWWorker
from .config import DaemonConfig
from .eastron import EastronWorker
from .forecast import ForecastSolarWorker, ForecastStore
from .model import MeasurementQueue, PlantState
from .optimisation import OptimisationWorker, PlanStore
from .plugins import PluginRegistry
from .solis import SolisPlugin
from .storage import (
    HistoryReader,
    HistoryWriter,
    StorageMaintainer,
    initialize_database,
)
from .tariff import ZeroHeroTariff


LOG = logging.getLogger(__name__)


class Daemon:
    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.state = PlantState()
        self.measurement_queue = MeasurementQueue()
        self.state.add_sink(self.measurement_queue.put)
        initialize_database(config.database)
        self.history = HistoryReader(config.database)
        self.writer = HistoryWriter(config.database, self.measurement_queue)
        self.maintainer = StorageMaintainer(config.database, config.storage)
        self.plugins = PluginRegistry()
        self.tariff = (
            ZeroHeroTariff(config.tariff) if config.tariff.enabled else None
        )
        self.forecast = (
            ForecastStore(config.forecast_solar, config.tariff.timezone)
            if config.forecast_solar.enabled
            else None
        )
        self.plans = PlanStore() if config.optimisation.enabled else None
        self.api = API(
            config,
            self.state,
            self.history,
            tariff=self.tariff,
            forecast=self.forecast,
            plans=self.plans,
            plugins=self.plugins,
        )
        self.threads: dict[str, threading.Thread] = {}
        self._mismatch_since: float | None = None
        self._mismatch_reported = False

    def start(self) -> None:
        self.history.record_event(
            "info",
            "daemon",
            "daemon started",
            {
                "control_available": False,
                "database": str(Path(self.config.database)),
                "mode": "shadow",
                "restored_counter_baselines": len(
                    self.history.counter_baselines()
                ),
            },
        )
        self._thread("history", self.writer.run)
        self._thread("storage-maintenance", self.maintainer.run)
        self._thread("api", lambda _: self.api.serve())
        self._thread("monitor", self._monitor)
        if self.config.eastron.enabled:
            worker = EastronWorker(self.config.eastron, self.state)
            self._thread("eastron", worker.run)
        else:
            self.state.update_health("eastron", status="disabled")
        if self.config.asw.enabled:
            worker = ASWWorker(self.config.asw, self.state)
            self._thread("asw", worker.run)
        else:
            self.state.update_health("asw", status="disabled")
        if self.config.solis.enabled:
            worker = SolisPlugin(self.config.solis, self.state)
            self.plugins.register(worker.descriptor)
            self._thread("solis", worker.run)
        else:
            self.state.update_health("solis", status="disabled")
        if self.forecast is not None:
            worker = ForecastSolarWorker(
                self.config.forecast_solar,
                self.config.tariff.timezone,
                self.forecast,
                self.state,
                self.history,
            )
            self._thread("forecast-solar", worker.run)
        else:
            self.state.update_health("forecast_solar", status="disabled")
        if self.plans is not None and self.forecast is not None and self.tariff:
            worker = OptimisationWorker(
                self.config.optimisation,
                self.state,
                self.forecast,
                self.tariff,
                self.plans,
                self.history,
            )
            self._thread("optimisation", worker.run)
        else:
            self.state.update_health(
                "optimisation",
                status="disabled",
                mode="shadow",
                control_commands_sent=0,
            )
        host, port = self.api.address
        LOG.info("API listening on http://%s:%s", host, port)

    def _thread(self, name: str, target) -> None:
        thread = threading.Thread(
            name=f"fasttalk-{name}",
            target=target,
            args=(self.stop_event,),
            daemon=True,
        )
        thread.start()
        self.threads[name] = thread

    def _monitor(self, stop: threading.Event) -> None:
        while not stop.is_set():
            persistence_degraded = bool(
                self.writer.failures or self.measurement_queue.dropped
            )
            self.state.update_health(
                "storage",
                status="degraded" if persistence_degraded else "ok",
                database=self.config.database,
                measurements_written=self.writer.written,
                write_failures=self.writer.failures,
                queue_depth=self.measurement_queue.queue.qsize(),
                queue_dropped=self.measurement_queue.dropped,
                maintenance_runs=self.maintainer.runs,
                maintenance_failures=self.maintainer.failures,
            )
            host, port = self.api.address
            self.state.update_health(
                "api",
                status="ok",
                bind=f"{host}:{port}",
                authenticated=self.api.auth_token is not None,
                control_available=False,
                diagnostics_ui="/diagnostics/",
            )
            if self.forecast is not None:
                self.forecast.update_actual(self.state)
            self._check_solis_mismatch()
            stop.wait(5.0)

    def _check_solis_mismatch(self) -> None:
        if not self.config.solis.enabled:
            return
        current = self.state.current()
        authoritative = current.get("external_pv.active_power")
        diagnostic = current.get("solis.active_power")
        usable = (
            authoritative
            and diagnostic
            and authoritative["quality"] == diagnostic["quality"] == "good"
            and isinstance(authoritative["value"], (int, float))
            and isinstance(diagnostic["value"], (int, float))
        )
        now = time.monotonic()
        difference = (
            abs(float(authoritative["value"]) - float(diagnostic["value"]))
            if usable
            else 0.0
        )
        if usable and difference > self.config.solis.mismatch_tolerance_watts:
            self._mismatch_since = self._mismatch_since or now
            if (
                not self._mismatch_reported
                and now - self._mismatch_since
                >= self.config.solis.mismatch_duration_seconds
            ):
                self.history.record_event(
                    "warning",
                    "source_resolver",
                    "Solis diagnostic power disagrees with authoritative Eastron",
                    {
                        "difference_watts": round(difference, 3),
                        "authority_retained": "eastron.external_pv",
                        "diagnostic_source": "plugin.solis_rs485",
                    },
                )
                self._mismatch_reported = True
        else:
            if self._mismatch_reported:
                self.history.record_event(
                    "info",
                    "source_resolver",
                    "Solis and authoritative Eastron power agreement restored",
                    {"authority_retained": "eastron.external_pv"},
                )
            self._mismatch_since = None
            self._mismatch_reported = False

    def run_forever(self) -> None:
        self.start()
        self.stop_event.wait()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.api.close()
        for name in tuple(self.threads):
            if name == "history":
                continue
            thread = self.threads.get(name)
            if thread is not None:
                thread.join(timeout=5)
        self.measurement_queue.queue.put(None)
        history_thread = self.threads.get("history")
        if history_thread is not None:
            history_thread.join(timeout=5)
        self.history.record_event("info", "daemon", "daemon stopped")

    def install_signal_handlers(self) -> None:
        def request_stop(_signum, _frame) -> None:
            self.stop()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
