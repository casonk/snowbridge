"""Config validation, owner-only filesystem rules and render/state checks of the Air backend.

Everything lives in a temporary fake home; no Podman, launchd or network is used.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import macos_filebrowser_podman as mod

IMAGE = (
    "docker.io/filebrowser/filebrowser@sha256:"
    "a469ea076d4a1b4b1d86a41d130f2f536cd9da996a2b1fb39c0d7635f9d89b9a"
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.home.chmod(0o700)
        bin_dir = self.home / "bin"
        bin_dir.mkdir(mode=0o700)
        self.podman = bin_dir / "podman"
        self.python = bin_dir / "python3"
        for tool in (self.podman, self.python):
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o700)
        self.share = self.home / "Snowbridge"
        self.state = self.home / ".local/share/snowbridge/air-filebrowser"
        self.config_path = self.home / "air.local.toml"
        self.write_config()

    def text(self, **overrides: str) -> str:
        values = {
            "schema_version": "1",
            "platform": '"macos"',
            "mode": '"rootless-podman"',
            "deployment_id": '"air-primary"',
            "listen_address": '"127.0.0.1"',
            "listen_port": "8080",
            "share_path": f'"{self.share}"',
            "state_directory": f'"{self.state}"',
            "binary": f'"{self.podman}"',
            "machine": '"podman-machine-default"',
            "connection": '"podman-machine-default"',
            "container_name": '"snowbridge-air-filebrowser"',
            "image": f'"{IMAGE}"',
            "python": f'"{self.python}"',
        }
        values.update(overrides)
        v = values
        return (
            f"schema_version = {v['schema_version']}\nplatform = {v['platform']}\n"
            f"mode = {v['mode']}\ndeployment_id = {v['deployment_id']}\n"
            f"[host]\nlisten_address = {v['listen_address']}\nlisten_port = {v['listen_port']}\n"
            f"share_path = {v['share_path']}\nstate_directory = {v['state_directory']}\n"
            f"[podman]\nbinary = {v['binary']}\nmachine = {v['machine']}\n"
            f"connection = {v['connection']}\ncontainer_name = {v['container_name']}\n"
            f"image = {v['image']}\n[launchd]\npython = {v['python']}\n"
        )

    def write_config(self, text: str | None = None, mode: int = 0o600) -> None:
        self.config_path.write_text(text if text is not None else self.text())
        self.config_path.chmod(mode)

    def load(self, **kwargs) -> mod.AirFileBrowserConfig:
        return mod.load_config(self.config_path, home=self.home, **kwargs)

    def rejects(self, fragment: str, text: str | None = None, **kwargs) -> None:
        if text is not None:
            self.write_config(text)
        with self.assertRaisesRegex(mod.AirFileBrowserError, fragment):
            self.load(**kwargs)


class ConfigValidationTests(Base):
    def test_valid_config_loads(self) -> None:
        config = self.load()
        self.assertEqual((config.listen_address, config.listen_port), ("127.0.0.1", 8080))
        self.assertEqual(config.share_path, self.share)
        self.assertEqual(config.podman_binary, self.podman)

    def test_scalar_rules(self) -> None:
        cases = {
            "schema_version must be 1": self.text(schema_version="2"),
            "schema_version must be an integer": self.text(schema_version="true"),
            "schema_version must be an integer ": self.text(schema_version='"1"'),
            "platform must be macos": self.text(platform='"linux"'),
            "mode must be rootless-podman": self.text(mode='"rootful"'),
            "deployment_id must be a safe": self.text(deployment_id='"Bad_Name"'),
            "deployment_id must be a safe ": self.text(deployment_id='"a"'),
            "non-empty string": self.text(deployment_id='""'),
            "must bind exactly": self.text(listen_address='"0.0.0.0"'),
            "must bind exactly ": self.text(listen_address='"::1"'),
            "must bind exactly  ": self.text(listen_port="8081"),
            "safe lowercase": self.text(machine='"Bad Name"', connection='"Bad Name"'),
            "same name": self.text(connection='"other-connection"'),
            "container_name must remain": self.text(container_name='"other-container"'),
            "pinned by sha256": self.text(image='"docker.io/filebrowser/filebrowser:latest"'),
            "pinned by sha256 ": self.text(image='"evil.example/filebrowser@sha256:' + "a" * 64 + '"'),
            "pinned by sha256  ": self.text(image=f'"{IMAGE[:-1]}"'),
            "absolute path": self.text(binary='"relative/podman"'),
        }
        for fragment, text in cases.items():
            with self.subTest(fragment):
                self.rejects(fragment.strip(), text)

    def test_unknown_missing_and_wrong_shaped_tables(self) -> None:
        base = self.text()
        self.rejects("unsupported field", base.replace("[launchd]", 'rogue = 1\n[launchd]'))
        self.rejects("unsupported field", base.replace('deployment_id = "air-primary"\n', 'deployment_id = "air-primary"\nextra = 1\n'))
        self.rejects("missing required field", base.replace('deployment_id = "air-primary"\n', ""))
        self.rejects("missing required field", base.replace('image = "' + IMAGE + '"\n', ""))
        head = base.split("[host]")[0]
        self.rejects("podman must be a TOML table", head + "podman = 5\n" + "[host]" + base.split("[host]")[1].split("[podman]")[0] + "[launchd]" + base.split("[launchd]")[1])

    def test_bad_toml_is_not_swallowed_as_valid(self) -> None:
        self.write_config("this is = = bad")
        with self.assertRaises(Exception) as ctx:
            self.load()
        self.assertNotIsInstance(ctx.exception, mod.AirFileBrowserError)

    def test_config_file_must_be_private_regular_and_small(self) -> None:
        self.write_config(mode=0o640)
        self.rejects("owner-only")
        self.write_config(mode=0o604)
        self.rejects("owner-only")
        self.write_config(mode=0o600)
        link = self.home / "link.toml"
        link.symlink_to(self.config_path)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "must not be a symlink"):
            mod.load_config(link, home=self.home)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "does not exist"):
            mod.load_config(self.home / "missing.toml", home=self.home)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "regular file"):
            mod.load_config(self.home / "bin", home=self.home)
        hard = self.home / "hard.toml"
        os.link(self.config_path, hard)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "singly-linked"):
            mod.load_config(hard, home=self.home)
        hard.unlink()
        with self.assertRaisesRegex(mod.AirFileBrowserError, "10-byte limit"):
            mod._validate_private_file(self.config_path, "cfg", maximum=10)
        self.assertEqual(mod.MAX_CONFIG_BYTES, 128 * 1024)

    def test_size_limit_boundary_is_inclusive(self) -> None:
        size = self.config_path.stat().st_size
        mod._validate_private_file(self.config_path, "cfg", maximum=size)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "byte limit"):
            mod._validate_private_file(self.config_path, "cfg", maximum=size - 1)

    def test_foreign_owner_is_refused(self) -> None:
        with mock.patch.object(mod.os, "getuid", return_value=os.getuid() + 1):
            self.rejects("owned by the current user")

    def test_binaries_must_be_real_executables_unless_skipped(self) -> None:
        self.podman.chmod(0o600)
        self.rejects("not an executable regular file")
        self.assertEqual(self.load(require_binaries=False).podman_binary, self.podman)
        self.podman.chmod(0o700)
        self.podman.unlink()
        self.rejects("does not exist")
        self.podman.write_text("x")
        self.podman.chmod(0o700)
        self.python.unlink()
        self.python.mkdir()
        self.rejects("launchd Python binary")

    def test_the_stable_binary_path_is_kept_not_the_symlink_target(self) -> None:
        real = self.home / "bin" / "podman-5.0"
        self.podman.rename(real)
        self.podman.symlink_to(real)
        self.assertEqual(self.load().podman_binary, self.podman)

    def test_paths_must_stay_inside_a_safe_home(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(outside.rmdir)
        self.rejects("beneath the current user's home", self.text(share_path=f'"{outside}"'))
        self.rejects("beneath the current user's home", self.text(share_path=f'"{self.home}/../escape"'))
        self.rejects("unsafe for a Podman bind mount", self.text(share_path=f'"{self.home}/a,b"'))
        self.rejects("unsafe for a Podman bind mount", self.text(share_path=f'"{self.home}/a\\nb"'))
        self.rejects("unsafe for a Podman bind mount", self.text(share_path=f'"{self.home}/a\\rb"'))
        self.rejects("absolute path", self.text(share_path='"relative"'))
        self.rejects("absolute path", self.text(share_path='"/x\\u0000y"'))
        self.rejects("separate, non-nested", self.text(state_directory=f'"{self.share}/state"'))
        self.rejects("separate, non-nested", self.text(share_path=f'"{self.state}/share"'))

    def test_traversing_symlinks_or_loose_directories_is_refused(self) -> None:
        real = self.home / "real"
        real.mkdir(mode=0o700)
        (self.home / "via").symlink_to(real)
        self.rejects("traverse a symlink", self.text(share_path=f'"{self.home}/via/share"'))
        loose = self.home / "loose"
        loose.mkdir()
        loose.chmod(0o770)
        self.rejects("group/world-writable", self.text(share_path=f'"{loose}/share"'))
        world = self.home / "world"
        world.mkdir()
        world.chmod(0o702)
        self.rejects("group/world-writable", self.text(share_path=f'"{world}/share"'))

    def test_a_dangling_symlink_component_is_still_a_symlink(self) -> None:
        (self.home / "dangling").symlink_to(self.home / "nowhere")
        self.rejects("traverse a symlink", self.text(share_path=f'"{self.home}/dangling/share"'))

    def test_home_itself_must_not_be_writable_by_others(self) -> None:
        self.home.chmod(0o770)
        try:
            self.rejects("not group/world writable")
        finally:
            self.home.chmod(0o700)

    def test_paths_owned_by_someone_else_are_refused(self) -> None:
        sub = self.home / "owned"
        sub.mkdir(mode=0o700)
        real = os.getuid()
        with mock.patch.object(mod.os, "getuid", side_effect=[real] + [real + 1] * 10):
            with self.assertRaisesRegex(mod.AirFileBrowserError, "current-user-owned paths"):
                mod._validate_home_path(sub, self.home, "label")


class DirectoryTests(Base):
    def test_create_makes_owner_only_directories(self) -> None:
        config = self.load()
        mod.ensure_runtime_directories(config, create=True)
        for path in (config.share_path, config.state_directory, config.database_directory,
                     config.config_directory, config.logs_directory):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)

    def test_without_create_a_missing_directory_is_an_error(self) -> None:
        config = self.load()
        with self.assertRaisesRegex(mod.AirFileBrowserError, "does not exist"):
            mod.ensure_runtime_directories(config, create=False)
        self.assertFalse(config.share_path.exists())

    def test_wrong_mode_or_type_is_refused_not_repaired(self) -> None:
        config = self.load()
        mod.ensure_runtime_directories(config, create=True)
        config.share_path.chmod(0o750)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "exact mode 0700"):
            mod.ensure_runtime_directories(config, create=True)
        self.assertEqual(stat.S_IMODE(config.share_path.stat().st_mode), 0o750)
        config.share_path.chmod(0o700)
        config.share_path.rmdir()
        config.share_path.write_text("a file")
        with self.assertRaisesRegex(mod.AirFileBrowserError, "real directory"):
            mod.ensure_runtime_directories(config, create=True)

    def test_stricter_than_0700_is_also_refused_because_the_contract_is_exact(self) -> None:
        config = self.load()
        mod.ensure_runtime_directories(config, create=True)
        config.share_path.chmod(0o500)
        try:
            with self.assertRaisesRegex(mod.AirFileBrowserError, "exact mode 0700"):
                mod.ensure_runtime_directories(config, create=True)
        finally:
            config.share_path.chmod(0o700)

    def test_a_directory_owned_by_another_group_is_refused(self) -> None:
        config = self.load()
        mod.ensure_runtime_directories(config, create=True)
        with mock.patch.object(mod.os, "getgid", return_value=os.getgid() + 1):
            with self.assertRaisesRegex(mod.AirFileBrowserError, "current uid/gid"):
                mod.ensure_runtime_directories(config, create=True)

    def test_extended_acl_is_refused(self) -> None:
        config = self.load()
        with mock.patch.object(mod, "_has_extended_acl", return_value=True):
            with self.assertRaisesRegex(mod.AirFileBrowserError, "extended ACL"):
                mod.ensure_runtime_directories(config, create=True)

    def test_acl_detection(self) -> None:
        path = self.home
        with mock.patch.object(mod.platform, "system", return_value="Linux"):
            self.assertFalse(mod._has_extended_acl(path))
        def result(out, rc=0):
            return subprocess.CompletedProcess([], rc, stdout=out, stderr="")
        with mock.patch.object(mod.platform, "system", return_value="Darwin"):
            with mock.patch.object(mod.subprocess, "run", return_value=result("drwx------+ 2 u g 64 d n\n")) as run:
                self.assertTrue(mod._has_extended_acl(path))
            self.assertEqual(run.call_args.args[0][:2], ["/bin/ls", "-lde"])
            self.assertEqual(run.call_args.kwargs["env"]["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
            with mock.patch.object(mod.subprocess, "run", return_value=result("drwx------ 2 u g 64 d n\n")):
                self.assertFalse(mod._has_extended_acl(path))
            for bad in (result("", 0), result("x", 1)):
                with mock.patch.object(mod.subprocess, "run", return_value=bad):
                    with self.assertRaisesRegex(mod.AirFileBrowserError, "could not verify"):
                        mod._has_extended_acl(path)
            for error in (OSError("x"), subprocess.TimeoutExpired("ls", 5)):
                with mock.patch.object(mod.subprocess, "run", side_effect=error):
                    with self.assertRaisesRegex(mod.AirFileBrowserError, "could not verify"):
                        mod._has_extended_acl(path)


class AtomicWriteTests(Base):
    def test_writes_with_the_requested_mode_and_leaves_no_temp(self) -> None:
        target = self.home / "f.json"
        old = os.umask(0)
        try:
            mod._atomic_write(target, b"one", mode=0o600)
        finally:
            os.umask(old)
        self.assertEqual((target.read_bytes(), stat.S_IMODE(target.stat().st_mode)), (b"one", 0o600))
        mod._atomic_write(target, b"two")
        self.assertEqual(target.read_bytes(), b"two")
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(".f")], [])

    def test_a_stale_temp_path_is_never_followed(self) -> None:
        victim = self.home / "victim"
        victim.write_text("keep")
        target = self.home / "f.json"
        (self.home / f".f.json.tmp-{os.getpid()}").symlink_to(victim)
        with self.assertRaises(OSError):
            mod._atomic_write(target, b"payload")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(target.exists())

    def test_requested_mode_wins_over_the_umask(self) -> None:
        target = self.home / "m.json"
        old = os.umask(0o077)
        try:
            mod._atomic_write(target, b"x", mode=0o644)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_a_file_owned_by_someone_else_is_refused(self) -> None:
        target = self.home / "theirs"
        target.write_text("keep")
        with mock.patch.object(mod.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(mod.AirFileBrowserError, "unowned"):
                mod._atomic_write(target, b"x")
        self.assertEqual(target.read_text(), "keep")

    def test_refuses_to_replace_symlinks_directories_foreign_or_hardlinked_files(self) -> None:
        real = self.home / "real"
        real.write_text("keep")
        link = self.home / "link"
        link.symlink_to(real)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "non-regular"):
            mod._atomic_write(link, b"x")
        self.assertEqual(real.read_text(), "keep")
        with self.assertRaisesRegex(mod.AirFileBrowserError, "non-regular"):
            mod._atomic_write(self.home / "bin", b"x")
        hard = self.home / "hard"
        os.link(real, hard)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "multiply-linked"):
            mod._atomic_write(hard, b"x")
        self.assertEqual(real.read_text(), "keep")

    def test_failed_replace_keeps_the_original_and_removes_the_temp(self) -> None:
        target = self.home / "f"
        target.write_text("orig")
        with mock.patch.object(mod.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                mod._atomic_write(target, b"new")
        self.assertEqual(target.read_text(), "orig")
        self.assertEqual([p.name for p in self.home.iterdir() if ".tmp-" in p.name], [])


class RenderTests(Base):
    def render(self, output: Path, **kwargs):
        return mod.render_bundle(self.load(), self.config_path, output, **kwargs)

    def test_render_writes_owner_only_review_artifacts(self) -> None:
        out = self.home / "out"
        manifest, spec, plist = self.render(out, enforce_repo_boundary=False)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        for path in (manifest, spec, plist):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        data = json.loads(manifest.read_text())
        self.assertFalse(data["requires_root"])
        self.assertFalse(data["touches_smb_or_pf"])
        self.assertFalse(data["podman"]["ambient_connection_allowed"])
        self.assertEqual(data["authentication"]["password_material"], "none")
        self.assertEqual((data["backend"]["host"], data["backend"]["port"]), ("127.0.0.1", 8080))
        spec_data = json.loads(spec.read_text())
        config = self.load()
        self.assertEqual(spec_data["spec_sha256"], mod.container_spec_hash(config))
        self.assertEqual(spec_data["reviewed_create_argv"], mod.build_container_create_command(config))
        self.assertTrue(plist.name.endswith(".plist"))

    def test_render_is_repeatable(self) -> None:
        out = self.home / "out"
        first = [p.read_bytes() for p in self.render(out, enforce_repo_boundary=False)]
        second = [p.read_bytes() for p in self.render(out, enforce_repo_boundary=False)]
        self.assertEqual(first, second)

    def test_output_location_rules(self) -> None:
        with self.assertRaisesRegex(mod.AirFileBrowserError, "beneath the ignored artifacts"):
            self.render(self.home / "elsewhere")
        real = self.home / "real"
        real.mkdir(mode=0o700)
        link = self.home / "link"
        link.symlink_to(real)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "must not be a symlink"):
            self.render(link, enforce_repo_boundary=False)
        loose = self.home / "loose"
        loose.mkdir()
        loose.chmod(0o755)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "exact mode 0700"):
            self.render(loose, enforce_repo_boundary=False)
        afile = self.home / "afile"
        afile.write_text("x")
        with self.assertRaisesRegex(mod.AirFileBrowserError, "real directory"):
            self.render(afile, enforce_repo_boundary=False)

    def test_output_owned_by_someone_else_is_refused(self) -> None:
        out = self.home / "out"
        out.mkdir(mode=0o700)
        config = self.load()
        with mock.patch.object(mod.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(mod.AirFileBrowserError, "current-user-owned"):
                mod.render_bundle(config, self.config_path, out, enforce_repo_boundary=False)

    def test_the_repo_boundary_allows_the_artifacts_tree(self) -> None:
        out = mod.REPO_ROOT / "artifacts" / "test-render-edges"
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        self.render(out)
        self.assertTrue((out / "manifest.json").exists())


class ManagedStateTests(Base):
    def make_state(self, config, marker=None, db_mode=0o600):
        mod.ensure_runtime_directories(config, create=True)
        mod._atomic_write(config.settings_path, mod._settings_payload())
        mod._atomic_write(config.database_path, b"db")
        os.chmod(config.database_path, db_mode)
        mod._atomic_write(
            config.state_marker,
            mod._json_bytes(marker if marker is not None else mod._state_marker_payload(config)),
        )

    def test_matching_state_validates(self) -> None:
        config = self.load()
        self.make_state(config)
        mod.validate_managed_state(config)

    def test_mismatched_marker_is_refused(self) -> None:
        config = self.load()
        marker = mod._state_marker_payload(config)
        marker["image"] = "other"
        self.make_state(config, marker)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "does not match"):
            mod.validate_managed_state(config)

    def test_corrupt_marker_and_loose_files_are_refused(self) -> None:
        config = self.load()
        self.make_state(config)
        config.state_marker.write_text("{oops")
        with self.assertRaisesRegex(mod.AirFileBrowserError, "marker is invalid"):
            mod.validate_managed_state(config)
        self.make_state(config, db_mode=0o644)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "owner-only"):
            mod.validate_managed_state(config)

    def test_missing_pieces_are_refused(self) -> None:
        config = self.load()
        self.make_state(config)
        config.settings_path.unlink()
        with self.assertRaisesRegex(mod.AirFileBrowserError, "does not exist"):
            mod.validate_managed_state(config)

    def test_settings_payload_pins_the_container_paths(self) -> None:
        data = json.loads(mod._settings_payload())
        self.assertEqual(data["database"], "/database/filebrowser.db")
        self.assertEqual(data["root"], "/srv")
        self.assertEqual(data["port"], 8080)


class ProvisionTests(Base):
    class Manager:
        def __init__(self, create_db: bool = True):
            self.stopped = 0
            self.commands: list[list[str]] = []
            self.create_db = create_db
            self.config = None
            self.runner = self

        def stop_managed_container(self):
            self.stopped += 1
            return mod.ContainerState(exists=False)

        def run(self, command, *, acceptable=None, timeout=60):
            self.commands.append(list(command))
            if self.create_db:
                self.config.database_path.write_bytes(b"db")
                os.chmod(self.config.database_path, 0o644)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def test_fresh_provision_claims_state_then_initialises_and_tightens(self) -> None:
        config = self.load()
        manager = self.Manager()
        manager.config = config
        mod.provision_database(config, manager)
        self.assertEqual(manager.stopped, 1)
        self.assertEqual(stat.S_IMODE(config.database_path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(config.state_marker.read_text()), mod._state_marker_payload(config))
        self.assertEqual(manager.commands, [mod.build_filebrowser_config_command(config, initialize=True)])

    def test_reprovision_reconciles_instead_of_reinitialising(self) -> None:
        config = self.load()
        manager = self.Manager()
        manager.config = config
        mod.provision_database(config, manager)
        manager.commands.clear()
        mod.provision_database(config, manager)
        self.assertEqual(manager.commands, [mod.build_filebrowser_config_command(config, initialize=False)])

    def test_unrecognised_state_is_never_adopted(self) -> None:
        config = self.load()
        mod.ensure_runtime_directories(config, create=True)
        config.database_path.write_bytes(b"someone else's database")
        manager = self.Manager()
        manager.config = config
        with self.assertRaisesRegex(mod.AirFileBrowserError, "without the Snowbridge marker"):
            mod.provision_database(config, manager)
        self.assertEqual((manager.stopped, manager.commands), (0, []))

        config.database_path.unlink()
        mod._atomic_write(config.state_marker, mod._json_bytes({"managed_by": "someone-else"}))
        with self.assertRaisesRegex(mod.AirFileBrowserError, "unrecognized File Browser state"):
            mod.provision_database(config, manager)
        mod._atomic_write(config.state_marker, mod._json_bytes([1]))
        with self.assertRaisesRegex(mod.AirFileBrowserError, "unrecognized File Browser state"):
            mod.provision_database(config, manager)
        config.state_marker.write_text("{bad")
        with self.assertRaisesRegex(mod.AirFileBrowserError, "marker is invalid"):
            mod.provision_database(config, manager)

    def test_settings_alone_without_the_marker_is_also_foreign_state(self) -> None:
        config = self.load()
        mod.ensure_runtime_directories(config, create=True)
        config.settings_path.write_text("{}")
        manager = self.Manager()
        manager.config = config
        with self.assertRaisesRegex(mod.AirFileBrowserError, "without the Snowbridge marker"):
            mod.provision_database(config, manager)
        self.assertEqual(manager.stopped, 0)

    def test_hardlinked_database_is_refused(self) -> None:
        config = self.load()
        manager = self.Manager()
        manager.config = config
        mod.provision_database(config, manager)
        os.link(config.database_path, self.home / "second-name")
        with self.assertRaisesRegex(mod.AirFileBrowserError, "not a safe managed file"):
            mod.provision_database(config, manager)

    def test_unsafe_existing_database_is_refused(self) -> None:
        config = self.load()
        manager = self.Manager()
        manager.config = config
        mod.provision_database(config, manager)
        real = self.home / "realdb"
        real.write_bytes(b"x")
        config.database_path.unlink()
        config.database_path.symlink_to(real)
        with self.assertRaisesRegex(mod.AirFileBrowserError, "not a safe managed file"):
            mod.provision_database(config, manager)

    def test_initialiser_that_creates_nothing_is_an_error(self) -> None:
        config = self.load()
        manager = self.Manager(create_db=False)
        manager.config = config
        with self.assertRaisesRegex(mod.AirFileBrowserError, "did not create its managed database"):
            mod.provision_database(config, manager)


class CliTests(Base):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(mod.Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_validate_reports_the_contract(self) -> None:
        code, out, _ = self.run_main("--config", str(self.config_path), "validate")
        self.assertEqual(code, 0)
        self.assertIn("127.0.0.1:8080", out)
        self.assertIn("podman-machine-default", out)

    def test_every_expected_error_class_becomes_exit_2(self) -> None:
        self.write_config(mode=0o644)
        code, _, err = self.run_main("--config", str(self.config_path), "validate")
        self.assertEqual((code, err.startswith("error: ")), (2, True))
        self.write_config("not = = toml")
        code, _, err = self.run_main("--config", str(self.config_path), "validate")
        self.assertEqual(code, 2)
        code, _, err = self.run_main("--config", str(self.home / "none.toml"), "validate")
        self.assertEqual(code, 2)

    def test_render_command_outputs_paths_and_enforces_the_boundary(self) -> None:
        code, _, err = self.run_main(
            "--config", str(self.config_path), "render", "--output", str(self.home / "out")
        )
        self.assertEqual(code, 2)
        self.assertIn("beneath the ignored artifacts", err)
        out_dir = mod.REPO_ROOT / "artifacts" / "test-cli-render-edges"
        self.addCleanup(lambda: __import__("shutil").rmtree(out_dir, ignore_errors=True))
        code, out, _ = self.run_main("--config", str(self.config_path), "render", "--output", str(out_dir))
        self.assertEqual(code, 0)
        self.assertIn("rendered review manifest", out)
        self.assertIn("no service state changed", out)

    def test_init_writes_an_owner_only_config_and_never_overwrites(self) -> None:
        target = self.home / "new.toml"
        code, out, _ = self.run_main("--config", str(target), "init")
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        text = target.read_text()
        self.assertNotIn("<account>", text)
        self.assertNotIn("<podman-binary>", text)
        self.assertNotIn("<python-binary>", text)
        self.assertIn(str(self.home), text)
        before = target.read_bytes()
        code, _, err = self.run_main("--config", str(target), "init")
        self.assertEqual(code, 2)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(target.read_bytes(), before)

    def test_init_refuses_a_dangling_symlink_destination(self) -> None:
        target = self.home / "dangling.toml"
        target.symlink_to(self.home / "nowhere")
        code, _, err = self.run_main("--config", str(target), "init")
        self.assertEqual(code, 2)
        self.assertIn("refusing to overwrite", err)
        self.assertFalse((self.home / "nowhere").exists())

    def test_status_exit_code_reflects_readiness(self) -> None:
        for ready, expected in ((True, 0), (False, 3)):
            with mock.patch.object(mod, "status", return_value=({"ready": ready}, ready)):
                code, out, _ = self.run_main("--config", str(self.config_path), "status")
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(out), {"ready": ready})

    def test_serve_and_bootstrap_dispatch(self) -> None:
        with mock.patch.object(mod, "serve") as serve:
            self.assertEqual(self.run_main("--config", str(self.config_path), "serve")[0], 0)
        serve.assert_called_once()
        with mock.patch.object(mod, "bootstrap") as boot:
            self.assertEqual(self.run_main("--config", str(self.config_path), "bootstrap")[0], 0)
        self.assertEqual(boot.call_args.args[1], self.config_path.resolve())

    def test_a_command_is_required(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mod.main([])


if __name__ == "__main__":
    unittest.main()
