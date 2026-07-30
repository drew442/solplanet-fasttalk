"""TOML configuration and validation."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class APIConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class EastronConfig:
    enabled: bool = True
    device: str = ""
    baud: int = 9600
    grid_slave: int = 1
    external_pv_slave: int = 2
    grid_power_multiplier: float = 1.0
    external_pv_power_multiplier: float = 1.0


@dataclass(frozen=True)
class ASWConfig:
    enabled: bool = True
    device: str = ""
    baud: int = 9600
    slave: int = 3
    timeout_seconds: float = 1.5
    active_power_multiplier: float = 1.0


@dataclass(frozen=True)
class StorageConfig:
    raw_retention_days: int = 14
    hourly_retention_days: int = 400
    daily_retention_days: int = 3650
    maintenance_interval_seconds: int = 3600


@dataclass(frozen=True)
class SolisConfig:
    enabled: bool = False
    device: str = ""
    baud: int = 9600
    slave: int = 1
    timeout_seconds: float = 1.5
    poll_interval_seconds: float = 5.0
    mismatch_tolerance_watts: float = 750.0
    mismatch_duration_seconds: float = 60.0


@dataclass(frozen=True)
class TariffConfig:
    enabled: bool = True
    plan: str = "globird-zerohero-vpp-ausgrid-pre-2026-07"
    timezone: str = "Australia/Sydney"
    exceptional_events_file: str = ""


@dataclass(frozen=True)
class ForecastPlane:
    name: str
    declination: float
    azimuth: float
    peak_power_kw: float


@dataclass(frozen=True)
class ForecastSolarConfig:
    enabled: bool = False
    api_key_file: str = ""
    location_file: str = ""
    cache_file: str = ""
    refresh_interval_seconds: int = 3600
    retry_interval_seconds: int = 300
    request_timeout_seconds: float = 15.0
    max_cache_age_seconds: int = 21600
    planes: tuple[ForecastPlane, ...] = ()


@dataclass(frozen=True)
class OptimisationConfig:
    enabled: bool = False
    interval_seconds: int = 300
    horizon_hours: int = 36
    step_minutes: int = 15
    battery_capacity_kwh: float = 53.76
    reserve_soc_percent: float = 15.0
    maximum_soc_percent: float = 95.0
    max_charge_watts: float = 12000.0
    max_discharge_watts: float = 12000.0
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    site_import_limit_watts: float = 30000.0
    site_export_limit_watts: float = 30000.0
    minimum_arbitrage_margin_per_kwh: float = 0.03


@dataclass(frozen=True)
class DaemonConfig:
    database: str
    api: APIConfig
    eastron: EastronConfig
    asw: ASWConfig
    storage: StorageConfig = StorageConfig()
    solis: SolisConfig = SolisConfig()
    tariff: TariffConfig = TariffConfig()
    forecast_solar: ForecastSolarConfig = ForecastSolarConfig()
    optimisation: OptimisationConfig = OptimisationConfig()


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a TOML table")
    return value


def _construct(cls: type, values: dict[str, Any]):
    allowed = set(cls.__dataclass_fields__)
    unknown = set(values) - allowed
    if unknown:
        raise ConfigError(
            f"unknown {cls.__name__} option(s): {', '.join(sorted(unknown))}"
        )
    try:
        return cls(**values)
    except TypeError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: str | os.PathLike[str]) -> DaemonConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    daemon = _table(data, "daemon")
    unknown_root = set(data) - {
        "daemon",
        "api",
        "eastron",
        "asw",
        "storage",
        "solis",
        "tariff",
        "forecast_solar",
        "optimisation",
    }
    if unknown_root:
        raise ConfigError(f"unknown table(s): {', '.join(sorted(unknown_root))}")
    unknown_daemon = set(daemon) - {"database"}
    if unknown_daemon:
        raise ConfigError(
            f"unknown [daemon] option(s): {', '.join(sorted(unknown_daemon))}"
        )
    database = daemon.get("database", "solplanet-fasttalk.sqlite3")
    if not isinstance(database, str) or not database:
        raise ConfigError("daemon.database must be a non-empty path")
    forecast_values = dict(_table(data, "forecast_solar"))
    raw_planes = forecast_values.pop("planes", [])
    if not isinstance(raw_planes, list):
        raise ConfigError("forecast_solar.planes must be an array of tables")
    planes: list[ForecastPlane] = []
    for raw_plane in raw_planes:
        if not isinstance(raw_plane, dict):
            raise ConfigError("each forecast_solar plane must be a TOML table")
        planes.append(_construct(ForecastPlane, raw_plane))
    forecast_values["planes"] = tuple(planes)
    config = DaemonConfig(
        database=database,
        api=_construct(APIConfig, _table(data, "api")),
        eastron=_construct(EastronConfig, _table(data, "eastron")),
        asw=_construct(ASWConfig, _table(data, "asw")),
        storage=_construct(StorageConfig, _table(data, "storage")),
        solis=_construct(SolisConfig, _table(data, "solis")),
        tariff=_construct(TariffConfig, _table(data, "tariff")),
        forecast_solar=_construct(ForecastSolarConfig, forecast_values),
        optimisation=_construct(
            OptimisationConfig, _table(data, "optimisation")
        ),
    )
    validate_config(config)
    return config


def validate_config(config: DaemonConfig) -> None:
    if not 1 <= config.api.port <= 65535:
        raise ConfigError("api.port must be between 1 and 65535")
    if config.api.host not in ("127.0.0.1", "localhost"):
        raise ConfigError(
            "the unauthenticated milestone API must bind to loopback"
        )
    devices: list[tuple[str, str]] = []
    for name, enabled, device, baud, slave in (
        (
            "eastron",
            config.eastron.enabled,
            config.eastron.device,
            config.eastron.baud,
            config.eastron.grid_slave,
        ),
        ("asw", config.asw.enabled, config.asw.device, config.asw.baud, config.asw.slave),
        (
            "solis",
            config.solis.enabled,
            config.solis.device,
            config.solis.baud,
            config.solis.slave,
        ),
    ):
        if enabled and not device:
            raise ConfigError(f"{name}.device is required when enabled")
        if baud != 9600:
            raise ConfigError(f"{name}.baud must be 9600 for the confirmed plant")
        if not 1 <= slave <= 247:
            raise ConfigError(f"{name} slave must be between 1 and 247")
        if enabled:
            devices.append((name, os.path.realpath(device)))
    if config.eastron.grid_slave == config.eastron.external_pv_slave:
        raise ConfigError("Eastron grid and external-PV slaves must differ")
    if not 1 <= config.eastron.external_pv_slave <= 247:
        raise ConfigError("Eastron external-PV slave must be between 1 and 247")
    for value, name in (
        (config.eastron.grid_power_multiplier, "grid_power_multiplier"),
        (
            config.eastron.external_pv_power_multiplier,
            "external_pv_power_multiplier",
        ),
        (config.asw.active_power_multiplier, "active_power_multiplier"),
    ):
        if value not in (-1, 1, -1.0, 1.0):
            raise ConfigError(f"{name} must be 1 or -1")
    resolved = [device for _, device in devices]
    if len(resolved) != len(set(resolved)):
        raise ConfigError("each enabled integration must use a different serial device")
    if config.asw.timeout_seconds <= 0:
        raise ConfigError("asw.timeout_seconds must be greater than zero")
    if config.solis.timeout_seconds <= 0 or config.solis.poll_interval_seconds <= 0:
        raise ConfigError("Solis timeouts and poll interval must be greater than zero")
    if config.solis.mismatch_tolerance_watts < 0:
        raise ConfigError("solis.mismatch_tolerance_watts cannot be negative")
    for value, name in (
        (config.storage.raw_retention_days, "raw_retention_days"),
        (config.storage.hourly_retention_days, "hourly_retention_days"),
        (config.storage.daily_retention_days, "daily_retention_days"),
        (
            config.storage.maintenance_interval_seconds,
            "maintenance_interval_seconds",
        ),
    ):
        if value <= 0:
            raise ConfigError(f"storage.{name} must be greater than zero")
    if config.tariff.plan != "globird-zerohero-vpp-ausgrid-pre-2026-07":
        raise ConfigError("unsupported tariff.plan")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(config.tariff.timezone)
    except Exception as exc:
        raise ConfigError(f"unknown tariff.timezone: {config.tariff.timezone}") from exc
    forecast = config.forecast_solar
    if forecast.enabled:
        for name, value in (
            ("api_key_file", forecast.api_key_file),
            ("location_file", forecast.location_file),
            ("cache_file", forecast.cache_file),
        ):
            if not value:
                raise ConfigError(
                    f"forecast_solar.{name} is required when enabled"
                )
        if not 1 <= len(forecast.planes) <= 2:
            raise ConfigError(
                "Forecast.Solar requires one or two configured array planes"
            )
    for plane in forecast.planes:
        if not 0 <= plane.declination <= 90:
            raise ConfigError("forecast plane declination must be between 0 and 90")
        if not -180 <= plane.azimuth <= 180:
            raise ConfigError("forecast plane azimuth must be between -180 and 180")
        if plane.peak_power_kw <= 0:
            raise ConfigError("forecast plane peak_power_kw must be positive")
    optimisation = config.optimisation
    if optimisation.enabled and not config.tariff.enabled:
        raise ConfigError("optimisation requires the tariff model")
    if optimisation.enabled and not forecast.enabled:
        raise ConfigError("optimisation requires Forecast.Solar")
    if optimisation.step_minutes not in (5, 15, 30, 60):
        raise ConfigError("optimisation.step_minutes must be 5, 15, 30, or 60")
    if not 0 <= optimisation.reserve_soc_percent < optimisation.maximum_soc_percent <= 100:
        raise ConfigError("optimisation SOC limits are invalid")
    for value, name in (
        (optimisation.battery_capacity_kwh, "battery_capacity_kwh"),
        (optimisation.max_charge_watts, "max_charge_watts"),
        (optimisation.max_discharge_watts, "max_discharge_watts"),
        (optimisation.site_import_limit_watts, "site_import_limit_watts"),
        (optimisation.site_export_limit_watts, "site_export_limit_watts"),
    ):
        if value <= 0:
            raise ConfigError(f"optimisation.{name} must be greater than zero")
    for value, name in (
        (optimisation.charge_efficiency, "charge_efficiency"),
        (optimisation.discharge_efficiency, "discharge_efficiency"),
    ):
        if not 0 < value <= 1:
            raise ConfigError(f"optimisation.{name} must be in (0, 1]")
