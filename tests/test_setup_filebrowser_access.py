#!/usr/bin/env python3
"""Unit tests for scripts/setup_filebrowser_access.py."""

from __future__ import annotations

import unittest
from pathlib import Path

from dyno_lab.auto_pass import AutoPassPatch, AutoPassRecorder
from scripts import setup_filebrowser_access


class SetupFilebrowserAccessAutoPassTests(unittest.TestCase):
    def test_resolve_keepass_value_loads_profile_and_falls_back_to_prefixed_entry(self) -> None:
        recorder = AutoPassRecorder()
        recorder.add_response(
            "filebrowser-admin",
            recorder.keepass_error("Entry filebrowser-admin was not found."),
        )
        recorder.add_response(
            "snowbridge/filebrowser-admin",
            {"value": "secret-pass"},
        )

        with AutoPassPatch(recorder):
            resolved = setup_filebrowser_access._resolve_keepass_value(
                "filebrowser-admin",
                "password",
                "infra",
            )

        self.assertEqual(resolved, "secret-pass")
        self.assertEqual(recorder.load_calls[0].profile, "infra")
        self.assertTrue(str(recorder.load_calls[0].path).endswith("auto-pass/config/auto-pass.env.local"))
        self.assertEqual(
            [call.entry for call in recorder.resolve_calls],
            ["filebrowser-admin", "snowbridge/filebrowser-admin"],
        )
        self.assertEqual(recorder.resolve_calls[0].attrs_map, {"value": "password"})


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
