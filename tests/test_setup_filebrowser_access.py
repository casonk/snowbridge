#!/usr/bin/env python3
"""Unit tests for scripts/setup_filebrowser_access.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from scripts import setup_filebrowser_access

DYNO_LAB_SRC = Path(__file__).resolve().parents[2] / "dyno-lab" / "src"
if str(DYNO_LAB_SRC) not in sys.path:
    sys.path.insert(0, str(DYNO_LAB_SRC))

from dyno_lab.auto_pass import AutoPassPatch, AutoPassRecorder


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


if __name__ == "__main__":
    unittest.main()
