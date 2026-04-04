#!/usr/bin/env python3
"""Unit tests for scripts/check_wireguard_endpoint.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import check_wireguard_endpoint


class ParseEndpointValueTests(unittest.TestCase):
    def test_parses_ipv4_endpoint(self) -> None:
        parsed = check_wireguard_endpoint.parse_endpoint_value("68.41.12.47:51820")
        self.assertEqual(parsed.host, "68.41.12.47")
        self.assertEqual(parsed.port, 51820)

    def test_parses_bracketed_ipv6_endpoint(self) -> None:
        parsed = check_wireguard_endpoint.parse_endpoint_value("[2001:db8::1]:51820")
        self.assertEqual(parsed.host, "2001:db8::1")
        self.assertEqual(parsed.port, 51820)


class RunMonitorTests(unittest.TestCase):
    def test_updates_stale_profiles_regenerates_qrs_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            public_profile = root / "iphone-peer.public-vpn.local.conf"
            lan_profile = root / "iphone-peer.lan-vpn.local.conf"
            for path in (public_profile, lan_profile):
                path.write_text(
                    "[Interface]\n"
                    "Address = 10.99.0.2/32\n"
                    "\n"
                    "[Peer]\n"
                    "Endpoint = 68.41.117.38:51820\n",
                    encoding="utf-8",
                )

            config = check_wireguard_endpoint.MonitorConfig(
                config_path=root / "endpoint-monitor.local.toml",
                host_label="snowbridge",
                shock_relay_root=root,
                state_file=root / "state.json",
                public_ip_lookup_urls=("https://example.test/ip",),
                profiles=(
                    check_wireguard_endpoint.ProfileConfig(
                        name="public",
                        config_path=public_profile,
                        qr_path=root / "public.png",
                    ),
                    check_wireguard_endpoint.ProfileConfig(
                        name="lan",
                        config_path=lan_profile,
                        qr_path=root / "lan.png",
                    ),
                ),
                email=check_wireguard_endpoint.EmailConfig(
                    enabled=True,
                    to_address="user@example.com",
                    config_path=root / "gmail-config.local.yaml",
                    sender_script=root / "send_email.py",
                ),
                signal=check_wireguard_endpoint.SignalConfig(
                    enabled=True,
                    recipient="+15551234567",
                    note_to_self=False,
                    config_path=root / "signal-config.local.yaml",
                    sender_script=root / "send_signal.py",
                ),
            )

            rendered: list[Path] = []
            emails: list[tuple[str, str]] = []
            signals: list[str] = []
            state: dict[str, object] = {}

            outcome = check_wireguard_endpoint.run_monitor(
                config,
                state,
                public_ip_detector=lambda _urls: "68.41.12.47",
                qr_renderer=lambda profile: rendered.append(profile.qr_path),
                email_sender=lambda _cfg, subject, body: emails.append((subject, body)),
                signal_sender=lambda _cfg, message: signals.append(message),
            )

            self.assertTrue(outcome.endpoint_changed)
            self.assertEqual(
                public_profile.read_text(encoding="utf-8").splitlines()[-1],
                "Endpoint = 68.41.12.47:51820",
            )
            self.assertEqual(
                lan_profile.read_text(encoding="utf-8").splitlines()[-1],
                "Endpoint = 68.41.12.47:51820",
            )
            self.assertEqual(rendered, [root / "public.png", root / "lan.png"])
            self.assertEqual(len(emails), 1)
            self.assertEqual(len(signals), 1)
            self.assertEqual(state["last_applied_public_ip"], "68.41.12.47")
            self.assertEqual(state["last_applied_endpoint"], "68.41.12.47:51820")

    def test_skips_repeated_notification_when_endpoint_and_state_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_path = root / "iphone-peer.public-vpn.local.conf"
            profile_path.write_text(
                "[Peer]\nEndpoint = 68.41.12.47:51820\n",
                encoding="utf-8",
            )

            config = check_wireguard_endpoint.MonitorConfig(
                config_path=root / "endpoint-monitor.local.toml",
                host_label="snowbridge",
                shock_relay_root=root,
                state_file=root / "state.json",
                public_ip_lookup_urls=("https://example.test/ip",),
                profiles=(
                    check_wireguard_endpoint.ProfileConfig(
                        name="public",
                        config_path=profile_path,
                        qr_path=root / "public.png",
                    ),
                ),
                email=check_wireguard_endpoint.EmailConfig(
                    enabled=True,
                    to_address="user@example.com",
                    config_path=root / "gmail-config.local.yaml",
                    sender_script=root / "send_email.py",
                ),
                signal=check_wireguard_endpoint.SignalConfig(
                    enabled=True,
                    recipient="+15551234567",
                    note_to_self=False,
                    config_path=root / "signal-config.local.yaml",
                    sender_script=root / "send_signal.py",
                ),
            )

            rendered: list[Path] = []
            emails: list[tuple[str, str]] = []
            signals: list[str] = []
            state: dict[str, object] = {
                "last_applied_public_ip": "68.41.12.47",
                "last_applied_endpoint": "68.41.12.47:51820",
                "notifications": {
                    "email": {"last_sent_endpoint": "68.41.12.47:51820"},
                    "signal": {"last_sent_endpoint": "68.41.12.47:51820"},
                },
            }

            outcome = check_wireguard_endpoint.run_monitor(
                config,
                state,
                public_ip_detector=lambda _urls: "68.41.12.47",
                qr_renderer=lambda profile: rendered.append(profile.qr_path),
                email_sender=lambda _cfg, subject, body: emails.append((subject, body)),
                signal_sender=lambda _cfg, message: signals.append(message),
            )

            self.assertFalse(outcome.endpoint_changed)
            self.assertEqual(emails, [])
            self.assertEqual(signals, [])
            self.assertEqual(rendered, [root / "public.png"])

    def test_retries_unsent_notification_without_rewriting_matching_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_path = root / "iphone-peer.public-vpn.local.conf"
            profile_path.write_text(
                "[Peer]\nEndpoint = 68.41.12.47:51820\n",
                encoding="utf-8",
            )

            config = check_wireguard_endpoint.MonitorConfig(
                config_path=root / "endpoint-monitor.local.toml",
                host_label="snowbridge",
                shock_relay_root=root,
                state_file=root / "state.json",
                public_ip_lookup_urls=("https://example.test/ip",),
                profiles=(
                    check_wireguard_endpoint.ProfileConfig(
                        name="public",
                        config_path=profile_path,
                        qr_path=root / "public.png",
                    ),
                ),
                email=check_wireguard_endpoint.EmailConfig(
                    enabled=True,
                    to_address="user@example.com",
                    config_path=root / "gmail-config.local.yaml",
                    sender_script=root / "send_email.py",
                ),
                signal=check_wireguard_endpoint.SignalConfig(
                    enabled=False,
                    recipient="",
                    note_to_self=False,
                    config_path=root / "signal-config.local.yaml",
                    sender_script=root / "send_signal.py",
                ),
            )

            emails: list[tuple[str, str]] = []
            state: dict[str, object] = {
                "last_applied_public_ip": "68.41.12.47",
                "last_applied_endpoint": "68.41.12.47:51820",
                "notifications": {
                    "email": {"last_sent_endpoint": "68.41.117.38:51820"},
                },
            }

            outcome = check_wireguard_endpoint.run_monitor(
                config,
                state,
                public_ip_detector=lambda _urls: "68.41.12.47",
                qr_renderer=lambda profile: None,
                email_sender=lambda _cfg, subject, body: emails.append((subject, body)),
                signal_sender=lambda _cfg, message: None,
            )

            self.assertFalse(outcome.endpoint_changed)
            self.assertEqual(len(emails), 1)
            self.assertEqual(
                state["notifications"]["email"]["last_sent_endpoint"],  # type: ignore[index]
                "68.41.12.47:51820",
            )

    def test_note_to_self_signal_config_does_not_require_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_path = root / "iphone-peer.public-vpn.local.conf"
            profile_path.write_text(
                "[Peer]\nEndpoint = 68.41.117.38:51820\n",
                encoding="utf-8",
            )

            config = check_wireguard_endpoint.MonitorConfig(
                config_path=root / "endpoint-monitor.local.toml",
                host_label="snowbridge",
                shock_relay_root=root,
                state_file=root / "state.json",
                public_ip_lookup_urls=("https://example.test/ip",),
                profiles=(
                    check_wireguard_endpoint.ProfileConfig(
                        name="public",
                        config_path=profile_path,
                        qr_path=root / "public.png",
                    ),
                ),
                email=check_wireguard_endpoint.EmailConfig(
                    enabled=False,
                    to_address="",
                    config_path=root / "gmail-config.local.yaml",
                    sender_script=root / "send_email.py",
                ),
                signal=check_wireguard_endpoint.SignalConfig(
                    enabled=True,
                    recipient="",
                    note_to_self=True,
                    config_path=root / "signal-config.local.yaml",
                    sender_script=root / "send_signal.py",
                ),
            )

            signals: list[str] = []
            state: dict[str, object] = {}

            outcome = check_wireguard_endpoint.run_monitor(
                config,
                state,
                public_ip_detector=lambda _urls: "68.41.12.47",
                qr_renderer=lambda profile: None,
                email_sender=lambda _cfg, subject, body: None,
                signal_sender=lambda _cfg, message: signals.append(message),
            )

            self.assertTrue(outcome.signal_sent)
            self.assertEqual(len(signals), 1)


if __name__ == "__main__":
    unittest.main()
