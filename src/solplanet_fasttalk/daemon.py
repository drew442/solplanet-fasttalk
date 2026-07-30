"""Daemon lifecycle and worker orchestration."""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path

from .api import API
from .asw import ASWWorker
from .config import DaemonConfig
from .eastron import EastronWorker
from .model import MeasurementQueue, PlantState
from .storage import HistoryReader, HistoryWriter, initialize_database


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
        self.api = API(config, self.state, self.history)
        self.threads: dict[str, threading.Thread] = {}

    def start(self) -> None:
        self.history.record_event(
            "info",
            "daemon",
            "daemon started",
            {
                "control_available": False,
                "database": str(Path(self.config.database)),
            },
        )
        self._thread("history", self.writer.run)
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
            )
            host, port = self.api.address
            self.state.update_health(
                "api",
                status="ok",
                bind=f"{host}:{port}",
                authenticated=False,
                control_available=False,
            )
            stop.wait(5.0)

    def run_forever(self) -> None:
        self.start()
        self.stop_event.wait()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.api.close()
        for name in ("eastron", "asw", "monitor", "api"):
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
