from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_filebrowser_backend.sh"

FAKE_CURL = """#!/usr/bin/env bash
# Healthy File Browser response: write headers to --dump-header, print 200.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump-header) printf 'HTTP/1.1 200 OK\\r\\nContent-Type: text/html; charset=utf-8\\r\\n' > "$2"; shift 2 ;;
    --output) : > "$2"; shift 2 ;;
    --max-time|--write-out) shift 2 ;;
    *) shift ;;
  esac
done
printf '200'
"""


class CheckFileBrowserBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self._write_tool("curl", FAKE_CURL)

    def _write_tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def _fake_podman(self, running: str, conmon_pid: int) -> None:
        self._write_tool(
            "podman",
            f"#!/usr/bin/env bash\n[[ $1 == inspect ]] && echo '{running} {conmon_pid}'\n",
        )

    def _live_conmon(self) -> int:
        # A process whose comm is "conmon", standing in for Podman's monitor.
        binary = self.tmp / "conmon"
        shutil.copy(shutil.which("sleep"), binary)
        proc = subprocess.Popen([str(binary), "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(50):
            if Path(f"/proc/{proc.pid}/comm").read_text().strip() == "conmon":
                break
            time.sleep(0.01)
        return proc.pid

    def _run(self) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin"}
        return subprocess.run(
            ["bash", str(SCRIPT), "--url", "http://127.0.0.1:8080/"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    @unittest.skipUnless(Path("/proc/self/comm").exists(), "needs procfs")
    def test_healthy_backend_with_live_conmon_passes(self) -> None:
        self._fake_podman("true", self._live_conmon())

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("without conmon", result.stdout + result.stderr)

    def test_running_container_without_conmon_is_unhealthy(self) -> None:
        self._fake_podman("true", 0)

        result = self._run()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("running without conmon (pid 0)", result.stdout + result.stderr)

    def test_conmon_pid_that_is_another_process_is_unhealthy(self) -> None:
        self._fake_podman("true", os.getpid())

        result = self._run()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("running without conmon", result.stdout + result.stderr)

    def test_unknown_container_state_does_not_fail_healthy_probe(self) -> None:
        self._write_tool("podman", "#!/usr/bin/env bash\nexit 125\n")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
