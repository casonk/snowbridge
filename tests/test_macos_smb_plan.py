from __future__ import annotations

import contextlib
import io
import json
import os
import pwd
import stat
import tempfile
import unittest
from pathlib import Path

from scripts import macos_smb_plan


class MacOSSMBPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.share_path = self.root / "share"
        self.share_path.mkdir(mode=0o700)
        self.config_path = self.root / "air-smb.local.toml"
        self.write_config()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, *, extra: str = "", replacements: dict[str, str] | None = None) -> None:
        payload = (
            'schema_version = 1\nplatform = "macos"\nmode = "render-only"\n'
            'deployment_id = "air-primary"\n'
            "\n[share]\n"
            'name = "snowbridge"\n'
            f'path = "{self.share_path}"\n'
            f'expected_accounts = ["{pwd.getpwuid(os.getuid()).pw_name}"]\n'
            "guest_access = false\nread_only = false\n"
            "smb3_encryption_required = true\n"
            "\n[wireguard]\n"
            'interface = "utun7"\n'
            'host_address = "10.99.0.254/32"\n'
            'allowed_client_addresses = ["10.99.0.241/32", "10.99.0.242/32"]\n'
            "\n[safety]\n"
            "refuse_any_guest_share = true\n"
            "refuse_non_target_shares = true\n"
            "refuse_non_wireguard_listener = true\n"
            "require_pf_default_deny = true\n"
            f"{extra}"
        )
        for source, target in (replacements or {}).items():
            payload = payload.replace(source, target)
        self.config_path.write_text(payload, encoding="utf-8")
        self.config_path.chmod(0o600)

    @staticmethod
    def safe_inventory(**changes: object) -> macos_smb_plan.HostInventory:
        values: dict[str, object] = {
            "guest_smb_share_count": 0,
            "non_target_smb_share_count": 0,
            "target_share_state": "absent",
            "tcp_445_listeners": (),
            "pf_boundary_verified": False,
            "wireguard_interface_present": True,
            "wireguard_addresses": ("10.99.0.254/32",),
            "provenance": "fixture",
        }
        values.update(changes)
        return macos_smb_plan.HostInventory(**values)

    def test_loads_strict_render_only_authenticated_config(self) -> None:
        config = macos_smb_plan.load_config(self.config_path)

        self.assertEqual(config.share.name, "snowbridge")
        self.assertEqual(
            config.share.expected_accounts,
            (pwd.getpwuid(os.getuid()).pw_name,),
        )
        self.assertEqual(str(config.wireguard.host_address), "10.99.0.254/32")

    def test_rejects_unknown_fields_and_non_owner_only_file(self) -> None:
        self.write_config(extra="unexpected = true\n")
        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "unsupported field"):
            macos_smb_plan.load_config(self.config_path)

        self.write_config()
        self.config_path.chmod(0o644)
        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "owner-only"):
            macos_smb_plan.load_config(self.config_path)

    def test_rejects_guest_read_only_unencrypted_and_non_render_modes(self) -> None:
        invalid_replacements = (
            ("guest_access = false", "guest_access = true", "guest_access must be false"),
            ("read_only = false", "read_only = true", "read_only must be false"),
            (
                "smb3_encryption_required = true",
                "smb3_encryption_required = false",
                "encryption_required must be true",
            ),
            ('mode = "render-only"', 'mode = "apply"', "mode must remain render-only"),
        )
        for source, target, message in invalid_replacements:
            with self.subTest(target=target):
                self.write_config(replacements={source: target})
                with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, message):
                    macos_smb_plan.load_config(self.config_path)

    def test_rejects_broad_or_non_host_wireguard_addresses(self) -> None:
        for source, target in (
            ('host_address = "10.99.0.254/32"', 'host_address = "10.99.0.254/24"'),
            ('host_address = "10.99.0.254/32"', 'host_address = "203.0.113.10/32"'),
            ('interface = "utun7"', 'interface = "en0"'),
            (
                'allowed_client_addresses = ["10.99.0.241/32", "10.99.0.242/32"]',
                'allowed_client_addresses = ["10.99.0.0/24"]',
            ),
        ):
            with self.subTest(target=target):
                self.write_config(replacements={source: target})
                with self.assertRaises(macos_smb_plan.MacOSSMBPlanError):
                    macos_smb_plan.load_config(self.config_path)

    def test_parses_macos_sharing_inventory_without_logging_names_or_paths(self) -> None:
        records = macos_smb_plan.parse_sharing_list("""
            List of Share Points
            name:\t\tPublic Folder
            path:\t\t/Users/example/Public
                smb:\t{
                    name:\tPublic Folder
                    shared:\t1
                    guest access:\t1
                    read-only:\t0
                    sealed:\t0
                }
            """)

        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].smb_enabled)
        self.assertTrue(records[0].guest_access)

        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "could not be enumerated"):
            macos_smb_plan.parse_sharing_list("")

    def test_audit_refuses_guest_and_unrelated_shares(self) -> None:
        config = macos_smb_plan.load_config(self.config_path)
        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "guest access"):
            macos_smb_plan.audit_inventory(config, self.safe_inventory(guest_smb_share_count=1))
        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "non-target"):
            macos_smb_plan.audit_inventory(
                config, self.safe_inventory(non_target_smb_share_count=1)
            )

    def test_audit_refuses_wildcard_lan_listener_and_wrong_wireguard_address(self) -> None:
        config = macos_smb_plan.load_config(self.config_path)
        for listener in ("*", "0.0.0.0", "::"):
            with self.subTest(listener=listener):
                with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "PF boundary"):
                    macos_smb_plan.audit_inventory(
                        config, self.safe_inventory(tcp_445_listeners=(listener,))
                    )
        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "broad exposure"):
            macos_smb_plan.audit_inventory(
                config,
                self.safe_inventory(tcp_445_listeners=("192.168.10.125",)),
            )
        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "exactly"):
            macos_smb_plan.audit_inventory(
                config,
                self.safe_inventory(wireguard_addresses=("10.99.0.254/24",)),
            )

    def test_audit_accepts_native_wildcard_listener_only_behind_verified_pf(self) -> None:
        config = macos_smb_plan.load_config(self.config_path)

        macos_smb_plan.audit_inventory(
            config,
            self.safe_inventory(
                tcp_445_listeners=("*", "::"),
                pf_boundary_verified=True,
            ),
        )

    def test_renders_owner_only_non_executable_plan_and_pf_default_deny(self) -> None:
        config = macos_smb_plan.load_config(self.config_path)
        output = self.root / "artifacts"

        plan_path, anchor_path = macos_smb_plan.render_plan(
            config,
            self.safe_inventory(),
            output,
            enforce_repo_boundary=False,
        )

        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(plan_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(anchor_path.stat().st_mode), 0o600)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertFalse(plan["activation_supported"])
        command = plan["review_only_commands"]["proposed_share_change"]
        self.assertEqual(command[command.index("-g") + 1], "000")
        self.assertEqual(command[command.index("-E") + 1], "1")
        anchor = anchor_path.read_text(encoding="utf-8")
        self.assertIn("pass in quick on $wg_if", anchor)
        self.assertIn("from $allowed_clients to $wg_host port 445", anchor)
        self.assertIn("block drop in quick inet proto tcp from any to any port 445", anchor)
        self.assertNotIn("password", plan_path.read_text(encoding="utf-8").lower())

    def test_render_refuses_symlink_share_path(self) -> None:
        target = self.root / "target"
        target.mkdir()
        self.share_path.rmdir()
        self.share_path.symlink_to(target, target_is_directory=True)
        config = macos_smb_plan.load_config(self.config_path)

        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "not a symlink"):
            macos_smb_plan.render_plan(
                config,
                self.safe_inventory(),
                self.root / "artifacts",
                enforce_repo_boundary=False,
                syntax_check=False,
            )

    def test_init_creates_owner_only_template_and_refuses_overwrite(self) -> None:
        destination = self.root / "initialized.local.toml"
        created = macos_smb_plan.initialize_config(destination)

        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)
        with self.assertRaisesRegex(macos_smb_plan.MacOSSMBPlanError, "overwrite"):
            macos_smb_plan.initialize_config(destination)

    def test_cli_has_no_live_apply_command(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                macos_smb_plan.build_parser().parse_args(["apply"])


if __name__ == "__main__":
    unittest.main()
