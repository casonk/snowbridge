from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_crew_chief_relay.sh"


class SetupCrewChiefRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _env(self, body: str) -> Path:
        path = self.tmp / "filebrowser.env.local"
        path.write_text(body, encoding="utf-8")
        return path

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dry_run_renders_service_running_as_runtime_user(self) -> None:
        env = self._env(
            "SNOWBRIDGE_UID=968\n"
            "SNOWBRIDGE_GID=968\n"
            'FILEBROWSER_CONFIG_DIR="/var/lib/snowbridge/filebrowser/config"\n'
        )
        result = self._run("--env-file", str(env), "--dry-run", "--unit-dir", str(self.tmp / "units"))

        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("--listen=/var/lib/snowbridge/filebrowser/config/crew-chief.sock", out)
        self.assertIn("User=968", out)
        self.assertIn("UMask=0077", out)
        self.assertIn("127.0.0.1:11434", out)
        self.assertNotIn("[Socket]", out)
        self.assertIn("IPAddressAllow=localhost", out)
        self.assertFalse((self.tmp / "units").exists())

    def test_custom_port_and_last_assignment_wins(self) -> None:
        env = self._env(
            "SNOWBRIDGE_UID=1\nSNOWBRIDGE_UID=968\nSNOWBRIDGE_GID=968\n"
            "FILEBROWSER_CONFIG_DIR=/srv/fb/config/\n"
        )
        result = self._run("--env-file", str(env), "--dry-run", "--ollama-port", "11500")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--listen=/srv/fb/config/crew-chief.sock", result.stdout)
        self.assertIn("User=968", result.stdout)
        self.assertIn("127.0.0.1:11500", result.stdout)

    def test_rejects_missing_uid(self) -> None:
        env = self._env("SNOWBRIDGE_GID=968\nFILEBROWSER_CONFIG_DIR=/srv/fb/config\n")
        result = self._run("--env-file", str(env), "--dry-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SNOWBRIDGE_UID", result.stderr)

    def test_rejects_socket_path_over_sun_path_limit(self) -> None:
        env = self._env(
            "SNOWBRIDGE_UID=968\nSNOWBRIDGE_GID=968\n" f"FILEBROWSER_CONFIG_DIR=/{'x' * 120}\n"
        )
        result = self._run("--env-file", str(env), "--dry-run")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("too long", result.stderr)


if __name__ == "__main__":
    unittest.main()
