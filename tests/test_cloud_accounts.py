from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from scripts import cloud_accounts, rclone_config_password


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

    def test_doctor_separates_a_failing_password_provider_from_a_plaintext_config(
        self,
    ) -> None:
        self.write_registry()
        self.rclone_config.parent.mkdir(mode=0o700)
        self.rclone_config.write_text("encrypted-test-fixture\n", encoding="utf-8")
        self.rclone_config.chmod(0o600)
        registry = cloud_accounts.load_registry(self.registry_path)

        failing = self.root / "password-command-failure"
        failing.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stderr.write('ERROR : Using --password-command returned: exit status 2\\n')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        failing.chmod(0o700)
        with self.assertRaisesRegex(
            cloud_accounts.CloudAccountError, "config-password provider failed"
        ):
            cloud_accounts.doctor_registry(
                registry,
                rclone_binary=os.fspath(failing),
                password_command=("/usr/bin/printf", "synthetic-test-password"),
            )

        plaintext = self.root / "plaintext-config"
        plaintext.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stderr.write('Error: config file is NOT encrypted\\n')\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        plaintext.chmod(0o700)
        with self.assertRaisesRegex(
            cloud_accounts.CloudAccountError, "plaintext configs are refused"
        ):
            cloud_accounts.doctor_registry(
                registry,
                rclone_binary=os.fspath(plaintext),
                password_command=("/usr/bin/printf", "synthetic-test-password"),
            )

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


