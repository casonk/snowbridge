from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import macos_filebrowser_podman as air_filebrowser

IMAGE = (
    "docker.io/filebrowser/filebrowser@sha256:"
    "a469ea076d4a1b4b1d86a41d130f2f536cd9da996a2b1fb39c0d7635f9d89b9a"
)


class QueueRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def run(self, command, *, acceptable=None, timeout=60):
        del acceptable, timeout
        self.calls.append(list(command))
        if not self.responses:
            raise AssertionError(f"unexpected command: {command}")
        return self.responses.pop(0)


class FailingRunner:
    def run(self, command, *, acceptable=None, timeout=60):
        del command, acceptable, timeout
        raise air_filebrowser.AirFileBrowserError("synthetic initializer failure")


class ProvisionManager:
    def __init__(self) -> None:
        self.runner = FailingRunner()

    def stop_managed_container(self):
        return air_filebrowser.ContainerState(exists=False)


class SupervisorStopEvent:
    def __init__(self, wait_results: list[bool]) -> None:
        self.wait_results = wait_results
        self.wait_calls: list[float] = []
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float) -> bool:
        self.wait_calls.append(timeout)
        if not self.wait_results:
            raise AssertionError("supervisor performed an unexpected extra wait")
        self.stopped = self.wait_results.pop(0)
        return self.stopped


