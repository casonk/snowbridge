#!/usr/bin/env python3
"""Unit tests for scripts/setup_filebrowser_access.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dyno_lab.auto_pass import AutoPassPatch, AutoPassRecorder
from scripts import setup_filebrowser_access


class SetupFilebrowserAccessAutoPassTests(unittest.TestCase):
    """The lookup reads the sibling auto-pass checkout; these tests give it a private one.

    ``AUTO_PASS_ROOT`` is the real sibling repository, whose ``auto-pass.env.local`` is an
    ignored, machine-local file. Using it would make the result depend on the machine.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "auto-pass"
        (self.root / "config").mkdir(parents=True)
        patcher = mock.patch.object(setup_filebrowser_access, "AUTO_PASS_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, recorder: AutoPassRecorder) -> str:
        with AutoPassPatch(recorder):
            return setup_filebrowser_access._resolve_keepass_value(
                "filebrowser-admin",
                "password",
                "infra",
            )

    def test_resolve_keepass_value_loads_profile_and_falls_back_to_prefixed_entry(self) -> None:
        (self.root / "config" / "auto-pass.env.local").write_text("", encoding="utf-8")
        recorder = AutoPassRecorder()
        recorder.add_response(
            "filebrowser-admin",
            recorder.keepass_error("Entry filebrowser-admin was not found."),
        )
        recorder.add_response(
            "snowbridge/filebrowser-admin",
            {"value": "secret-pass"},
        )

        resolved = self.resolve(recorder)

        self.assertEqual(resolved, "secret-pass")
        self.assertEqual(recorder.load_calls[0].profile, "infra")
        self.assertTrue(str(recorder.load_calls[0].path).endswith("auto-pass/config/auto-pass.env.local"))
        self.assertEqual(
            [call.entry for call in recorder.resolve_calls],
            ["filebrowser-admin", "snowbridge/filebrowser-admin"],
        )
        self.assertEqual(recorder.resolve_calls[0].attrs_map, {"value": "password"})

    def test_without_a_local_env_file_no_profile_is_loaded_and_the_lookup_still_works(self) -> None:
        recorder = AutoPassRecorder()
        recorder.add_response("filebrowser-admin", {"value": "direct"})

        self.assertEqual(self.resolve(recorder), "direct")
        self.assertEqual(recorder.load_calls, [])

    def test_an_empty_entry_name_resolves_to_nothing_without_touching_auto_pass(self) -> None:
        recorder = AutoPassRecorder()
        with AutoPassPatch(recorder):
            self.assertEqual(setup_filebrowser_access._resolve_keepass_value("", "password"), "")
        self.assertEqual((recorder.load_calls, recorder.resolve_calls), ([], []))


class SetupFilebrowserAccessRuntimeTests(unittest.TestCase):
    def build_runtime_spec(self, filebrowser_image: str | None) -> setup_filebrowser_access.RuntimeSpec:
        return setup_filebrowser_access.RuntimeSpec(
            web_env_file=Path("/tmp/filebrowser.env"),
            web_setup_script=Path("/tmp/setup_caddy_filebrowser.sh"),
            mode="private-vpn",
            container_runtime="podman",
            filebrowser_image=filebrowser_image,
            container_name="snowbridge-filebrowser",
            share_mount_path="/srv",
            database_path="/database/filebrowser.db",
            run_as_account="snowbridge",
            run_as_group="snowbridge",
            sync_web_env_uid_gid=True,
            restart_strategy="recreate",
        )

    def test_resolve_filebrowser_image_prefers_runtime_override(self) -> None:
        runtime_spec = self.build_runtime_spec("ghcr.io/example/filebrowser:dirsize")

        resolved = setup_filebrowser_access.resolve_filebrowser_image(
            runtime_spec,
            {"FILEBROWSER_IMAGE": "docker.io/filebrowser/filebrowser:latest"},
        )

        self.assertEqual(resolved, "ghcr.io/example/filebrowser:dirsize")

    def test_resolve_filebrowser_image_falls_back_to_env(self) -> None:
        runtime_spec = self.build_runtime_spec(None)

        resolved = setup_filebrowser_access.resolve_filebrowser_image(
            runtime_spec,
            {"FILEBROWSER_IMAGE": "ghcr.io/example/filebrowser:dirsize"},
        )

        self.assertEqual(resolved, "ghcr.io/example/filebrowser:dirsize")

    def test_resolve_filebrowser_image_uses_repo_default_when_unset(self) -> None:
        runtime_spec = self.build_runtime_spec(None)

        resolved = setup_filebrowser_access.resolve_filebrowser_image(runtime_spec, {})

        self.assertEqual(resolved, setup_filebrowser_access.DEFAULT_FILEBROWSER_IMAGE)


if __name__ == "__main__":
    unittest.main()
