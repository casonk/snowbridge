from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import cloud_accounts


class CloudAccountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry_path = self.root / "private" / "accounts.local.toml"
        self.rclone_config = self.root / "secrets" / "rclone.conf"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_registry(self, accounts: str = "accounts = []\n", extra: str = "") -> None:
        self.registry_path.parent.mkdir(mode=0o700, exist_ok=True)
        payload = f'schema_version = 1\nrclone_config = "{self.rclone_config}"\n{extra}{accounts}'
        self.registry_path.write_text(
            payload,
            encoding="utf-8",
        )
        self.registry_path.chmod(0o600)

    def test_init_creates_owner_only_empty_registry_and_refuses_overwrite(self) -> None:
        created = cloud_accounts.initialize_registry(self.registry_path, self.rclone_config)

        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(created.parent.stat().st_mode), 0o700)
        loaded = cloud_accounts.load_registry(created)
        self.assertEqual(loaded.rclone_config, self.rclone_config.resolve(strict=False))
        self.assertEqual(loaded.accounts, ())
        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "refusing to overwrite"):
            cloud_accounts.initialize_registry(self.registry_path, self.rclone_config)

    def test_init_then_replace_empty_account_array_with_first_table_validates(self) -> None:
        created = cloud_accounts.initialize_registry(self.registry_path, self.rclone_config)
        payload = created.read_text(encoding="utf-8").replace(
            "accounts = []\n",
            "[[accounts]]\n"
            'id = "local-inventory"\n'
            'backend = "local"\n'
            'remote = "local-inventory"\n'
            'root = "selected/folder"\n'
            f'share_target = "{self.root / "share"}"\n'
            'mode = "inventory"\n'
            "enabled = false\n",
        )
        created.write_text(payload, encoding="utf-8")
        created.chmod(0o600)

        loaded = cloud_accounts.load_registry(created)

        self.assertEqual(len(loaded.accounts), 1)
        self.assertEqual(loaded.accounts[0].backend, "local")

    def test_init_rejects_relative_rclone_path_and_does_not_chmod_arbitrary_parent(self) -> None:
        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "absolute path"):
            cloud_accounts.initialize_registry(self.registry_path, Path("relative/rclone.conf"))

        public_parent = self.root / "public-parent"
        public_parent.mkdir(mode=0o755)
        public_parent.chmod(0o755)
        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "already be owner-only"):
            cloud_accounts.initialize_registry(public_parent / "accounts.toml", self.rclone_config)
        self.assertEqual(stat.S_IMODE(public_parent.stat().st_mode), 0o755)

    def test_init_canonicalizes_an_intermediate_symlink_before_writing(self) -> None:
        real_parent = self.root / "real-parent"
        private_directory = real_parent / "private"
        private_directory.mkdir(mode=0o700, parents=True)
        private_directory.chmod(0o700)
        alias = self.root / "alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        lexical_registry = alias / "private" / "accounts.local.toml"

        created = cloud_accounts.initialize_registry(lexical_registry, self.rclone_config)

        self.assertEqual(
            created,
            real_parent.resolve(strict=True) / "private" / "accounts.local.toml",
        )
        self.assertEqual(cloud_accounts.load_registry(created).accounts, ())

    def test_valid_inventory_account_loads(self) -> None:
        self.write_registry(
            accounts=(
                "[[accounts]]\n"
                'id = "drive-readonly"\n'
                'backend = "drive"\n'
                'remote = "drive-readonly"\n'
                'root = "selected/folder"\n'
                f'share_target = "{self.root / "share"}"\n'
                'mode = "inventory"\n'
                "enabled = true\n"
            )
        )

        loaded = cloud_accounts.load_registry(self.registry_path)

        self.assertEqual(len(loaded.accounts), 1)
        self.assertTrue(loaded.accounts[0].enabled)

    def test_unknown_fields_and_mutating_modes_fail_closed(self) -> None:
        self.write_registry(extra='token = "not-allowed"\n')
        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "unsupported field"):
            cloud_accounts.load_registry(self.registry_path)

        self.registry_path.write_text(
            "schema_version = 1\n"
            f'rclone_config = "{self.rclone_config}"\n'
            "[[accounts]]\n"
            'id = "drive-readonly"\n'
            'backend = "drive"\n'
            'remote = "drive-readonly"\n'
            'root = ""\n'
            f'share_target = "{self.root / "share"}"\n'
            'mode = "bisync"\n'
            "enabled = true\n",
            encoding="utf-8",
        )
        self.registry_path.chmod(0o600)
        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "data mutation"):
            cloud_accounts.load_registry(self.registry_path)

    def test_enabled_account_requires_selected_root(self) -> None:
        for invalid_root in ("", " ", ".", "./selected", "selected//folder"):
            with self.subTest(root=invalid_root):
                self.write_registry(
                    accounts=(
                        "[[accounts]]\n"
                        'id = "drive-readonly"\n'
                        'backend = "drive"\n'
                        'remote = "drive-readonly"\n'
                        f'root = "{invalid_root}"\n'
                        f'share_target = "{self.root / "share"}"\n'
                        'mode = "inventory"\n'
                        "enabled = true\n"
                    )
                )
                with self.assertRaisesRegex(
                    cloud_accounts.CloudAccountError,
                    "select a folder|select a subfolder|whitespace|normalized relative path",
                ):
                    cloud_accounts.load_registry(self.registry_path)

    def test_registry_and_data_paths_must_not_live_inside_git_repo(self) -> None:
        self.registry_path.parent.mkdir(mode=0o700)
        self.registry_path.write_text(
            "schema_version = 1\n"
            f'rclone_config = "{cloud_accounts.REPO_ROOT / "config/cloud/rclone.local.conf"}"\n'
            "accounts = []\n",
            encoding="utf-8",
        )
        self.registry_path.chmod(0o600)
        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "outside the Git repository"):
            cloud_accounts.load_registry(self.registry_path)

    def test_registry_parent_and_file_must_be_owner_only_and_not_symlinks(self) -> None:
        self.write_registry()
        self.registry_path.parent.chmod(0o755)
        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "parent directory"):
            cloud_accounts.load_registry(self.registry_path)

        self.registry_path.parent.chmod(0o700)
        target = self.root / "registry-target.toml"
        self.registry_path.rename(target)
        self.registry_path.symlink_to(target)
        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "non-symlink"):
            cloud_accounts.load_registry(self.registry_path)

    def make_fake_rclone(self, remotes: tuple[tuple[str, str], ...]) -> Path:
        executable = self.root / "fake-rclone"
        inventory = json.dumps(
            [
                {"name": remote, "type": backend, "source": "file", "description": ""}
                for remote, backend in remotes
            ]
        )
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "required = ['--config', '--password-command', '--ask-password=false']\n"
            "if any(flag not in sys.argv for flag in required):\n"
            "    raise SystemExit(8)\n"
            "if sys.argv[-3:] == ['config', 'encryption', 'check']:\n"
            "    raise SystemExit(0)\n"
            "if sys.argv[-2:] == ['listremotes', '--json']:\n"
            f"    print({inventory!r})\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(9)\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        return executable

    def test_doctor_checks_encryption_and_declared_aliases_offline(self) -> None:
        self.write_registry(
            accounts=(
                "[[accounts]]\n"
                'id = "drive-readonly"\n'
                'backend = "drive"\n'
                'remote = "drive-readonly"\n'
                'root = "selected/folder"\n'
                f'share_target = "{self.root / "share"}"\n'
                'mode = "inventory"\n'
                "enabled = true\n"
            )
        )
        self.rclone_config.parent.mkdir(mode=0o700)
        self.rclone_config.write_text("encrypted-test-fixture\n", encoding="utf-8")
        self.rclone_config.chmod(0o600)
        registry = cloud_accounts.load_registry(self.registry_path)
        fake = self.make_fake_rclone((("drive-readonly", "drive"),))

        result = cloud_accounts.doctor_registry(
            registry,
            rclone_binary=os.fspath(fake),
            password_command=("/usr/bin/printf", "synthetic-test-password"),
        )

        self.assertEqual(result, (1, 1))

    def test_doctor_rejects_missing_enabled_remote_without_printing_config(self) -> None:
        self.write_registry(
            accounts=(
                "[[accounts]]\n"
                'id = "drive-readonly"\n'
                'backend = "drive"\n'
                'remote = "drive-readonly"\n'
                'root = "selected/folder"\n'
                f'share_target = "{self.root / "share"}"\n'
                'mode = "inventory"\n'
                "enabled = true\n"
            )
        )
        self.rclone_config.parent.mkdir(mode=0o700)
        self.rclone_config.write_text("encrypted-test-fixture\n", encoding="utf-8")
        self.rclone_config.chmod(0o600)
        registry = cloud_accounts.load_registry(self.registry_path)
        fake = self.make_fake_rclone(())
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "not configured"):
                cloud_accounts.doctor_registry(
                    registry,
                    rclone_binary=os.fspath(fake),
                    password_command=("/usr/bin/printf", "synthetic-test-password"),
                )
        self.assertNotIn("encrypted-test-fixture", stderr.getvalue())

    def test_doctor_rejects_backend_mismatch(self) -> None:
        self.write_registry(
            accounts=(
                "[[accounts]]\n"
                'id = "drive-readonly"\n'
                'backend = "drive"\n'
                'remote = "drive-readonly"\n'
                'root = "selected/folder"\n'
                f'share_target = "{self.root / "share"}"\n'
                'mode = "inventory"\n'
                "enabled = true\n"
            )
        )
        self.rclone_config.parent.mkdir(mode=0o700)
        self.rclone_config.write_text("encrypted-test-fixture\n", encoding="utf-8")
        self.rclone_config.chmod(0o600)
        registry = cloud_accounts.load_registry(self.registry_path)
        fake = self.make_fake_rclone((("drive-readonly", "dropbox"),))

        with self.assertRaisesRegex(cloud_accounts.CloudAccountError, "different rclone backend"):
            cloud_accounts.doctor_registry(
                registry,
                rclone_binary=os.fspath(fake),
                password_command=("/usr/bin/printf", "synthetic-test-password"),
            )

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_real_rclone_encrypted_local_backend_integration(self) -> None:
        rclone = shutil.which("rclone")
        assert rclone is not None
        password_helper = self.root / "password helper"
        password_helper.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'synthetic-integration-password'\n",
            encoding="utf-8",
        )
        password_helper.chmod(0o700)
        self.rclone_config.parent.mkdir(mode=0o700)
        password_command = cloud_accounts._validate_password_command(
            (os.fspath(password_helper),)
        )
        common = [
            rclone,
            "--config",
            os.fspath(self.rclone_config),
            "--password-command",
            password_command,
            "--ask-password=false",
        ]
        subprocess.run(
            [*common, "config", "encryption", "set"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env=cloud_accounts._rclone_environment(),
        )
        subprocess.run(
            [*common, "config", "create", "synthetic-local", "local", "--non-interactive"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env=cloud_accounts._rclone_environment(),
        )
        self.assertEqual(stat.S_IMODE(self.rclone_config.stat().st_mode), 0o600)
        ciphertext = self.rclone_config.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-local", ciphertext)
        self.assertNotIn("type = local", ciphertext)
        self.write_registry(
            accounts=(
                "[[accounts]]\n"
                'id = "synthetic-local"\n'
                'backend = "local"\n'
                'remote = "synthetic-local"\n'
                'root = "selected-folder"\n'
                f'share_target = "{self.root / "share"}"\n'
                'mode = "inventory"\n'
                "enabled = true\n"
            )
        )

        result = cloud_accounts.doctor_registry(
            cloud_accounts.load_registry(self.registry_path),
            rclone_binary=rclone,
            password_command=(os.fspath(password_helper),),
        )

        self.assertEqual(result, (1, 1))


if __name__ == "__main__":
    unittest.main()