class SupervisorManager:
    def __init__(
        self,
        config: air_filebrowser.AirFileBrowserConfig,
        states: list[air_filebrowser.ContainerState],
    ) -> None:
        self.config = config
        self.states = states
        self.stop_calls = 0

    def container_state(self) -> air_filebrowser.ContainerState:
        if not self.states:
            raise AssertionError("supervisor performed an unexpected extra inspection")
        return self.states.pop(0)

    def stop_managed_container(self) -> air_filebrowser.ContainerState:
        self.stop_calls += 1
        return air_filebrowser.ContainerState(exists=True, managed=True, running=False)


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AirFileBrowserPodmanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.bin_dir = self.home / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.podman = self.bin_dir / "podman"
        self.python = self.bin_dir / "python3"
        for executable in (self.podman, self.python):
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
        self.share = self.home / "Snowbridge"
        self.state = self.home / ".local/share/snowbridge/air-filebrowser"
        self.config_path = self.home / "air-filebrowser.local.toml"
        self.write_config()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, replacements: dict[str, str] | None = None, extra: str = "") -> None:
        payload = f"""schema_version = 1
platform = "macos"
mode = "rootless-podman"
deployment_id = "air-primary"

[host]
listen_address = "127.0.0.1"
listen_port = 8080
share_path = "{self.share}"
state_directory = "{self.state}"

[podman]
binary = "{self.podman}"
machine = "podman-machine-default"
connection = "podman-machine-default"
container_name = "snowbridge-air-filebrowser"
image = "{IMAGE}"

[launchd]
python = "{self.python}"
{extra}"""
        for source, target in (replacements or {}).items():
            payload = payload.replace(source, target)
        self.config_path.write_text(payload, encoding="utf-8")
        self.config_path.chmod(0o600)

    def config(self) -> air_filebrowser.AirFileBrowserConfig:
        return air_filebrowser.load_config(self.config_path, home=self.home)

    def test_loads_exact_loopback_digest_and_named_rootless_runtime(self) -> None:
        config = self.config()

        self.assertEqual(config.listen_address, "127.0.0.1")
        self.assertEqual(config.listen_port, 8080)
        self.assertEqual(config.podman_machine, "podman-machine-default")
        self.assertEqual(config.podman_connection, "podman-machine-default")
        self.assertEqual(config.image, IMAGE)

    def test_rejects_readable_config_broad_bind_tag_and_ambient_connection(self) -> None:
        self.config_path.chmod(0o644)
        with self.assertRaisesRegex(air_filebrowser.AirFileBrowserError, "owner-only"):
            self.config()

        invalid = (
            ('listen_address = "127.0.0.1"', 'listen_address = "0.0.0.0"'),
            ("listen_port = 8080", "listen_port = 18080"),
            (f'image = "{IMAGE}"', 'image = "docker.io/filebrowser/filebrowser:latest"'),
            (
                'connection = "podman-machine-default"',
                'connection = "ambient-default"',
            ),
        )
        for source, target in invalid:
            with self.subTest(target=target):
                self.write_config({source: target})
                with self.assertRaises(air_filebrowser.AirFileBrowserError):
                    self.config()

    def test_rejects_paths_outside_home_or_nested_with_private_state(self) -> None:
        for source, target in (
            (f'share_path = "{self.share}"', 'share_path = "/srv/snowbridge"'),
            (f'share_path = "{self.share}"', f'share_path = "{self.home}/bad,share"'),
            (
                f'state_directory = "{self.state}"',
                f'state_directory = "{self.share}/state"',
            ),
        ):
            with self.subTest(target=target):
                self.write_config({source: target})
                with self.assertRaises(air_filebrowser.AirFileBrowserError):
                    self.config()

    def test_runtime_directories_are_current_identity_exact_0700_and_not_symlinks(self) -> None:
        config = self.config()
        air_filebrowser.ensure_runtime_directories(config, create=True)

        for path in (
            config.share_path,
            config.state_directory,
            config.database_directory,
            config.config_directory,
            config.logs_directory,
        ):
            details = path.stat()
            self.assertEqual(stat.S_IMODE(details.st_mode), 0o700)
            self.assertEqual(details.st_uid, os.getuid())
            self.assertEqual(details.st_gid, os.getgid())

        config.share_path.chmod(0o755)
        with self.assertRaisesRegex(air_filebrowser.AirFileBrowserError, "0700"):
            air_filebrowser.ensure_runtime_directories(config, create=False)

    def test_container_command_has_only_loopback_publish_and_required_confinement(self) -> None:
        config = self.config()
        command = air_filebrowser.build_container_create_command(config)
        text = "\n".join(command)

        self.assertEqual(
            command[:3], [str(config.podman_binary), "--connection", config.podman_connection]
        )
        self.assertIn("--publish=127.0.0.1:8080:8080/tcp", command)
        self.assertIn("--restart=no", command)
        self.assertIn("--read-only", command)
        self.assertIn("--read-only-tmpfs=false", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn("--http-proxy=false", command)
        self.assertIn(f"--userns=keep-id:uid={os.getuid()},gid={os.getgid()}", command)
        self.assertIn(f"--user={os.getuid()}:{os.getgid()}", command)
        self.assertIn("--tmpfs=/tmp:rw,nodev,nosuid,noexec,size=64m", command)
        self.assertIn(IMAGE, command)
        self.assertNotIn("10.99.0.254", text)
        self.assertNotIn("0.0.0.0:8080:8080", text)
        self.assertNotIn("sudo", text)

    def test_database_init_is_networkless_proxy_auth_without_password_material(self) -> None:
        config = self.config()
        command = air_filebrowser.build_filebrowser_config_command(config, initialize=True)
        text = "\n".join(command)

        self.assertIn("--network=none", command)
        self.assertIn("--auth.method=proxy", command)
        self.assertIn("--auth.header=X-Snowbridge-Auth-User", command)
        self.assertIn("--hideLoginButton=true", command)
        self.assertIn("--perm.execute=false", command)
        self.assertIn("--perm.share=false", command)
        self.assertNotIn("password", text.lower())
        self.assertNotIn("secret", text.lower())

    def test_interrupted_initializer_leaves_recognizable_owner_only_state(self) -> None:
        config = self.config()

        with self.assertRaisesRegex(air_filebrowser.AirFileBrowserError, "synthetic"):
            air_filebrowser.provision_database(config, ProvisionManager())

        self.assertTrue(config.state_marker.is_file())
        self.assertTrue(config.settings_path.is_file())
        self.assertEqual(stat.S_IMODE(config.state_marker.stat().st_mode), 0o600)
        marker = json.loads(config.state_marker.read_text(encoding="utf-8"))
        self.assertEqual(marker["managed_by"], air_filebrowser.LAUNCHD_LABEL)
        self.assertEqual(marker["password_material"], "none")

    def test_render_writes_owner_only_review_bundle_and_login_agent(self) -> None:
        config = self.config()
        output = self.home / "rendered"

        manifest_path, spec_path, plist_path = air_filebrowser.render_bundle(
            config,
            self.config_path,
            output,
            enforce_repo_boundary=False,
        )

        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        for path in (manifest_path, spec_path, plist_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["backend"]["host"], "127.0.0.1")
        self.assertFalse(manifest["backend"]["direct_wireguard_bind"])
        self.assertFalse(manifest["requires_root"])
        self.assertFalse(manifest["touches_smb_or_pf"])
        self.assertEqual(manifest["authentication"]["header"], "X-Snowbridge-Auth-User")
        self.assertEqual(manifest["authentication"]["header_value"], "snowbridge")
        self.assertTrue(manifest["launchd"]["supervises_container_exit"])
        self.assertTrue(manifest["launchd"]["persistent_health_supervisor"])
        self.assertTrue(manifest["launchd"]["termination_stops_managed_container"])
        self.assertTrue(manifest["launchd"]["runtime_or_health_loss_exits_unsuccessfully"])
        self.assertIn("--publish=127.0.0.1:8080:8080/tcp", spec["reviewed_create_argv"])
        self.assertIn("--network=none", spec["reviewed_database_init_argv"])
        plist = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(plist["Label"], air_filebrowser.LAUNCHD_LABEL)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["Umask"], 0o077)
        self.assertEqual(plist["ProgramArguments"][0], str(self.python))
        self.assertEqual(plist["ProgramArguments"][-1], "serve")
        self.assertEqual(plist["ExitTimeOut"], 15)
        combined = manifest_path.read_text() + spec_path.read_text() + plist_path.read_text()
        self.assertNotIn("password=", combined.lower())
        self.assertNotIn("pfctl", combined)
        self.assertNotIn("/usr/sbin/sharing", combined)

    def test_machine_start_is_named_and_does_not_update_ambient_connection(self) -> None:
        config = self.config()
        runner = QueueRunner(
            [
                completed('[{"Name":"podman-machine-default"}]'),
                completed('[{"Rootful":false,"State":"stopped"}]'),
                completed(),
                completed('[{"Rootful":false,"State":"running"}]'),
                completed("{}"),
            ]
        )
        manager = air_filebrowser.PodmanManager(config, runner=runner)

        manager.ensure_machine_running()

        start = runner.calls[2]
        self.assertEqual(
            start,
            [
                str(config.podman_binary),
                "machine",
                "start",
                "--update-connection=false",
                "podman-machine-default",
            ],
        )
        self.assertEqual(
            runner.calls[-1][:3],
            [str(config.podman_binary), "--connection", "podman-machine-default"],
        )

    def test_container_inspection_requires_managed_hash_and_exact_binding(self) -> None:
        config = self.config()
        expected = air_filebrowser.container_spec_hash(config)
        inspect = [
            {
                "Config": {
                    "Labels": {
                        air_filebrowser.MANAGED_LABEL: "true",
                        air_filebrowser.SPEC_LABEL: expected,
                    }
                },
                "State": {"Running": True},
                "NetworkSettings": {
                    "Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}
                },
            }
        ]
        runner = QueueRunner([completed(), completed(json.dumps(inspect))])

        state = air_filebrowser.PodmanManager(config, runner=runner).container_state()

        self.assertTrue(state.exists)
        self.assertTrue(state.managed)
        self.assertTrue(state.running)
        self.assertTrue(state.loopback_binding_valid)
        self.assertEqual(state.spec_hash, expected)

    def supervised_state(self, *, running: bool = True) -> air_filebrowser.ContainerState:
        config = self.config()
        return air_filebrowser.ContainerState(
            exists=True,
            managed=True,
            spec_hash=air_filebrowser.container_spec_hash(config),
            running=running,
            loopback_binding_valid=True,
        )

    def test_supervisor_stays_alive_until_event_and_stops_managed_container(self) -> None:
        config = self.config()
        state = self.supervised_state()
        manager = SupervisorManager(config, [state, state])
        stop_event = SupervisorStopEvent([False, True])
        probes: list[bool] = [True, True]

        air_filebrowser.supervise_backend(
            manager,
            stop_event=stop_event,
            health_probe=lambda: probes.pop(0),
        )

        self.assertEqual(stop_event.wait_calls, [5.0, 5.0])
        self.assertEqual(manager.stop_calls, 1)
        self.assertEqual(probes, [])

    def test_supervisor_health_loss_stops_container_and_requests_launchd_restart(self) -> None:
        config = self.config()
        state = self.supervised_state()
        manager = SupervisorManager(config, [state, state])
        stop_event = SupervisorStopEvent([False])
        probes: list[bool] = [True, False]

        with self.assertRaisesRegex(air_filebrowser.AirFileBrowserError, "health check failed"):
            air_filebrowser.supervise_backend(
                manager,
                stop_event=stop_event,
                health_probe=lambda: probes.pop(0),
            )

        self.assertEqual(manager.stop_calls, 1)

    def test_supervisor_runtime_loss_stops_container_and_requests_restart(self) -> None:
        config = self.config()
        manager = SupervisorManager(config, [self.supervised_state(running=False)])
        stop_event = SupervisorStopEvent([])
        probe_called = False

        def health_probe() -> bool:
            nonlocal probe_called
            probe_called = True
            return True

        with self.assertRaisesRegex(air_filebrowser.AirFileBrowserError, "container stopped"):
            air_filebrowser.supervise_backend(
                manager,
                stop_event=stop_event,
                health_probe=health_probe,
            )

        self.assertFalse(probe_called)
        self.assertEqual(manager.stop_calls, 1)

    def test_backend_readiness_wait_is_event_driven_when_termination_arrives(self) -> None:
        stop_event = SupervisorStopEvent([True])

        ready = air_filebrowser.wait_for_backend(
            seconds=20,
            stop_event=stop_event,
            health_probe=lambda _timeout: False,
        )

        self.assertFalse(ready)
        self.assertEqual(stop_event.wait_calls, [0.25])

    def test_initialize_config_is_owner_only_and_refuses_overwrite(self) -> None:
        destination = self.home / "initialized.local.toml"

        created = air_filebrowser.initialize_config(destination, home=self.home)

        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)
        text = created.read_text(encoding="utf-8")
        self.assertIn(str(self.home / "Snowbridge"), text)
        self.assertIn("@sha256:", text)
        with self.assertRaisesRegex(air_filebrowser.AirFileBrowserError, "overwrite"):
            air_filebrowser.initialize_config(destination, home=self.home)


if __name__ == "__main__":
    unittest.main()
