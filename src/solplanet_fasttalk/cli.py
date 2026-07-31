"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import datetime as dt
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import ConfigError, EastronConfig, load_config, validate_config
from .daemon import Daemon
from .eastron import EastronDecoder
from .model import PlantState
from .modbus import RTUStreamDecoder, TransactionMatcher
from .optimisation import ForecastSlot, simulate_plan
from .storage import HistoryReader
from .tariff import ZeroHeroTariff


def _runtime_config(args: argparse.Namespace):
    config = load_config(args.config)
    api_host = getattr(args, "api_host", None)
    api_port = getattr(args, "api_port", None)
    auth_token_file = getattr(args, "api_auth_token_file", None)
    if (
        api_host is not None
        or api_port is not None
        or auth_token_file is not None
    ):
        config = replace(
            config,
            api=replace(
                config.api,
                host=config.api.host if api_host is None else api_host,
                port=config.api.port if api_port is None else api_port,
                auth_token_file=(
                    config.api.auth_token_file
                    if auth_token_file is None
                    else auth_token_file
                ),
            ),
        )
        validate_config(config)
    return config


def command_check(args: argparse.Namespace) -> int:
    config = _runtime_config(args)
    print(
        json.dumps(
            {
                "status": "ok",
                "database": config.database,
                "api": {
                    "host": config.api.host,
                    "port": config.api.port,
                    "authenticated": bool(config.api.auth_token_file),
                },
                "eastron_enabled": config.eastron.enabled,
                "asw_enabled": config.asw.enabled,
                "solis_enabled": config.solis.enabled,
                "forecast_solar_enabled": config.forecast_solar.enabled,
                "optimisation_mode": (
                    "shadow" if config.optimisation.enabled else "disabled"
                ),
                "control_available": False,
            },
            indent=2,
        )
    )
    return 0


def command_replay_optimisation(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    history = HistoryReader(config.database)
    step_seconds = config.optimisation.step_minutes * 60

    def buckets(name: str) -> dict[int, float]:
        values: dict[int, list[float]] = defaultdict(list)
        for item in history.measurements(
            name,
            since=args.since,
            until=args.until,
            limit=10000,
        ):
            if isinstance(item["value"], (int, float)):
                timestamp = dt.datetime.fromisoformat(item["observed_at"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
                bucket = int(timestamp.timestamp()) // step_seconds
                values[bucket].append(float(item["value"]))
        return {
            bucket: sum(samples) / len(samples)
            for bucket, samples in values.items()
        }

    loads = buckets("site.load_power")
    pv = buckets("external_pv.active_power")
    common = sorted(set(loads) & set(pv))
    if not common:
        raise ValueError(
            "history contains no overlapping site.load_power and "
            "external_pv.active_power samples"
        )
    soc_history = history.measurements(
        "battery.soc", since=args.since, until=args.until, limit=10000
    )
    numeric_soc = [
        float(item["value"])
        for item in reversed(soc_history)
        if isinstance(item["value"], (int, float))
    ]
    if not numeric_soc:
        raise ValueError("history contains no battery.soc baseline")
    slots = [
        ForecastSlot(
            dt.datetime.fromtimestamp(
                bucket * step_seconds, tz=dt.timezone.utc
            ),
            loads[bucket],
            max(0.0, pv[bucket]),
        )
        for bucket in common
    ]
    result = simulate_plan(
        config.optimisation,
        ZeroHeroTariff(config.tariff),
        slots,
        initial_soc_percent=numeric_soc[0],
        charge_limit_w=config.optimisation.max_charge_watts,
        discharge_limit_w=config.optimisation.max_discharge_watts,
    )
    print(
        json.dumps(
            {
                "mode": "historical_shadow_replay",
                "slots": len(slots),
                "control_commands_sent": 0,
                **result,
            },
            indent=2,
        )
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = _runtime_config(args)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    daemon = Daemon(config)
    daemon.install_signal_handlers()
    try:
        daemon.run_forever()
    finally:
        daemon.stop()
    return 0


def command_replay(args: argparse.Namespace) -> int:
    capture = json.loads(Path(args.capture).read_text(encoding="utf-8"))
    raw = bytes.fromhex(capture["raw_stream_hex"])
    stream = RTUStreamDecoder()
    matcher = TransactionMatcher()
    decoder = EastronDecoder(
        EastronConfig(enabled=False, grid_slave=1, external_pv_slave=2)
    )
    state = PlantState()
    measurements = 0
    for start in range(0, len(raw), args.chunk_size):
        for frame in stream.feed(raw[start : start + args.chunk_size]):
            transaction = matcher.accept(frame)
            if transaction:
                decoded = decoder.decode(
                    transaction,
                    observed_at=capture.get("started_at_utc"),
                    observed_monotonic=time.monotonic(),
                )
                measurements += len(decoded)
                state.publish_many(decoded)
    print(
        json.dumps(
            {
                "frames": stream.frames,
                "transactions": matcher.matched,
                "measurements": measurements,
                "discarded_bytes": stream.discarded_bytes,
                "suspect_crc_frames": stream.suspect_crc_frames,
                "unmatched_responses": matcher.unmatched_responses,
                "current": state.current(),
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="solplanet-fasttalk")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check-config")
    check.add_argument("--config", required=True)
    _add_api_overrides(check)
    check.set_defaults(handler=command_check)

    run = commands.add_parser("run")
    run.add_argument("--config", required=True)
    _add_api_overrides(run)
    run.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        default="info",
    )
    run.set_defaults(handler=command_run)

    replay = commands.add_parser("replay-eastron")
    replay.add_argument("--capture", required=True)
    replay.add_argument("--chunk-size", type=int, default=37)
    replay.set_defaults(handler=command_replay)

    optimisation = commands.add_parser("replay-optimisation")
    optimisation.add_argument("--config", required=True)
    optimisation.add_argument("--since")
    optimisation.add_argument("--until")
    optimisation.set_defaults(handler=command_replay_optimisation)
    return parser


def _add_api_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-host",
        help="override the configured API bind address for this process",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        help="override the configured API port for this process",
    )
    parser.add_argument(
        "--api-auth-token-file",
        help=(
            "override the API bearer-token file; pass an empty value only "
            "with a loopback bind"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ConfigError, OSError, ValueError, KeyError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
