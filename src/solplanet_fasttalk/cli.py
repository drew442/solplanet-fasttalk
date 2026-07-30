"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import ConfigError, EastronConfig, load_config
from .daemon import Daemon
from .eastron import EastronDecoder
from .model import PlantState
from .modbus import RTUStreamDecoder, TransactionMatcher


def command_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(
        json.dumps(
            {
                "status": "ok",
                "database": config.database,
                "api": {"host": config.api.host, "port": config.api.port},
                "eastron_enabled": config.eastron.enabled,
                "asw_enabled": config.asw.enabled,
                "control_available": False,
            },
            indent=2,
        )
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
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
    check.set_defaults(handler=command_check)

    run = commands.add_parser("run")
    run.add_argument("--config", required=True)
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
    return parser


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