class CloudProviderTests(unittest.TestCase):
    def test_provider_table_is_unique_and_marks_icloud_as_write_only_enrollment(self) -> None:
        names = [provider.name for provider in cloud_accounts.PROVIDERS]
        backends = [provider.backend for provider in cloud_accounts.PROVIDERS]

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(backends), len(set(backends)))
        self.assertEqual(set(names), {"google-drive", "onedrive", "icloud"})

        icloud = cloud_accounts.PROVIDERS_BY_NAME["icloud"]
        self.assertFalse(icloud.supports_read_only_enrollment)
        self.assertEqual(icloud.credential, "primary-apple-id-password")
        for name in ("google-drive", "onedrive"):
            provider = cloud_accounts.PROVIDERS_BY_NAME[name]
            self.assertTrue(provider.supports_read_only_enrollment)
            self.assertEqual(provider.credential, "oauth-token")

    def test_icloud_notes_do_not_recommend_an_app_specific_password(self) -> None:
        """rclone rejects app-specific passwords for iclouddrive.

        Recommending one sends the operator down a flow that cannot succeed
        and understates the credential's blast radius, so the guidance must
        say the opposite.
        """

        icloud = cloud_accounts.PROVIDERS_BY_NAME["icloud"]
        notes = " ".join(icloud.notes).lower()

        self.assertIn("does not accept app-specific passwords", notes)
        self.assertIn("primary apple id password", notes)
        self.assertNotIn("app-specific password so", notes)
        self.assertNotIn("app-specific passwords", icloud.revocation.lower())

    def test_provider_lookup_by_backend_ignores_unlisted_types(self) -> None:
        self.assertIsNotNone(cloud_accounts.provider_for_backend("drive"))
        self.assertIsNone(cloud_accounts.provider_for_backend("local"))
        self.assertIsNone(cloud_accounts.provider_for_backend("gcs"))

    def test_render_providers_json_covers_every_documented_field(self) -> None:
        payload = json.loads(cloud_accounts.render_providers(as_json=True))

        self.assertEqual(len(payload), len(cloud_accounts.PROVIDERS))
        icloud = next(entry for entry in payload if entry["name"] == "icloud")
        self.assertEqual(icloud["backend"], "iclouddrive")
        self.assertIsNone(icloud["read_only_option"])
        self.assertFalse(icloud["supports_read_only_enrollment"])
        self.assertTrue(icloud["notes"])

    def test_providers_command_runs_without_a_registry(self) -> None:
        missing = Path(tempfile.gettempdir()) / "snowbridge-absent-registry.toml"
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = cloud_accounts.main(["--config", os.fspath(missing), "providers"])

        self.assertEqual(code, 0)
        self.assertIn("iclouddrive", stdout.getvalue())

    def test_describe_accounts_counts_providers_without_naming_accounts(self) -> None:
        accounts = (
            cloud_accounts.CloudAccount(
                account_id="secret-alias",
                backend="drive",
                remote="secret-alias",
                root="folder",
                share_target=Path("/srv/one"),
                mode="inventory",
                enabled=True,
            ),
            cloud_accounts.CloudAccount(
                account_id="another-alias",
                backend="drive",
                remote="another-alias",
                root="folder",
                share_target=Path("/srv/two"),
                mode="inventory",
                enabled=False,
            ),
            cloud_accounts.CloudAccount(
                account_id="third-alias",
                backend="local",
                remote="third-alias",
                root="folder",
                share_target=Path("/srv/three"),
                mode="inventory",
                enabled=False,
            ),
        )

        lines = cloud_accounts.describe_accounts(accounts)
        joined = "\n".join(lines)

        self.assertIn("google-drive: 2 declared, 1 enabled", joined)
        self.assertIn("unlisted backends: 1 declared", joined)
        for alias in ("secret-alias", "another-alias", "third-alias"):
            self.assertNotIn(alias, joined)

    @unittest.skipUnless(shutil.which("rclone"), "rclone is not installed")
    def test_declared_backends_still_exist_in_the_installed_rclone(self) -> None:
        """Catch an rclone rename before it silently invalidates the table."""

        rclone = shutil.which("rclone")
        assert rclone is not None
        completed = subprocess.run(
            [rclone, "config", "providers"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env=cloud_accounts._rclone_environment(),
        )
        available = {entry["Name"] for entry in json.loads(completed.stdout)}

        for provider in cloud_accounts.PROVIDERS:
            with self.subTest(provider=provider.name):
                self.assertIn(provider.backend, available)


class StubKeepassCommandError(Exception):
    pass


class RcloneConfigPasswordTests(unittest.TestCase):
    """Cover the auto-pass password helper without importing auto-pass.

    The cloud CI job installs no Python dependencies and has no sibling
    auto-pass checkout, so the module is stubbed through sys.modules and the
    sibling root is redirected at a temporary directory.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "auto-pass.ini"
        self.auto_pass_root = self.root / "auto-pass"
        (self.auto_pass_root / "src").mkdir(parents=True)

        self._original_root = rclone_config_password.AUTO_PASS_ROOT
        rclone_config_password.AUTO_PASS_ROOT = self.auto_pass_root
        # Keep the real ~/.config/snowbridge/cache untouched by the suite.
        self._original_cache = rclone_config_password.CACHE_DIR
        rclone_config_password.CACHE_DIR = self.root / "cache"
        self.store_configs: list[object] = []
        self._original_path = list(sys.path)
        self._original_modules = {
            name: sys.modules.get(name)
            for name in ("auto_pass", "auto_pass.envfile", "auto_pass.keepassxc")
        }
        self.load_calls: list[tuple[Path, str | None]] = []
        self.resolved: dict[str, str] = {"value": "synthetic-config-password"}
        self.raise_error: Exception | None = None
        self._install_stub()

    def tearDown(self) -> None:
        rclone_config_password.AUTO_PASS_ROOT = self._original_root
        rclone_config_password.CACHE_DIR = self._original_cache
        sys.path[:] = self._original_path
        for name, module in self._original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self.temporary.cleanup()

    def _install_stub(self) -> None:
        package = types.ModuleType("auto_pass")
        package.__path__ = []  # type: ignore[attr-defined]
        envfile = types.ModuleType("auto_pass.envfile")
        keepassxc = types.ModuleType("auto_pass.keepassxc")

        def load_config_environment(path, profile=None):  # noqa: ANN001, ANN202
            self.load_calls.append((path, profile))
            return ({}, {})

        def resolve_keepassxc_entry(entry, attrs_map, config=None):  # noqa: ANN001, ANN202
            self.store_configs.append(config)
            if self.raise_error is not None:
                raise self.raise_error
            return dict(self.resolved)

        class StubStoreConfig:
            def __init__(self, database_password_cache_dir: str = "") -> None:
                self.database_password_cache_dir = database_password_cache_dir

        envfile.load_config_environment = load_config_environment
        keepassxc.resolve_keepassxc_entry = resolve_keepassxc_entry
        keepassxc.KeepassCommandError = StubKeepassCommandError
        keepassxc.KeepassXCStoreConfig = StubStoreConfig
        package.envfile = envfile
        package.keepassxc = keepassxc
        sys.modules["auto_pass"] = package
        sys.modules["auto_pass.envfile"] = envfile
        sys.modules["auto_pass.keepassxc"] = keepassxc

    def write_config(self, body: str) -> None:
        self.config_path.write_text(body, encoding="utf-8")

    def test_cache_directory_is_created_owner_only(self) -> None:
        target = self.root / "config" / "snowbridge" / "cache"

        created = rclone_config_password.prepare_cache_directory(target)

        self.assertEqual(created, target)
        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_cache_directory_permissions_are_tightened_when_already_loose(self) -> None:
        target = self.root / "loose-cache"
        target.mkdir(mode=0o755)
        target.chmod(0o755)

        rclone_config_password.prepare_cache_directory(target)

        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_cache_directory_failure_is_reported_without_a_traceback(self) -> None:
        blocked = self.root / "not-a-directory"
        blocked.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(
            rclone_config_password.PasswordHelperError, "password cache directory"
        ):
            rclone_config_password.prepare_cache_directory(blocked / "cache")

    def test_missing_config_and_missing_entry_fail_closed(self) -> None:
        with self.assertRaisesRegex(rclone_config_password.PasswordHelperError, "does not exist"):
            rclone_config_password.read_helper_config(self.config_path)

        self.write_config("[auto_pass]\nprofile = snowbridge\n")
        with self.assertRaisesRegex(
            rclone_config_password.PasswordHelperError, "rclone_config_keepass_entry"
        ):
            rclone_config_password.read_helper_config(self.config_path)

    def test_config_defaults_field_to_password(self) -> None:
        self.write_config(
            "[auto_pass]\nprofile = snowbridge\n"
            "[cloud]\nrclone_config_keepass_entry = snowbridge/rclone\n"
        )

        profile, entry, field = rclone_config_password.read_helper_config(self.config_path)

        self.assertEqual((profile, entry, field), ("snowbridge", "snowbridge/rclone", "Password"))

    def test_resolve_uses_profile_scoped_env_file_and_returns_value(self) -> None:
        environment_file = self.auto_pass_root / "config" / "auto-pass.env.local"
        environment_file.parent.mkdir(parents=True)
        environment_file.write_text("# synthetic\n", encoding="utf-8")

        password = rclone_config_password.resolve_password(
            "snowbridge", "snowbridge/rclone", "Password"
        )

        self.assertEqual(password, "synthetic-config-password")
        self.assertEqual(len(self.load_calls), 1)
        self.assertEqual(self.load_calls[0][0], environment_file)
        self.assertEqual(self.load_calls[0][1], "snowbridge")
        # The snowbridge-scoped cache must reach auto-pass, otherwise the
        # unlocked password would land in the shared ~/.cache instead.
        self.assertEqual(len(self.store_configs), 1)
        self.assertEqual(
            Path(self.store_configs[0].database_password_cache_dir),
            rclone_config_password.CACHE_DIR,
        )

    def test_lookup_failure_and_empty_value_fail_closed(self) -> None:
        self.raise_error = StubKeepassCommandError("entry not found")
        with self.assertRaisesRegex(
            rclone_config_password.PasswordHelperError, "auto-pass lookup failed"
        ):
            rclone_config_password.resolve_password("p", "entry", "Password")

        self.raise_error = None
        self.resolved = {"value": ""}
        with self.assertRaisesRegex(rclone_config_password.PasswordHelperError, "has no value"):
            rclone_config_password.resolve_password("p", "entry", "Password")

    def _seed_cache(self, database_path: str) -> Path:
        cache_dir = rclone_config_password.prepare_cache_directory(
            rclone_config_password.CACHE_DIR
        )
        cached = rclone_config_password.cached_password_path(cache_dir, database_path)
        cached.write_text('{"password": "stale"}\n', encoding="utf-8")
        cached.chmod(0o600)
        return cached

    def test_cache_filename_is_stable_for_a_database_path(self) -> None:
        first = rclone_config_password.cached_password_path(Path("/cache"), "/vault.kdbx")
        second = rclone_config_password.cached_password_path(Path("/cache"), "/vault.kdbx")
        other = rclone_config_password.cached_password_path(Path("/cache"), "/elsewhere.kdbx")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(first.name.startswith(rclone_config_password.CACHE_PREFIX))

    def test_a_rejected_password_clears_only_that_cache_entry(self) -> None:
        database = "/vault.kdbx"
        cached = self._seed_cache(database)
        keep = rclone_config_password.cached_password_path(cached.parent, "/other.kdbx")
        keep.write_text('{"password": "keep"}\n', encoding="utf-8")

        cleared = rclone_config_password.invalidate_cached_password(cached.parent, database)

        self.assertTrue(cleared)
        self.assertFalse(cached.exists())
        self.assertTrue(keep.exists(), "an unrelated vault's cache must survive")

    def test_invalid_credentials_discard_the_cache_and_say_so(self) -> None:
        database = "/vault.kdbx"
        cached = self._seed_cache(database)
        os.environ["AUTO_PASS_KEEPASSXC_DB_PATH"] = database
        self.addCleanup(os.environ.pop, "AUTO_PASS_KEEPASSXC_DB_PATH", None)
        self.raise_error = StubKeepassCommandError(
            "keepassxc-cli failed: Invalid credentials were provided (HMAC mismatch)"
        )

        with self.assertRaisesRegex(
            rclone_config_password.PasswordHelperError, "discarded the cached database password"
        ):
            rclone_config_password.resolve_password("snowbridge", "entry", "Password")

        self.assertFalse(cached.exists(), "a rejected password must not persist")

    def test_a_missing_entry_leaves_a_working_cached_password_alone(self) -> None:
        database = "/vault.kdbx"
        cached = self._seed_cache(database)
        os.environ["AUTO_PASS_KEEPASSXC_DB_PATH"] = database
        self.addCleanup(os.environ.pop, "AUTO_PASS_KEEPASSXC_DB_PATH", None)
        self.raise_error = StubKeepassCommandError("Could not find entry with path foo/bar")

        with self.assertRaisesRegex(
            rclone_config_password.PasswordHelperError, "auto-pass lookup failed"
        ):
            rclone_config_password.resolve_password("snowbridge", "entry", "Password")

        self.assertTrue(cached.exists(), "the vault password was never in question here")

    def test_multiline_value_is_rejected(self) -> None:
        self.resolved = {"value": "first-line\nsecond-line"}

        with self.assertRaisesRegex(rclone_config_password.PasswordHelperError, "line break"):
            rclone_config_password.resolve_password("p", "entry", "Password")

    def test_check_mode_reports_length_without_printing_the_password(self) -> None:
        self.write_config(
            "[auto_pass]\nprofile = snowbridge\n"
            "[cloud]\nrclone_config_keepass_entry = snowbridge/rclone\n"
        )
        original = rclone_config_password.AUTO_PASS_CONFIG
        rclone_config_password.AUTO_PASS_CONFIG = self.config_path
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                code = rclone_config_password.main(["--check"])
        finally:
            rclone_config_password.AUTO_PASS_CONFIG = original

        self.assertEqual(code, 0)
        self.assertIn("25 characters", stdout.getvalue())
        self.assertNotIn("synthetic-config-password", stdout.getvalue())

    def test_default_mode_writes_only_the_password_to_stdout(self) -> None:
        self.write_config(
            "[auto_pass]\nprofile = snowbridge\n"
            "[cloud]\nrclone_config_keepass_entry = snowbridge/rclone\n"
        )
        original = rclone_config_password.AUTO_PASS_CONFIG
        rclone_config_password.AUTO_PASS_CONFIG = self.config_path
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                code = rclone_config_password.main([])
        finally:
            rclone_config_password.AUTO_PASS_CONFIG = original

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "synthetic-config-password\n")


class RcloneWrapperTests(unittest.TestCase):
    """The wrapper must always pin --config, never rclone's own default."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wrapper = cloud_accounts.REPO_ROOT / "scripts" / "rclone_snowbridge.sh"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fake_rclone_bin(self) -> Path:
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        fake = binary_dir / "rclone"
        fake.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@"\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return binary_dir

    def test_wrapper_is_executable(self) -> None:
        self.assertTrue(self.wrapper.is_file())
        self.assertTrue(os.access(self.wrapper, os.X_OK))

    def test_wrapper_pins_the_snowbridge_config_and_password_command(self) -> None:
        binary_dir = self._fake_rclone_bin()
        config = self.root / "rclone.conf"
        config.write_text("# encrypted fixture\n", encoding="utf-8")
        environment = dict(os.environ)
        environment["PATH"] = f"{binary_dir}:{environment['PATH']}"
        environment["SNOWBRIDGE_RCLONE_CONFIG"] = os.fspath(config)

        completed = subprocess.run(
            [os.fspath(self.wrapper), "listremotes"],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        arguments = completed.stdout.split("\n")
        self.assertIn("--config", arguments)
        self.assertIn(os.fspath(config), arguments)
        self.assertIn("--password-command", arguments)
        self.assertIn("listremotes", arguments)
        password_command = arguments[arguments.index("--password-command") + 1]
        self.assertIn("rclone_config_password.py", password_command)
        interpreter = password_command.split(" ", 1)[0]
        version = subprocess.run(
            [interpreter, "-c", "import sys; print(sys.version_info >= (3, 11))"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(version.stdout.strip(), "True", "wrapper picked a stale interpreter")


class PasswordSourceSelectionTests(unittest.TestCase):
    def test_auto_pass_source_pins_the_interpreter_and_repo_helper(self) -> None:
        command = cloud_accounts.PASSWORD_SOURCES["auto-pass"]

        self.assertEqual(len(command), 2)
        interpreter, script = Path(command[0]), Path(command[1])
        # Pinned rather than relying on the shebang: rclone runs the helper with
        # a minimal PATH whose /usr/bin precedes Homebrew, so `env python3`
        # there is the macOS system Python, older than auto-pass supports.
        self.assertTrue(interpreter.is_absolute())
        self.assertTrue(os.access(interpreter, os.X_OK))
        self.assertEqual(interpreter, Path(sys.executable).resolve())
        self.assertTrue(script.is_absolute())
        self.assertEqual(script.name, "rclone_config_password.py")
        self.assertTrue(script.is_file(), "the auto-pass helper script must exist")
        self.assertTrue(os.access(script, os.X_OK), "the helper must stay directly runnable")

    def test_helper_refuses_an_interpreter_older_than_auto_pass_supports(self) -> None:
        self.assertGreaterEqual(rclone_config_password.MINIMUM_PYTHON, (3, 11))

    def test_helper_command_passes_password_command_validation(self) -> None:
        encoded = cloud_accounts._validate_password_command(
            cloud_accounts.PASSWORD_SOURCES["auto-pass"]
        )

        self.assertIn("rclone_config_password.py", encoded)

    def test_password_source_and_custom_executable_are_mutually_exclusive(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = cloud_accounts.main(
                [
                    "doctor",
                    "--password-source",
                    "auto-pass",
                    "--password-command-executable",
                    "/usr/bin/printf",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
