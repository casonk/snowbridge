from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "prefetch_filebrowser_folder_icons",
    Path(__file__).parents[1] / "scripts/prefetch_filebrowser_folder_icons.py",
)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class PrefetchFileBrowserFolderIconsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _config_file(self, config: dict[str, str]) -> Path:
        path = self.root / "scheduler.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_reads_legacy_json_credentials(self) -> None:
        path = self._config_file(
            {
                "base_url": "http://127.0.0.1:8080",
                "username": "folder-icons-scheduler",
                "password": "test-only-password",
            }
        )

        config = module._read_config(path)

        self.assertEqual(config["username"], "folder-icons-scheduler")
        self.assertNotIn("auth_method", config)

    def test_reads_mtls_proxy_credentials_with_private_key_permissions(self) -> None:
        cert = self.root / "client.crt"
        key = self.root / "client.key"
        ca = self.root / "server-ca.crt"
        for path in (cert, key, ca):
            path.write_text("test certificate material", encoding="utf-8")
        os.chmod(cert, 0o644)
        os.chmod(key, 0o600)
        os.chmod(ca, 0o644)
        config_path = self._config_file(
            {
                "auth_method": "mtls-proxy",
                "base_url": "https://files.snowbridge.internal",
                "username": "snowbridge",
                "client_cert_path": str(cert),
                "client_key_path": str(key),
                "server_ca_path": str(ca),
            }
        )

        config = module._read_config(config_path)

        self.assertEqual(config["auth_method"], "mtls-proxy")
        self.assertEqual(config["username"], "snowbridge")

    def test_rejects_non_loopback_json_url(self) -> None:
        path = self._config_file(
            {
                "base_url": "https://example.com",
                "username": "folder-icons-scheduler",
                "password": "test-only-password",
            }
        )

        with self.assertRaisesRegex(ValueError, "loopback"):
            module._read_config(path)

    def test_mtls_login_and_prefetch_share_client_tls_context(self) -> None:
        config = {
            "auth_method": "mtls-proxy",
            "base_url": "https://files.snowbridge.internal",
            "username": "snowbridge",
            "client_cert_path": str(self.root / "client.crt"),
            "client_key_path": str(self.root / "client.key"),
            "server_ca_path": str(self.root / "server-ca.crt"),
        }
        fake_context = MagicMock()
        strict_flag = module.ssl.VERIFY_X509_STRICT
        trusted_first_flag = module.ssl.VERIFY_X509_TRUSTED_FIRST
        fake_context.verify_flags = strict_flag | trusted_first_flag
        with (
            patch.object(
                module.ssl, "create_default_context", return_value=fake_context
            ) as make_context,
            patch.object(
                module,
                "_request",
                side_effect=[b"header.payload.signature", b'{"folders_generated": 2}'],
            ) as request,
        ):
            result = module.prefetch(config)

        self.assertEqual(result["folders_generated"], 2)
        make_context.assert_called_once_with(cafile=config["server_ca_path"])
        make_context.return_value.load_cert_chain.assert_called_once_with(
            certfile=config["client_cert_path"], keyfile=config["client_key_path"]
        )
        self.assertFalse(fake_context.verify_flags & strict_flag)
        self.assertTrue(fake_context.verify_flags & trusted_first_flag)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[1], b"{}")
        self.assertIs(request.call_args_list[0].kwargs["ssl_context"], fake_context)
        self.assertEqual(request.call_args_list[1].args[2], "header.payload.signature")
        self.assertIs(request.call_args_list[1].args[3], fake_context)


if __name__ == "__main__":
    unittest.main()
