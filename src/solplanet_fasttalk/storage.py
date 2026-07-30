"""SQLite measurement and event history."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from queue import Empty
from typing import Any

from .model import Measurement, MeasurementQueue, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    name TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    unit TEXT NOT NULL,
    quality TEXT NOT NULL,
    source TEXT NOT NULL,
    authority TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS measurements_name_time
    ON measurements(name, observed_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    severity TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events(occurred_at);
"""


def initialize_database(path: str) -> None:
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(SCHEMA)


class HistoryWriter:
    def __init__(self, path: str, queue: MeasurementQueue) -> None:
        self.path = path
        self.queue = queue
        self.written = 0
        self.failures = 0

    def run(self, stop: threading.Event) -> None:
        initialize_database(self.path)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            pending = 0
            while not stop.is_set() or not self.queue.queue.empty():
                try:
                    item = self.queue.queue.get(timeout=0.5)
                except Empty:
                    if pending:
                        connection.commit()
                        pending = 0
                    continue
                if item is None:
                    break
                try:
                    number = (
                        float(item.value)
                        if isinstance(item.value, (int, float))
                        and not isinstance(item.value, bool)
                        else None
                    )
                    text = None if number is not None or item.value is None else str(item.value)
                    connection.execute(
                        """
                        INSERT INTO measurements (
                            observed_at, name, value_num, value_text, unit,
                            quality, source, authority, access_mode, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.observed_at,
                            item.name,
                            number,
                            text,
                            item.unit,
                            item.quality,
                            item.source,
                            item.authority,
                            item.access_mode,
                            json.dumps(item.metadata, separators=(",", ":")),
                        ),
                    )
                    self.written += 1
                    pending += 1
                    if pending >= 100:
                        connection.commit()
                        pending = 0
                except sqlite3.Error:
                    self.failures += 1
            connection.commit()


class HistoryReader:
    def __init__(self, path: str) -> None:
        self.path = path

    def measurements(
        self,
        name: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["name = ?"]
        values: list[Any] = [name]
        if since:
            clauses.append("observed_at >= ?")
            values.append(since)
        if until:
            clauses.append("observed_at <= ?")
            values.append(until)
        values.append(max(1, min(limit, 10000)))
        query = f"""
            SELECT observed_at, name, value_num, value_text, unit, quality,
                   source, authority, access_mode, metadata_json
            FROM measurements
            WHERE {' AND '.join(clauses)}
            ORDER BY observed_at DESC, id DESC
            LIMIT ?
        """
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(query, values).fetchall()
        return [
            {
                "observed_at": row[0],
                "name": row[1],
                "value": row[2] if row[2] is not None else row[3],
                "unit": row[4],
                "quality": row[5],
                "source": row[6],
                "authority": row[7],
                "access_mode": row[8],
                "metadata": json.loads(row[9]),
            }
            for row in rows
        ]

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT occurred_at, severity, component, message, details_json
                FROM events ORDER BY occurred_at DESC, id DESC LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [
            {
                "occurred_at": row[0],
                "severity": row[1],
                "component": row[2],
                "message": row[3],
                "details": json.loads(row[4]),
            }
            for row in rows
        ]

    def record_event(
        self,
        severity: str,
        component: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO events (
                    occurred_at, severity, component, message, details_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    severity,
                    component,
                    message,
                    json.dumps(details or {}, separators=(",", ":")),
                ),
            )
            connection.commit()
