"""SQLite measurement and event history."""

from __future__ import annotations

import json
import datetime as dt
import sqlite3
import threading
import time
from pathlib import Path
from queue import Empty
from typing import Any

from .model import Measurement, MeasurementQueue, utc_now


def dt_from_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    return (
        parsed.replace(tzinfo=dt.timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(dt.timezone.utc)
    )


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
CREATE TABLE IF NOT EXISTS measurement_rollups (
    period_start TEXT NOT NULL,
    period_seconds INTEGER NOT NULL,
    name TEXT NOT NULL,
    samples INTEGER NOT NULL,
    value_avg REAL,
    value_min REAL,
    value_max REAL,
    value_last REAL,
    unit TEXT NOT NULL,
    quality TEXT NOT NULL,
    source TEXT NOT NULL,
    authority TEXT NOT NULL,
    PRIMARY KEY (period_start, period_seconds, name)
);
CREATE INDEX IF NOT EXISTS rollups_name_time
    ON measurement_rollups(name, period_seconds, period_start);
CREATE TABLE IF NOT EXISTS forecast_points (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    forecast_at TEXT NOT NULL,
    power_w REAL NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(provider, issued_at, forecast_at)
);
CREATE INDEX IF NOT EXISTS forecast_points_time
    ON forecast_points(provider, forecast_at, issued_at);
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
                        # Give forecast/event writers a bounded opportunity to
                        # acquire SQLite's single WAL write lock. Without this
                        # handoff a continuously populated measurement queue
                        # can immediately begin another transaction and starve
                        # lower-frequency writers.
                        time.sleep(0.01)
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
        resolution: str = "raw",
    ) -> list[dict[str, Any]]:
        if resolution != "raw":
            return self.rollups(
                name,
                resolution=resolution,
                since=since,
                until=until,
                limit=limit,
            )
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

    def rollups(
        self,
        name: str,
        *,
        resolution: str,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        seconds = {"hourly": 3600, "daily": 86400}.get(resolution)
        if seconds is None:
            raise ValueError("resolution must be raw, hourly, or daily")
        clauses = ["name = ?", "period_seconds = ?"]
        values: list[Any] = [name, seconds]
        if since:
            clauses.append("period_start >= ?")
            values.append(since)
        if until:
            clauses.append("period_start <= ?")
            values.append(until)
        values.append(max(1, min(limit, 10000)))
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                f"""
                SELECT period_start, samples, value_avg, value_min, value_max,
                       value_last, unit, quality, source, authority
                FROM measurement_rollups
                WHERE {' AND '.join(clauses)}
                ORDER BY period_start DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [
            {
                "observed_at": row[0],
                "name": name,
                "value": row[5] if ".energy." in name else row[2],
                "unit": row[6],
                "quality": row[7],
                "source": row[8],
                "authority": row[9],
                "access_mode": "rollup",
                "metadata": {
                    "resolution": resolution,
                    "samples": row[1],
                    "minimum": row[3],
                    "maximum": row[4],
                    "last": row[5],
                    "average": row[2],
                },
            }
            for row in rows
        ]

    def series(
        self,
        name: str,
        *,
        since: str | None = None,
        until: str | None = None,
        bucket_seconds: int = 300,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return graph-ready averages without transferring raw high-rate data."""

        if not 10 <= bucket_seconds <= 86400:
            raise ValueError("bucket_seconds must be between 10 and 86400")
        clauses = ["name = ?", "value_num IS NOT NULL"]
        values: list[Any] = [name]
        if since:
            clauses.append("observed_at >= ?")
            values.append(since)
        if until:
            clauses.append("observed_at <= ?")
            values.append(until)
        query_limit = max(1, min(limit, 10000))
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    CAST(strftime('%s', observed_at) AS INTEGER) / ? AS bucket,
                    AVG(value_num), MIN(value_num), MAX(value_num), COUNT(*),
                    MAX(unit),
                    CASE WHEN MIN(quality) = 'good' AND MAX(quality) = 'good'
                         THEN 'good' ELSE 'mixed' END,
                    MAX(source), MAX(authority)
                FROM measurements
                WHERE {' AND '.join(clauses)}
                GROUP BY bucket
                ORDER BY bucket DESC
                LIMIT ?
                """,
                [bucket_seconds, *values, query_limit],
            ).fetchall()
        return [
            {
                "observed_at": dt.datetime.fromtimestamp(
                    int(row[0]) * bucket_seconds,
                    tz=dt.timezone.utc,
                ).isoformat(),
                "name": name,
                "value": row[1],
                "unit": row[5],
                "quality": row[6],
                "source": row[7],
                "authority": row[8],
                "access_mode": "time_bucket",
                "metadata": {
                    "bucket_seconds": bucket_seconds,
                    "samples": row[4],
                    "minimum": row[2],
                    "maximum": row[3],
                },
            }
            for row in rows
        ]

    def counter_baselines(self) -> dict[str, dict[str, Any]]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT m.name, m.observed_at, m.value_num, m.unit, m.source
                FROM measurements AS m
                JOIN (
                    SELECT name, MAX(id) AS id
                    FROM measurements
                    WHERE value_num IS NOT NULL AND name LIKE '%.energy.%'
                    GROUP BY name
                ) AS latest ON latest.id = m.id
                """
            ).fetchall()
        return {
            row[0]: {
                "observed_at": row[1],
                "value": row[2],
                "unit": row[3],
                "source": row[4],
            }
            for row in rows
        }

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

    def record_forecast(
        self,
        provider: str,
        issued_at: str,
        points: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO forecast_points (
                    provider, issued_at, forecast_at, power_w, metadata_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        provider,
                        issued_at,
                        point["timestamp"],
                        float(point["power_w"]),
                        json.dumps(metadata or {}, separators=(",", ":")),
                    )
                    for point in points
                ),
            )
            connection.commit()

    def forecast_comparison(
        self,
        *,
        provider: str = "forecast.solar",
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["provider = ?"]
        values: list[Any] = [provider]
        if since:
            clauses.append("forecast_at >= ?")
            values.append(since)
        if until:
            clauses.append("forecast_at <= ?")
            values.append(until)
        values.append(max(1, min(limit, 10000)))
        with sqlite3.connect(self.path) as connection:
            forecasts = connection.execute(
                f"""
                SELECT issued_at, forecast_at, power_w, metadata_json
                FROM forecast_points AS candidate
                WHERE {' AND '.join(clauses)}
                  AND id = (
                      SELECT MAX(latest.id) FROM forecast_points AS latest
                      WHERE latest.provider = candidate.provider
                        AND latest.forecast_at = candidate.forecast_at
                        AND latest.issued_at <= candidate.forecast_at
                  )
                ORDER BY forecast_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
            if not forecasts:
                return []
            earliest = min(row[1] for row in forecasts)
            latest = max(row[1] for row in forecasts)
            actuals = connection.execute(
                """
                SELECT observed_at, value_num, source, authority
                FROM measurements
                WHERE name = 'external_pv.active_power'
                  AND value_num IS NOT NULL
                  AND julianday(observed_at) >= julianday(?, '-15 minutes')
                  AND julianday(observed_at) <= julianday(?, '+15 minutes')
                ORDER BY observed_at
                """,
                (earliest, latest),
            ).fetchall()
        parsed_actuals = [
            (dt_from_iso(row[0]), row) for row in actuals
        ]
        result = []
        for issued_at, forecast_at, power_w, metadata_json in forecasts:
            target = dt_from_iso(forecast_at)
            closest = (
                min(
                    parsed_actuals,
                    key=lambda value: abs(
                        (value[0] - target).total_seconds()
                    ),
                )
                if parsed_actuals
                else None
            )
            if closest and abs((closest[0] - target).total_seconds()) <= 900:
                row = closest[1]
                actual = float(row[1])
                result.append(
                    {
                        "forecast_at": forecast_at,
                        "issued_at": issued_at,
                        "forecast_power_w": power_w,
                        "actual_power_w": actual,
                        "error_w": actual - power_w,
                        "actual_observed_at": row[0],
                        "actual_source": row[2],
                        "actual_authority": row[3],
                        "metadata": json.loads(metadata_json),
                    }
                )
        return result


class StorageMaintainer:
    """Build rollups before applying bounded retention."""

    def __init__(self, path: str, config) -> None:
        self.path = path
        self.config = config
        self.runs = 0
        self.failures = 0

    def run_once(self) -> None:
        with sqlite3.connect(self.path) as connection:
            for seconds, pattern in (
                (3600, "%Y-%m-%dT%H:00:00+00:00"),
                (86400, "%Y-%m-%dT00:00:00+00:00"),
            ):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO measurement_rollups (
                        period_start, period_seconds, name, samples,
                        value_avg, value_min, value_max, value_last,
                        unit, quality, source, authority
                    )
                    SELECT strftime(?, observed_at), ?, name, COUNT(*),
                           AVG(value_num), MIN(value_num), MAX(value_num),
                           (
                               SELECT last.value_num FROM measurements AS last
                               WHERE last.name = measurements.name
                                 AND strftime(?, last.observed_at) =
                                     strftime(?, measurements.observed_at)
                                 AND last.value_num IS NOT NULL
                               ORDER BY last.observed_at DESC, last.id DESC
                               LIMIT 1
                           ),
                           MAX(unit),
                           CASE WHEN MIN(quality) = 'good' AND
                                     MAX(quality) = 'good'
                                THEN 'good' ELSE 'mixed' END,
                           MAX(source), MAX(authority)
                    FROM measurements
                    WHERE value_num IS NOT NULL
                    GROUP BY strftime(?, observed_at), name
                    """,
                    (pattern, seconds, pattern, pattern, pattern),
                )
            connection.execute(
                """
                DELETE FROM measurements
                WHERE julianday(observed_at) < julianday('now', ?)
                """,
                (f"-{self.config.raw_retention_days} days",),
            )
            connection.execute(
                """
                DELETE FROM measurement_rollups
                WHERE period_seconds = 3600
                  AND julianday(period_start) < julianday('now', ?)
                """,
                (f"-{self.config.hourly_retention_days} days",),
            )
            connection.execute(
                """
                DELETE FROM measurement_rollups
                WHERE period_seconds = 86400
                  AND julianday(period_start) < julianday('now', ?)
                """,
                (f"-{self.config.daily_retention_days} days",),
            )
            connection.commit()
        self.runs += 1

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except sqlite3.Error:
                self.failures += 1
            elapsed = time.monotonic() - started
            stop.wait(max(1.0, self.config.maintenance_interval_seconds - elapsed))
