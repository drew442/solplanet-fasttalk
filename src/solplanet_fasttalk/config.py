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
class DaemonConfig:
    database: str
    api: APIConfig
    eastron: EastronConfig
    asw: ASWConfig


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
    unknown_root = set(data) - {"daemon", "api", "eastron", "asw"}
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
    config = DaemonConfig(
        database=database,
        api=_construct(APIConfig, _table(data, "api")),
        eastron=_construct(EastronConfig, _table(data, "eastron")),
        asw=_construct(ASWConfig, _table(data, "asw")),
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
