import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import unittest

from solplanet_fasttalk.cli import _runtime_config, build_parser


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"


class CLIRuntimeOverrideTests(unittest.TestCase):
    def test_lan_override_is_validated_without_changing_base_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "runtime.toml"
            config_path.write_text(
                """
[daemon]
database = "history.sqlite3"
[api]
host = "127.0.0.1"
port = 8765
[eastron]
enabled = false
[asw]
enabled = false
""",
                encoding="utf-8",
            )
            token_path = root / "diagnostics.token"
            token_path.write_text("x" * 48, encoding="utf-8")
            token_path.chmod(0o600)
            args = build_parser().parse_args(
                [
                    "run",
                    "--config",
                    str(config_path),
                    "--api-host",
                    "0.0.0.0",
                    "--api-port",
                    "9876",
                    "--api-auth-token-file",
                    str(token_path),
                ]
            )
            config = _runtime_config(args)

        self.assertEqual(config.api.host, "0.0.0.0")
        self.assertEqual(config.api.port, 9876)
        self.assertEqual(config.api.auth_token_file, str(token_path))

    def test_local_override_can_explicitly_remove_configured_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "diagnostics.token"
            token_path.write_text("y" * 48, encoding="utf-8")
            token_path.chmod(0o600)
            config_path = root / "runtime.toml"
            config_path.write_text(
                f"""
[daemon]
database = "history.sqlite3"
[api]
host = "127.0.0.1"
auth_token_file = "{token_path}"
[eastron]
enabled = false
[asw]
enabled = false
""",
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "run",
                    "--config",
                    str(config_path),
                    "--api-host",
                    "127.0.0.1",
                    "--api-auth-token-file=",
                ]
            )
            config = _runtime_config(args)

        self.assertEqual(config.api.host, "127.0.0.1")
        self.assertEqual(config.api.auth_token_file, "")


class OperatorScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.token = self.root / "private" / "diagnostics.token"
        self.config = self.root / "runtime.toml"
        self.config.write_text("# fake daemon fixture\n", encoding="utf-8")
        self.binary = self.root / "solplanet-fasttalk"
        self.binary.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
    check-config) exit 0 ;;
    run)
        trap 'exit 0' TERM INT
        while :; do sleep 0.1; done
        ;;
    *) exit 2 ;;
esac
""",
            encoding="utf-8",
        )
        self.binary.chmod(0o755)
        self.environment = {
            **os.environ,
            "SOLPLANET_FASTTALK_BIN": str(self.binary),
            "SOLPLANET_FASTTALK_CONFIG": str(self.config),
            "SOLPLANET_FASTTALK_STATE_DIR": str(self.state),
            "SOLPLANET_FASTTALK_TOKEN_FILE": str(self.token),
            "SOLPLANET_FASTTALK_LOG_FILE": str(self.root / "daemon.log"),
            "SOLPLANET_FASTTALK_API_PORT": "9876",
        }

    def tearDown(self):
        pid_file = self.state / "daemon.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        self.temporary.cleanup()

    def run_script(self, name, *arguments, expected=0):
        result = subprocess.run(
            [str(SCRIPTS / name), *arguments],
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            expected,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def test_token_lifecycle_does_not_print_secret_during_creation(self):
        created = self.run_script("fasttalk-token.sh", "create")
        token = self.token.read_text(encoding="utf-8").strip()
        self.assertGreaterEqual(len(token), 32)
        self.assertNotIn(token, created.stdout)
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)
        shown = self.run_script("fasttalk-token.sh", "show")
        self.assertIn(token, shown.stdout)
        self.run_script("fasttalk-token.sh", "destroy")
        self.assertFalse(self.token.exists())

    def test_mode_scripts_prevent_parallel_or_wrong_mode_operations(self):
        self.run_script("fasttalk-token.sh", "create")
        self.run_script("fasttalk-local.sh", "start")
        self.run_script("fasttalk-local.sh", "status")
        self.run_script("fasttalk-lan.sh", "start", expected=1)
        self.run_script("fasttalk-local.sh", "stop")

        self.run_script("fasttalk-lan.sh", "start")
        self.run_script("fasttalk-lan.sh", "status")
        self.run_script("fasttalk-local.sh", "stop", expected=1)
        self.run_script("fasttalk-token.sh", "destroy", expected=1)
        self.run_script("fasttalk-lan.sh", "stop")
        self.run_script("fasttalk-token.sh", "destroy")


if __name__ == "__main__":
    unittest.main()
