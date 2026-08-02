"""SQLite measurement and event history."""

from __future__ import annotations

import json
import datetime as dt
import math
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from queue import Empty, Full
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
CREATE TABLE IF NOT EXISTS forecast_context_points (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    forecast_at TEXT NOT NULL,
    features_json TEXT NOT NULL,
    UNIQUE(provider, issued_at, forecast_at)
);
CREATE INDEX IF NOT EXISTS forecast_context_time
    ON forecast_context_points(provider, forecast_at, issued_at);
CREATE TABLE IF NOT EXISTS prediction_points (
    id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    signal TEXT NOT NULL,
    scenario TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    prediction_at TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    lower_value REAL,
    upper_value REAL,
    features_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(model, model_version, signal, scenario, issued_at, prediction_at)
);
CREATE INDEX IF NOT EXISTS prediction_points_time
    ON prediction_points(signal, scenario, prediction_at, issued_at);
CREATE TABLE IF NOT EXISTS financial_intervals (
    period_start TEXT PRIMARY KEY,
    local_date TEXT NOT NULL,
    local_hour INTEGER NOT NULL,
    average_grid_power_w REAL NOT NULL,
    imported_kwh REAL NOT NULL,
    exported_kwh REAL NOT NULL,
    import_price_per_kwh REAL NOT NULL,
    export_price_per_kwh REAL NOT NULL,
    import_cost REAL NOT NULL,
    export_credit REAL NOT NULL,
    net_energy_cost REAL NOT NULL,
    samples INTEGER NOT NULL,
    import_period TEXT NOT NULL,
    export_period TEXT NOT NULL,
    plan_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS financial_intervals_date
    ON financial_intervals(local_date, period_start);
CREATE TABLE IF NOT EXISTS financial_adjustments (
    local_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    PRIMARY KEY (local_date, kind)
);
CREATE INDEX IF NOT EXISTS financial_adjustments_time
    ON financial_adjustments(occurred_at);
CREATE TABLE IF NOT EXISTS plan_history (
    generated_at TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    reason TEXT,
    recommendation_count INTEGER NOT NULL,
    estimated_cost_improvement REAL,
    baseline_cost REAL,
    optimized_cost REAL,
    plan_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plan_history_time
    ON plan_history(generated_at);
CREATE TABLE IF NOT EXISTS storage_observations (
    observed_at TEXT PRIMARY KEY,
    database_bytes INTEGER NOT NULL,
    wal_bytes INTEGER NOT NULL,
    shm_bytes INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    allocated_bytes INTEGER NOT NULL,
    used_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS storage_observations_time
    ON storage_observations(observed_at);
"""


# Selected signals retained at 15-minute resolution for seasonal forecasting
# and future model training. Identity strings, verbose diagnostics and redundant
# phase data stay at their existing raw/hourly retention levels.
MODEL_TRAINING_SIGNALS = (
    "grid.active_power",
    "external_pv.active_power",
    "site.load_power",
    "site.pv_generation_power",
    "site.local_supply_power",
    "asw.active_power",
    "battery.power",
    "battery.soc",
    "battery.soh",
    "battery.voltage",
    "battery.temperature",
    "battery.limit.charge_current",
    "battery.limit.discharge_current",
    "battery.limit.soc_lower",
    "battery.limit.soc_upper",
    "asw.control.run_mode",
    "asw.control.charge_discharge_state",
    "asw.control.power_command",
)


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
        self.consecutive_failures = 0

    def run(self, stop: threading.Event) -> None:
        initialize_database(self.path)
        with sqlite3.connect(self.path, timeout=30.0) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            shutdown_requested = False
            while not stop.is_set() or not self.queue.queue.empty():
                try:
                    item = self.queue.queue.get(timeout=0.5)
                except Empty:
                    continue
                if item is None:
                    break
                batch = [item]
                while len(batch) < 100:
                    try:
                        candidate = self.queue.queue.get_nowait()
                    except Empty:
                        break
                    if candidate is None:
                        shutdown_requested = True
                        break
                    batch.append(candidate)
                try:
                    connection.executemany(
                        """
                        INSERT INTO measurements (
                            observed_at, name, value_num, value_text, unit,
                            quality, source, authority, access_mode, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self._row(measurement)
                            for measurement in batch
                        ),
                    )
                    connection.commit()
                    self.written += len(batch)
                    self.consecutive_failures = 0
                    # Give lower-frequency forecast/accounting writers a
                    # bounded opportunity to acquire SQLite's WAL write lock.
                    time.sleep(0.01)
                except sqlite3.Error:
                    connection.rollback()
                    self.failures += 1
                    self.consecutive_failures += 1
                    # A transient SQLite writer collision must not silently
                    # discard telemetry. Requeue the complete rolled-back
                    # batch; serial workers remain non-blocking and the queue
                    # retains its existing bounded-drop behavior.
                    for measurement in batch:
                        try:
                            self.queue.queue.put_nowait(measurement)
                        except Full:
                            self.queue.dropped += 1
                    time.sleep(min(1.0, 0.05 * self.consecutive_failures))
                    shutdown_requested = False
                if shutdown_requested:
                    break

    @staticmethod
    def _row(item: Measurement) -> tuple[Any, ...]:
        number = (
            float(item.value)
            if isinstance(item.value, (int, float))
            and not isinstance(item.value, bool)
            else None
        )
        text = None if number is not None or item.value is None else str(item.value)
        return (
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
        )


class HistoryReader:
    def __init__(self, path: str) -> None:
        self.path = path
        self._storage_cache: dict[str, Any] | None = None
        self._storage_cache_at = 0.0
        self._storage_cache_lock = threading.RLock()

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
        seconds = {
            "quarter_hour": 900,
            "hourly": 3600,
            "daily": 86400,
        }.get(resolution)
        if seconds is None:
            raise ValueError(
                "resolution must be raw, quarter_hour, hourly, or daily"
            )
        clauses = ["name = ?", "period_seconds = ?"]
        values: list[Any] = [name, seconds]
        if since:
            clauses.append("period_start >= ?")
            values.append(since)
        if until:
            clauses.append("period_start <= ?")
            values.append(until)
        values.append(max(1, min(limit, 100000)))
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
        with sqlite3.connect(self.path, timeout=30.0) as connection:
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

    def record_forecast_context(
        self,
        provider: str,
        issued_at: str,
        points: list[dict[str, Any]],
    ) -> None:
        """Persist sanitized forecast vintages for later feature engineering."""

        if not points:
            return
        with sqlite3.connect(self.path, timeout=30.0) as connection:
            latest = connection.execute(
                """
                SELECT MAX(issued_at) FROM forecast_context_points
                WHERE provider = ?
                """,
                (provider,),
            ).fetchone()[0]
            if latest and (
                dt_from_iso(issued_at) - dt_from_iso(latest)
            ).total_seconds() < 3600:
                return
            connection.executemany(
                """
                INSERT OR REPLACE INTO forecast_context_points (
                    provider, issued_at, forecast_at, features_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        provider,
                        issued_at,
                        point["timestamp"],
                        json.dumps(
                            {
                                key: value
                                for key, value in point.items()
                                if key != "timestamp"
                            },
                            separators=(",", ":"),
                        ),
                    )
                    for point in points
                ),
            )
            connection.commit()

    def record_predictions(
        self,
        *,
        model: str,
        model_version: str,
        signal: str,
        scenario: str,
        issued_at: str,
        unit: str,
        points: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a complete prediction vintage and its causal features."""

        if not points:
            return
        shared = json.dumps(metadata or {}, separators=(",", ":"))
        with sqlite3.connect(self.path, timeout=30.0) as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO prediction_points (
                    model, model_version, signal, scenario, issued_at,
                    prediction_at, value, unit, lower_value, upper_value,
                    features_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        model,
                        model_version,
                        signal,
                        scenario,
                        issued_at,
                        point["timestamp"],
                        float(point["value"]),
                        unit,
                        (
                            float(point["lower"])
                            if point.get("lower") is not None
                            else None
                        ),
                        (
                            float(point["upper"])
                            if point.get("upper") is not None
                            else None
                        ),
                        json.dumps(
                            point.get("features") or {},
                            separators=(",", ":"),
                        ),
                        shared,
                    )
                    for point in points
                ),
            )
            connection.commit()

    def latest_prediction_issue(self) -> str | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT issued_at FROM prediction_points
                GROUP BY issued_at
                HAVING COUNT(DISTINCT signal || ':' || scenario) >= 3
                ORDER BY issued_at DESC LIMIT 1
                """
            ).fetchone()
        return row[0] if row and row[0] else None

    def prediction_samples(
        self,
        *,
        signal: str,
        scenario: str,
        model: str | None = None,
        model_version: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        """Match prediction vintages to actual raw or 15-minute observations."""

        clauses = ["signal = ?", "scenario = ?"]
        values: list[Any] = [signal, scenario]
        if model:
            clauses.append("model = ?")
            values.append(model)
        if model_version:
            clauses.append("model_version = ?")
            values.append(model_version)
        if since:
            clauses.append("prediction_at >= ?")
            values.append(since)
        if until:
            clauses.append("prediction_at <= ?")
            values.append(until)
        values.append(max(1, min(limit, 100000)))
        with sqlite3.connect(self.path) as connection:
            predictions = connection.execute(
                f"""
                SELECT model, model_version, issued_at, prediction_at,
                       value, unit, lower_value, upper_value,
                       features_json, metadata_json
                FROM prediction_points
                WHERE {' AND '.join(clauses)}
                ORDER BY prediction_at DESC, issued_at DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            if not predictions:
                return []
            earliest = min(row[3] for row in predictions)
            latest = max(row[3] for row in predictions)
            raw = connection.execute(
                """
                SELECT
                    CAST(strftime('%s', observed_at) AS INTEGER) / 60,
                    AVG(value_num)
                FROM measurements
                WHERE name = ? AND value_num IS NOT NULL
                  AND julianday(observed_at) >= julianday(?, '-15 minutes')
                  AND julianday(observed_at) <= julianday(?, '+15 minutes')
                GROUP BY 1
                """,
                (signal, earliest, latest),
            ).fetchall()
            rollups = connection.execute(
                """
                SELECT
                    CAST(strftime('%s', period_start) AS INTEGER) / 60,
                    value_avg
                FROM measurement_rollups
                WHERE name = ? AND period_seconds = 900
                  AND value_avg IS NOT NULL
                  AND julianday(period_start) >= julianday(?, '-15 minutes')
                  AND julianday(period_start) <= julianday(?, '+15 minutes')
                """,
                (signal, earliest, latest),
            ).fetchall()
        actual_by_minute = {int(row[0]): float(row[1]) for row in rollups}
        actual_by_minute.update({int(row[0]): float(row[1]) for row in raw})
        result = []
        for row in predictions:
            target = dt_from_iso(row[3])
            minute = int(target.timestamp()) // 60
            candidates = [
                (abs(offset), actual_by_minute[minute + offset])
                for offset in range(-15, 16)
                if minute + offset in actual_by_minute
            ]
            if not candidates:
                continue
            _, actual = min(candidates, key=lambda item: item[0])
            result.append(
                {
                    "model": row[0],
                    "model_version": row[1],
                    "signal": signal,
                    "scenario": scenario,
                    "issued_at": row[2],
                    "prediction_at": row[3],
                    "horizon_hours": round(
                        (target - dt_from_iso(row[2])).total_seconds() / 3600,
                        4,
                    ),
                    "predicted_value": float(row[4]),
                    "actual_value": actual,
                    "error": actual - float(row[4]),
                    "unit": row[5],
                    "lower_value": row[6],
                    "upper_value": row[7],
                    "interval_contains_actual": (
                        bool(row[6] <= actual <= row[7])
                        if row[6] is not None and row[7] is not None
                        else None
                    ),
                    "features": json.loads(row[8]),
                    "metadata": json.loads(row[9]),
                }
            )
        return result

    def training_coverage(self) -> dict[str, Any]:
        """Describe retained model inputs without exposing their values."""

        with sqlite3.connect(self.path) as connection:
            rollups = connection.execute(
                """
                SELECT name, COUNT(*), MIN(period_start), MAX(period_start)
                FROM measurement_rollups
                WHERE period_seconds = 900
                GROUP BY name ORDER BY name
                """
            ).fetchall()
            predictions = connection.execute(
                """
                SELECT signal, scenario, model, model_version, COUNT(*),
                       MIN(issued_at), MAX(issued_at),
                       MIN(prediction_at), MAX(prediction_at)
                FROM prediction_points
                GROUP BY signal, scenario, model, model_version
                ORDER BY signal, scenario
                """
            ).fetchall()
            contexts = connection.execute(
                """
                SELECT provider, COUNT(*), MIN(issued_at), MAX(issued_at),
                       MIN(forecast_at), MAX(forecast_at)
                FROM forecast_context_points
                GROUP BY provider ORDER BY provider
                """
            ).fetchall()
            forecasts = connection.execute(
                """
                SELECT provider, COUNT(*), MIN(issued_at), MAX(issued_at),
                       MIN(forecast_at), MAX(forecast_at)
                FROM forecast_points
                GROUP BY provider ORDER BY provider
                """
            ).fetchall()
        return {
            "quarter_hour_measurements": [
                {
                    "signal": row[0],
                    "points": int(row[1]),
                    "first": row[2],
                    "last": row[3],
                }
                for row in rollups
            ],
            "prediction_vintages": [
                {
                    "signal": row[0],
                    "scenario": row[1],
                    "model": row[2],
                    "model_version": row[3],
                    "points": int(row[4]),
                    "first_issued": row[5],
                    "last_issued": row[6],
                    "first_target": row[7],
                    "last_target": row[8],
                }
                for row in predictions
            ],
            "forecast_context_vintages": [
                {
                    "provider": row[0],
                    "points": int(row[1]),
                    "first_issued": row[2],
                    "last_issued": row[3],
                    "first_target": row[4],
                    "last_target": row[5],
                }
                for row in contexts
            ],
            "pv_forecast_vintages": [
                {
                    "provider": row[0],
                    "points": int(row[1]),
                    "first_issued": row[2],
                    "last_issued": row[3],
                    "first_target": row[4],
                    "last_target": row[5],
                }
                for row in forecasts
            ],
            "location_included": False,
        }

    def storage_status(self, *, raw_retention_days: int = 14) -> dict[str, Any]:
        """Report physical size and a bounded, explicitly qualified projection."""

        with self._storage_cache_lock:
            if (
                self._storage_cache is not None
                and time.monotonic() - self._storage_cache_at < 60
            ):
                return self._storage_cache

        database = Path(self.path)

        def file_size(path: Path) -> int:
            try:
                return path.stat().st_size
            except FileNotFoundError:
                return 0

        database_bytes = file_size(database)
        wal_bytes = file_size(Path(f"{self.path}-wal"))
        shm_bytes = file_size(Path(f"{self.path}-shm"))
        total_bytes = database_bytes + wal_bytes + shm_bytes
        now = dt.datetime.now(dt.timezone.utc)
        with sqlite3.connect(self.path) as connection:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(
                connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            observations = connection.execute(
                """
                SELECT observed_at, used_bytes
                FROM storage_observations
                WHERE julianday(observed_at) >= julianday('now', '-30 days')
                ORDER BY observed_at
                """
            ).fetchall()
            earliest = connection.execute(
                """
                SELECT MIN(timestamp) FROM (
                    SELECT MIN(observed_at) AS timestamp FROM measurements
                    UNION ALL SELECT MIN(period_start) FROM measurement_rollups
                    UNION ALL SELECT MIN(issued_at) FROM forecast_points
                    UNION ALL SELECT MIN(issued_at) FROM forecast_context_points
                    UNION ALL SELECT MIN(issued_at) FROM prediction_points
                    UNION ALL SELECT MIN(period_start) FROM financial_intervals
                    UNION ALL SELECT MIN(generated_at) FROM plan_history
                )
                """
            ).fetchone()[0]

        allocated_bytes = page_count * page_size
        used_bytes = max(0, page_count - free_pages) * page_size
        daily_growth: float | None = None
        projection_method = "insufficient_history"
        projection_confidence = "unavailable"
        observation_span_hours = 0.0
        data_age_days: float | None = None
        if len(observations) >= 2:
            first_time = dt_from_iso(observations[0][0])
            last_time = dt_from_iso(observations[-1][0])
            observation_span_hours = max(
                0.0,
                (last_time - first_time).total_seconds() / 3600.0,
            )
            if observation_span_hours >= 6:
                change = float(observations[-1][1]) - float(observations[0][1])
                daily_growth = max(0.0, change / observation_span_hours * 24.0)
                projection_method = "observed_logical_growth"
                projection_confidence = (
                    "established" if observation_span_hours >= 168 else "preliminary"
                )
        if daily_growth is None and earliest:
            data_age_days = max(
                1.0,
                (now - dt_from_iso(earliest)).total_seconds() / 86400.0,
            )
            # Remove a small fixed-schema allowance so a new empty database
            # does not dominate the early rate. This estimate is replaced by
            # observed logical growth after six hours of maintenance samples.
            daily_growth = max(0.0, used_bytes - 65536) / data_age_days
            projection_method = "database_age_estimate"
            projection_confidence = "rough"
        elif earliest:
            data_age_days = max(
                0.0,
                (now - dt_from_iso(earliest)).total_seconds() / 86400.0,
            )

        def linear_projected(days: int) -> int | None:
            return (
                round(total_bytes + daily_growth * days)
                if daily_growth is not None
                else None
            )

        # During the first raw-retention window, almost all observed growth is
        # high-rate telemetry that will begin recycling SQLite pages once raw
        # pruning starts. Keep a visible 1% allowance for long-lived rollups,
        # forecasts, predictions and financial records until post-retention
        # observations can measure their actual net rate.
        compact_growth_fraction = 0.01
        raw_growth_days_remaining = (
            max(0.0, raw_retention_days - data_age_days)
            if data_age_days is not None
            else None
        )

        def retention_projected(days: int) -> int | None:
            if daily_growth is None:
                return None
            if raw_growth_days_remaining is None or data_age_days is None:
                growth_days = float(days)
            elif data_age_days < raw_retention_days:
                growth_days = min(float(days), raw_growth_days_remaining)
                growth_days += days * compact_growth_fraction
            else:
                growth_days = float(days)
            return round(total_bytes + daily_growth * growth_days)

        result = {
            "measured_at": now.isoformat(),
            "current": {
                "database_bytes": database_bytes,
                "wal_bytes": wal_bytes,
                "shm_bytes": shm_bytes,
                "total_bytes": total_bytes,
                "allocated_database_bytes": allocated_bytes,
                "used_database_bytes": used_bytes,
            },
            "growth": {
                "bytes_per_day": (
                    round(daily_growth) if daily_growth is not None else None
                ),
                "projected_total_bytes_30_days": retention_projected(30),
                "projected_total_bytes_365_days": retention_projected(365),
                "linear_total_bytes_30_days": linear_projected(30),
                "linear_total_bytes_365_days": linear_projected(365),
                "method": projection_method,
                "confidence": projection_confidence,
                "observation_span_hours": round(observation_span_hours, 2),
                "data_age_days": (
                    round(data_age_days, 2) if data_age_days is not None else None
                ),
                "raw_retention_days": raw_retention_days,
                "raw_growth_days_remaining": (
                    round(raw_growth_days_remaining, 2)
                    if raw_growth_days_remaining is not None
                    else None
                ),
                "early_compact_growth_fraction": compact_growth_fraction,
                "note": (
                    "Retention-aware early projection: high-rate raw telemetry is "
                    "capped at its configured retention window, with a conservative "
                    "allowance for longer-lived compact data. It is replaced by "
                    "observed net growth after retention pruning matures. WAL and "
                    "SHM sizes are transient."
                ),
            },
        }
        with self._storage_cache_lock:
            self._storage_cache = result
            self._storage_cache_at = time.monotonic()
        return result

    def prediction_quality(
        self,
        *,
        signal: str,
        scenario: str,
        model: str | None = None,
        model_version: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100000,
    ) -> dict[str, Any]:
        scoreable = scenario in ("expected", "native_no_change")
        if not scoreable:
            return {
                "signal": signal,
                "scenario": scenario,
                "scoreable": False,
                "reason": (
                    "counterfactual shadow predictions cannot be compared "
                    "with actual operation until that policy is executed"
                ),
                "samples": 0,
                "days": 0,
                "by_horizon": {},
            }
        samples = self.prediction_samples(
            signal=signal,
            scenario=scenario,
            model=model,
            model_version=model_version,
            since=since,
            until=until,
            limit=limit,
        )
        buckets = {
            "0_to_2_hours": [],
            "2_to_8_hours": [],
            "8_to_24_hours": [],
            "24_plus_hours": [],
        }
        for sample in samples:
            horizon = sample["horizon_hours"]
            key = (
                "0_to_2_hours"
                if horizon <= 2
                else "2_to_8_hours"
                if horizon <= 8
                else "8_to_24_hours"
                if horizon <= 24
                else "24_plus_hours"
            )
            buckets[key].append(sample)

        def metrics(values: list[dict[str, Any]]) -> dict[str, Any]:
            errors = [float(value["error"]) for value in values]
            actuals = [abs(float(value["actual_value"])) for value in values]
            interval_values = [
                value["interval_contains_actual"]
                for value in values
                if value["interval_contains_actual"] is not None
            ]
            return {
                "samples": len(values),
                "mae": (
                    round(statistics.fmean(abs(error) for error in errors), 5)
                    if errors
                    else None
                ),
                "rmse": (
                    round(
                        math.sqrt(statistics.fmean(error * error for error in errors)),
                        5,
                    )
                    if errors
                    else None
                ),
                "bias": (
                    round(statistics.fmean(errors), 5) if errors else None
                ),
                "weighted_absolute_percentage_error": (
                    round(sum(abs(error) for error in errors) / sum(actuals), 6)
                    if errors and sum(actuals) > 0
                    else None
                ),
                "prediction_interval_coverage": (
                    round(
                        sum(bool(value) for value in interval_values)
                        / len(interval_values),
                        6,
                    )
                    if interval_values
                    else None
                ),
            }

        days = {
            dt_from_iso(sample["prediction_at"]).date() for sample in samples
        }
        by_horizon = {
            name: metrics(values) for name, values in buckets.items()
        }
        required = (
            by_horizon["0_to_2_hours"],
            by_horizon["2_to_8_hours"],
            by_horizon["8_to_24_hours"],
        )
        dataset_ready = len(days) >= 28 and all(
            value["samples"] >= 300 for value in required
        )
        return {
            "signal": signal,
            "scenario": scenario,
            "model": model,
            "model_version": model_version,
            "scoreable": True,
            "unit": samples[0]["unit"] if samples else None,
            "samples": len(samples),
            "days": len(days),
            "dataset_ready": dataset_ready,
            "required_days": 28,
            "required_samples_per_horizon": 300,
            "overall": metrics(samples),
            "by_horizon": by_horizon,
            "note": (
                "dataset readiness indicates sample coverage only; model "
                "accuracy must be assessed separately before control"
            ),
        }

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
                        "horizon_hours": round(
                            (
                                target - dt_from_iso(issued_at)
                            ).total_seconds()
                            / 3600,
                            4,
                        ),
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

    def grid_minute_buckets(
        self,
        *,
        since: str,
        until: str,
    ) -> list[dict[str, Any]]:
        """Return complete UTC-minute averages for financial accounting."""

        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT
                    CAST(strftime('%s', observed_at) AS INTEGER) / 60 AS bucket,
                    AVG(value_num), COUNT(*)
                FROM measurements
                WHERE name = 'grid.active_power'
                  AND value_num IS NOT NULL
                  AND observed_at >= ?
                  AND observed_at < ?
                GROUP BY bucket
                ORDER BY bucket
                """,
                (since, until),
            ).fetchall()
        return [
            {
                "period_start": dt.datetime.fromtimestamp(
                    int(row[0]) * 60,
                    tz=dt.timezone.utc,
                ).isoformat(),
                "average_grid_power_w": float(row[1]),
                "samples": int(row[2]),
            }
            for row in rows
        ]

    def forecast_samples(
        self,
        *,
        provider: str,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        """Return issued forecasts matched to actuals, preserving lead time."""

        clauses = ["provider = ?"]
        values: list[Any] = [provider]
        if since:
            clauses.append("forecast_at >= ?")
            values.append(since)
        if until:
            clauses.append("forecast_at <= ?")
            values.append(until)
        values.append(max(1, min(limit, 100000)))
        with sqlite3.connect(self.path) as connection:
            forecasts = connection.execute(
                f"""
                SELECT issued_at, forecast_at, power_w
                FROM forecast_points
                WHERE {' AND '.join(clauses)}
                ORDER BY forecast_at DESC, issued_at DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            if not forecasts:
                return []
            earliest = min(row[1] for row in forecasts)
            latest = max(row[1] for row in forecasts)
            actuals = connection.execute(
                """
                SELECT
                    CAST(strftime('%s', observed_at) AS INTEGER) / 60 AS bucket,
                    AVG(value_num)
                FROM measurements
                WHERE name = 'external_pv.active_power'
                  AND value_num IS NOT NULL
                  AND julianday(observed_at) >= julianday(?, '-15 minutes')
                  AND julianday(observed_at) <= julianday(?, '+15 minutes')
                GROUP BY bucket
                """,
                (earliest, latest),
            ).fetchall()
        actual_by_minute = {int(row[0]): float(row[1]) for row in actuals}
        result = []
        for issued_at, forecast_at, power_w in forecasts:
            target = dt_from_iso(forecast_at)
            minute = int(target.timestamp()) // 60
            candidates = [
                (abs(offset), actual_by_minute.get(minute + offset))
                for offset in range(-15, 16)
                if minute + offset in actual_by_minute
            ]
            if not candidates:
                continue
            _, actual = min(candidates, key=lambda item: item[0])
            result.append(
                {
                    "issued_at": issued_at,
                    "forecast_at": forecast_at,
                    "horizon_hours": round(
                        (target - dt_from_iso(issued_at)).total_seconds()
                        / 3600,
                        4,
                    ),
                    "forecast_power_w": float(power_w),
                    "actual_power_w": float(actual),
                    "error_w": float(actual) - float(power_w),
                }
            )
        return result

    def latest_financial_period(self) -> str | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT MAX(period_start) FROM financial_intervals"
            ).fetchone()
        return row[0] if row and row[0] else None

    def financial_day_state(self, local_date: str) -> dict[str, Any]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT local_hour, imported_kwh, exported_kwh, export_period
                FROM financial_intervals
                WHERE local_date = ?
                """,
                (local_date,),
            ).fetchall()
            adjustments = {
                row[0]: float(row[1])
                for row in connection.execute(
                    """
                    SELECT kind, amount FROM financial_adjustments
                    WHERE local_date = ?
                    """,
                    (local_date,),
                )
            }
        zerohero_import = {hour: 0.0 for hour in (18, 19, 20)}
        zerohero_minutes = {hour: 0 for hour in (18, 19, 20)}
        super_export_kwh = 0.0
        for hour, imported, exported, export_period in rows:
            if hour in zerohero_import:
                zerohero_import[hour] += float(imported)
                zerohero_minutes[hour] += 1
            if export_period == "super_export":
                super_export_kwh += float(exported)
        return {
            "intervals": len(rows),
            "super_export_kwh": super_export_kwh,
            "zerohero_import_kwh": zerohero_import,
            "zerohero_minutes": zerohero_minutes,
            "adjustments": adjustments,
        }

    def record_financial_intervals(
        self,
        intervals: list[dict[str, Any]],
    ) -> None:
        if not intervals:
            return
        with sqlite3.connect(self.path, timeout=30.0) as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO financial_intervals (
                    period_start, local_date, local_hour,
                    average_grid_power_w, imported_kwh, exported_kwh,
                    import_price_per_kwh, export_price_per_kwh,
                    import_cost, export_credit, net_energy_cost, samples,
                    import_period, export_period, plan_id
                ) VALUES (
                    :period_start, :local_date, :local_hour,
                    :average_grid_power_w, :imported_kwh, :exported_kwh,
                    :import_price_per_kwh, :export_price_per_kwh,
                    :import_cost, :export_credit, :net_energy_cost, :samples,
                    :import_period, :export_period, :plan_id
                )
                """,
                intervals,
            )
            connection.commit()

    def record_financial_adjustment(
        self,
        *,
        local_date: str,
        kind: str,
        occurred_at: str,
        amount: float,
        description: str,
        plan_id: str,
    ) -> None:
        with sqlite3.connect(self.path, timeout=30.0) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO financial_adjustments (
                    local_date, kind, occurred_at, amount, description, plan_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    local_date,
                    kind,
                    occurred_at,
                    amount,
                    description,
                    plan_id,
                ),
            )
            connection.commit()

    def financial_history(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        bucket_seconds: int = 3600,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if not 60 <= bucket_seconds <= 2678400:
            raise ValueError(
                "financial bucket_seconds must be between 60 and 2678400"
            )
        clauses = ["1 = 1"]
        values: list[Any] = [bucket_seconds]
        adjustment_clauses = ["1 = 1"]
        adjustment_values: list[Any] = [bucket_seconds]
        if since:
            clauses.append("period_start >= ?")
            values.append(since)
            adjustment_clauses.append("occurred_at >= ?")
            adjustment_values.append(since)
        if until:
            clauses.append("period_start <= ?")
            values.append(until)
            adjustment_clauses.append("occurred_at <= ?")
            adjustment_values.append(until)
        bounded_limit = max(1, min(limit, 10000))
        values.append(bounded_limit)
        adjustment_values.append(bounded_limit)
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    CAST(strftime('%s', period_start) AS INTEGER) / ? AS bucket,
                    SUM(imported_kwh), SUM(exported_kwh),
                    SUM(import_cost), SUM(export_credit),
                    SUM(net_energy_cost), SUM(samples),
                    COUNT(*),
                    AVG(import_price_per_kwh), AVG(export_price_per_kwh)
                FROM financial_intervals
                WHERE {' AND '.join(clauses)}
                GROUP BY bucket
                ORDER BY bucket DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            adjustment_rows = connection.execute(
                f"""
                SELECT
                    CAST(strftime('%s', occurred_at) AS INTEGER) / ? AS bucket,
                    SUM(amount)
                FROM financial_adjustments
                WHERE {' AND '.join(adjustment_clauses)}
                GROUP BY bucket
                ORDER BY bucket DESC
                LIMIT ?
                """,
                adjustment_values,
            ).fetchall()
        adjustments = {int(row[0]): float(row[1]) for row in adjustment_rows}
        result = []
        seen: set[int] = set()
        for row in rows:
            bucket = int(row[0])
            seen.add(bucket)
            adjustment = adjustments.get(bucket, 0.0)
            result.append(
                {
                    "period_start": dt.datetime.fromtimestamp(
                        bucket * bucket_seconds,
                        tz=dt.timezone.utc,
                    ).isoformat(),
                    "imported_kwh": float(row[1] or 0),
                    "exported_kwh": float(row[2] or 0),
                    "import_cost": float(row[3] or 0),
                    "export_credit": float(row[4] or 0),
                    "energy_net_cost": float(row[5] or 0),
                    "adjustments": adjustment,
                    "net_cost": float(row[5] or 0) + adjustment,
                    "samples": int(row[6] or 0),
                    "intervals": int(row[7] or 0),
                    "average_import_price_per_kwh": float(row[8] or 0),
                    "average_export_price_per_kwh": float(row[9] or 0),
                }
            )
        for bucket, adjustment in adjustments.items():
            if bucket not in seen:
                result.append(
                    {
                        "period_start": dt.datetime.fromtimestamp(
                            bucket * bucket_seconds,
                            tz=dt.timezone.utc,
                        ).isoformat(),
                        "imported_kwh": 0.0,
                        "exported_kwh": 0.0,
                        "import_cost": 0.0,
                        "export_credit": 0.0,
                        "energy_net_cost": 0.0,
                        "adjustments": adjustment,
                        "net_cost": adjustment,
                        "samples": 0,
                        "intervals": 0,
                        "average_import_price_per_kwh": 0.0,
                        "average_export_price_per_kwh": 0.0,
                    }
                )
        return sorted(
            result,
            key=lambda item: item["period_start"],
            reverse=True,
        )[:bounded_limit]

    def financial_summary(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        history = self.financial_history(
            since=since,
            until=until,
            bucket_seconds=2678400,
            limit=10000,
        )
        keys = (
            "imported_kwh",
            "exported_kwh",
            "import_cost",
            "export_credit",
            "energy_net_cost",
            "adjustments",
            "net_cost",
        )
        result = {
            key: round(sum(float(row[key]) for row in history), 6)
            for key in keys
        }
        result.update(
            {
                "since": since,
                "until": until,
                "intervals": sum(int(row["intervals"]) for row in history),
                "cost_payable": round(max(0.0, result["net_cost"]), 6),
                "net_profit": round(max(0.0, -result["net_cost"]), 6),
                "gross_export_revenue": result["export_credit"],
                "realized_daemon_savings": None,
                "model": (
                    "minute-average authoritative grid accounting; includes "
                    "daily supply and earned ZEROHERO credit; realized daemon "
                    "savings unavailable while control is shadow-only"
                ),
            }
        )
        return result

    def record_plan(self, plan: dict[str, Any]) -> None:
        generated_at = plan.get("generated_at")
        if not generated_at:
            return
        simulation = plan.get("simulation") or {}
        baseline = simulation.get("baseline") or {}
        optimized = simulation.get("optimized") or {}
        with sqlite3.connect(self.path, timeout=30.0) as connection:
            latest = connection.execute(
                """
                SELECT generated_at, status, reason, plan_json
                FROM plan_history ORDER BY generated_at DESC LIMIT 1
                """
            ).fetchone()
            current_action = next(
                (
                    item.get("action")
                    for item in plan.get("recommendations") or []
                    if item.get("action")
                ),
                None,
            )
            if latest:
                previous = json.loads(latest[3])
                previous_action = next(
                    (
                        item.get("action")
                        for item in previous.get("recommendations") or []
                        if item.get("action")
                    ),
                    None,
                )
                unchanged = (
                    latest[1] == str(plan.get("status", "unknown"))
                    and latest[2] == plan.get("reason")
                    and previous_action == current_action
                )
                if unchanged and (
                    dt_from_iso(generated_at) - dt_from_iso(latest[0])
                ).total_seconds() < 3 * 3600:
                    return
            connection.execute(
                """
                INSERT OR REPLACE INTO plan_history (
                    generated_at, status, mode, reason,
                    recommendation_count, estimated_cost_improvement,
                    baseline_cost, optimized_cost, plan_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    str(plan.get("status", "unknown")),
                    str(plan.get("mode", "shadow")),
                    plan.get("reason"),
                    len(plan.get("recommendations") or []),
                    simulation.get("estimated_cost_improvement"),
                    baseline.get("cost"),
                    optimized.get("cost"),
                    json.dumps(plan, separators=(",", ":")),
                ),
            )
            connection.commit()

    def plans(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
        include_plan: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["1 = 1"]
        values: list[Any] = []
        if since:
            clauses.append("generated_at >= ?")
            values.append(since)
        if until:
            clauses.append("generated_at <= ?")
            values.append(until)
        values.append(max(1, min(limit, 1000)))
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                f"""
                SELECT generated_at, status, mode, reason,
                       recommendation_count, estimated_cost_improvement,
                       baseline_cost, optimized_cost, plan_json
                FROM plan_history
                WHERE {' AND '.join(clauses)}
                ORDER BY generated_at DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [
            {
                "generated_at": row[0],
                "status": row[1],
                "mode": row[2],
                "reason": row[3],
                "recommendation_count": row[4],
                "estimated_cost_improvement": row[5],
                "baseline_cost": row[6],
                "optimized_cost": row[7],
                **({"plan": json.loads(row[8])} if include_plan else {}),
            }
            for row in rows
        ]


class StorageMaintainer:
    """Build rollups before applying bounded retention."""

    def __init__(self, path: str, config) -> None:
        self.path = path
        self.config = config
        self.runs = 0
        self.failures = 0

    def run_once(self) -> None:
        with sqlite3.connect(self.path) as connection:
            now = dt.datetime.now(dt.timezone.utc)
            for seconds, pattern, retention_days, selected_names in (
                (
                    900,
                    "%Y-%m-%dT%H:%M:00+00:00",
                    self.config.raw_retention_days,
                    MODEL_TRAINING_SIGNALS,
                ),
                (
                    3600,
                    "%Y-%m-%dT%H:00:00+00:00",
                    self.config.raw_retention_days,
                    None,
                ),
                (
                    86400,
                    "%Y-%m-%dT00:00:00+00:00",
                    self.config.raw_retention_days,
                    None,
                ),
            ):
                latest = connection.execute(
                    """
                    SELECT MAX(period_start) FROM measurement_rollups
                    WHERE period_seconds = ?
                    """,
                    (seconds,),
                ).fetchone()[0]
                cutoff_epoch = int(now.timestamp()) // seconds * seconds
                cutoff = dt.datetime.fromtimestamp(
                    cutoff_epoch,
                    tz=dt.timezone.utc,
                )
                since = (
                    dt_from_iso(latest) - dt.timedelta(seconds=seconds)
                    if latest
                    else cutoff - dt.timedelta(days=retention_days)
                )
                selected_clause = ""
                physical_training_clause = ""
                if selected_names:
                    selected_clause = " AND name IN ({})".format(
                        ",".join("?" for _ in selected_names)
                    )
                    physical_training_clause = (
                        " AND (name NOT IN ('site.load_power', "
                        "'external_pv.active_power', "
                        "'site.pv_generation_power') OR value_num >= 0)"
                    )
                bucket = (
                    "strftime(?, (CAST(strftime('%s', observed_at) AS "
                    f"INTEGER) / {seconds}) * {seconds}, 'unixepoch')"
                )
                rows = connection.execute(
                    f"""
                    WITH source AS (
                        SELECT id, {bucket} AS period_start, name, value_num,
                               unit, quality, source, authority
                        FROM measurements
                        WHERE value_num IS NOT NULL
                          AND observed_at >= ?
                          AND observed_at < ?
                          {selected_clause}
                          {physical_training_clause}
                    ), grouped AS (
                        SELECT period_start, name, COUNT(*) AS samples,
                               AVG(value_num) AS value_avg,
                               MIN(value_num) AS value_min,
                               MAX(value_num) AS value_max,
                               MAX(id) AS last_id,
                               MAX(unit) AS unit,
                               CASE WHEN MIN(quality) = 'good' AND
                                         MAX(quality) = 'good'
                                    THEN 'good' ELSE 'mixed' END AS quality,
                               MAX(source) AS source,
                               MAX(authority) AS authority
                        FROM source GROUP BY period_start, name
                    )
                    SELECT grouped.period_start, ?, grouped.name,
                           grouped.samples, grouped.value_avg,
                           grouped.value_min, grouped.value_max,
                           source.value_num, grouped.unit, grouped.quality,
                           grouped.source, grouped.authority
                    FROM grouped JOIN source ON source.id = grouped.last_id
                    """,
                    (
                        pattern,
                        since.isoformat(),
                        cutoff.isoformat(),
                        *(selected_names or ()),
                        seconds,
                    ),
                ).fetchall()
                for start in range(0, len(rows), 200):
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO measurement_rollups (
                            period_start, period_seconds, name, samples,
                            value_avg, value_min, value_max, value_last,
                            unit, quality, source, authority
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows[start : start + 200],
                    )
                    connection.commit()
                    time.sleep(0.01)
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
                WHERE period_seconds = 900
                  AND julianday(period_start) < julianday('now', ?)
                """,
                (
                    f"-{self.config.quarter_hour_retention_days} days",
                ),
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
            connection.execute(
                """
                DELETE FROM prediction_points
                WHERE julianday(issued_at) < julianday('now', ?)
                """,
                (f"-{self.config.prediction_retention_days} days",),
            )
            connection.execute(
                """
                DELETE FROM plan_history
                WHERE julianday(generated_at) < julianday('now', ?)
                """,
                (f"-{self.config.plan_retention_days} days",),
            )
            connection.execute(
                """
                DELETE FROM forecast_points
                WHERE julianday(issued_at) < julianday('now', ?)
                """,
                (f"-{self.config.forecast_retention_days} days",),
            )
            connection.execute(
                """
                DELETE FROM forecast_context_points
                WHERE julianday(issued_at) < julianday('now', ?)
                """,
                (
                    f"-{self.config.forecast_context_retention_days} days",
                ),
            )
            connection.commit()
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(
                connection.execute("PRAGMA freelist_count").fetchone()[0]
            )

            def file_size(path: Path) -> int:
                try:
                    return path.stat().st_size
                except FileNotFoundError:
                    return 0

            database = Path(self.path)
            database_bytes = file_size(database)
            wal_bytes = file_size(Path(f"{self.path}-wal"))
            shm_bytes = file_size(Path(f"{self.path}-shm"))
            connection.execute(
                """
                INSERT OR REPLACE INTO storage_observations (
                    observed_at, database_bytes, wal_bytes, shm_bytes,
                    total_bytes, allocated_bytes, used_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(),
                    database_bytes,
                    wal_bytes,
                    shm_bytes,
                    database_bytes + wal_bytes + shm_bytes,
                    page_count * page_size,
                    max(0, page_count - free_pages) * page_size,
                ),
            )
            connection.execute(
                """
                DELETE FROM storage_observations
                WHERE julianday(observed_at) < julianday('now', '-90 days')
                """
            )
            connection.commit()
        self.runs += 1

    def run(self, stop: threading.Event) -> None:
        # Forecast, accounting and telemetry workers all populate their first
        # state at startup. Avoid contending with that bounded initialization.
        if stop.wait(min(60.0, self.config.maintenance_interval_seconds)):
            return
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except sqlite3.Error:
                self.failures += 1
            elapsed = time.monotonic() - started
            stop.wait(max(1.0, self.config.maintenance_interval_seconds - elapsed))
