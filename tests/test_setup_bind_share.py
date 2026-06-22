#!/usr/bin/env python3
"""Unit tests for scripts/setup_bind_share.py."""

from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from scripts import setup_bind_share


class ValidateSourceTests(unittest.TestCase):
    def _ok(self, path: str) -> None:
        setup_bind_share.validate_source(pathlib.Path(path), "[folder x]")

    def _bad(self, path: str) -> None:
        with self.assertRaises(setup_bind_share.ConfigError):
            setup_bind_share.validate_source(pathlib.Path(path), "[folder x]")

    def test_rejects_tmp(self) -> None:
        self._bad("/tmp")

    def test_rejects_path_under_tmp(self) -> None:
        self._bad("/tmp/snowbridge")

    def test_rejects_proc(self) -> None:
        self._bad("/proc")

    def test_rejects_sys(self) -> None:
        self._bad("/sys")

    def test_rejects_dev(self) -> None:
        self._bad("/dev")

    def test_rejects_run(self) -> None:
        self._bad("/run")

    def test_accepts_normal_path(self) -> None:
        self._ok("/home/user/docs")

    def test_accepts_path_sharing_string_prefix_but_not_ancestry(self) -> None:
        # /tmpfs is not a child of /tmp
        self._ok("/tmpfs/data")


class MountedSourceMatchesTests(unittest.TestCase):
    def test_accepts_live_findmnt_device_subpath_for_expected_source(self) -> None:
        expected = pathlib.Path("/mnt/setup/bully/info/receipt")
        live_source = "/dev/mapper/setup[/bully/info/receipt]"

        with mock.patch.object(
            setup_bind_share,
            "mounted_source_details",
            return_value=("/dev/mapper/setup", pathlib.Path("/mnt/setup")),
        ):
            self.assertTrue(setup_bind_share.mounted_source_matches(live_source, expected))

    def test_accepts_live_btrfs_subpath_below_mountpoint(self) -> None:
        expected = pathlib.Path("/home/user/luks")
        live_source = "/dev/nvme1n1p3[/home/user/luks]"

        with mock.patch.object(
            setup_bind_share,
            "mounted_source_details",
            return_value=("/dev/nvme1n1p3[/home]", pathlib.Path("/home")),
        ):
            self.assertTrue(setup_bind_share.mounted_source_matches(live_source, expected))

    def test_rejects_stale_boot_time_btrfs_subpath_for_live_luks_source(self) -> None:
        expected = pathlib.Path("/mnt/4tb-m2/read")
        stale_source = "nvme1n1p3[/root/mnt/4tb-m2/read]"
        live_source = "/dev/mapper/luks-4tb-m2[/read]"

        with mock.patch.object(
            setup_bind_share,
            "mounted_source_details",
            return_value=("/dev/mapper/luks-4tb-m2", pathlib.Path("/mnt/4tb-m2")),
        ):
            self.assertFalse(
                setup_bind_share.mounted_source_matches(stale_source, expected)
            )

    def test_accepts_plain_path_match_via_realpath(self) -> None:
        expected = pathlib.Path("/mnt/4tb-m2/git/personal-finance/artifacts")

        with mock.patch.object(
            setup_bind_share, "mounted_source_details", return_value=None
        ):
            self.assertTrue(
                setup_bind_share.mounted_source_matches(str(expected), expected)
            )


class EnsureBindMountTests(unittest.TestCase):
    def test_repairs_existing_mount_from_wrong_source(self) -> None:
        folder = setup_bind_share.FolderMount(
            name="receipt",
            source=pathlib.Path("/mnt/setup/bully/info/receipt"),
            target_relative=pathlib.PurePosixPath("receipt"),
            target_path=pathlib.Path("/srv/snowbridge/share/receipt"),
            persist=True,
            acl_mode="rwx",
            default_acl_mode="rwx",
            grant_parent_traverse=True,
            create_missing_source=False,
            recursive_acl=True,
        )

        with (
            mock.patch.object(setup_bind_share, "is_mountpoint", return_value=True),
            mock.patch.object(
                setup_bind_share,
                "mounted_sources",
                return_value=["/dev/nvme1n1p3[/root/mnt/setup/bully/info/receipt]"],
            ),
            mock.patch.object(setup_bind_share, "mounted_source_matches", return_value=False),
            mock.patch.object(setup_bind_share, "run_command") as run_command,
        ):
            setup_bind_share.ensure_bind_mount(folder, dry_run=False, skip_mount=False)

        self.assertEqual(
            run_command.call_args_list,
            [
                mock.call(["umount", "/srv/snowbridge/share/receipt"], False),
                mock.call(
                    [
                        "mount",
                        "--bind",
                        "/mnt/setup/bully/info/receipt",
                        "/srv/snowbridge/share/receipt",
                    ],
                    False,
                ),
            ],
        )

    def test_skips_remount_when_skip_mount_requested(self) -> None:
        folder = setup_bind_share.FolderMount(
            name="receipt",
            source=pathlib.Path("/mnt/setup/bully/info/receipt"),
            target_relative=pathlib.PurePosixPath("receipt"),
            target_path=pathlib.Path("/srv/snowbridge/share/receipt"),
            persist=True,
            acl_mode="rwx",
            default_acl_mode="rwx",
            grant_parent_traverse=True,
            create_missing_source=False,
            recursive_acl=True,
        )

        with (
            mock.patch.object(setup_bind_share, "is_mountpoint", return_value=True),
            mock.patch.object(
                setup_bind_share,
                "mounted_sources",
                return_value=["/dev/nvme1n1p3[/root/mnt/setup/bully/info/receipt]"],
            ),
            mock.patch.object(setup_bind_share, "mounted_source_matches", return_value=False),
            mock.patch.object(setup_bind_share, "run_command") as run_command,
        ):
            setup_bind_share.ensure_bind_mount(folder, dry_run=False, skip_mount=True)

        run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
