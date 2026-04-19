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


if __name__ == "__main__":
    unittest.main()
