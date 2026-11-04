"""Config loading, endpoint rewriting, QR rendering and notification edges."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import check_wireguard_endpoint as mod

PROFILE_TEXT = (
    "[Interface]\nPrivateKey = CANARYprivatekeyplaceholder\nAddress = 10.0.0.2/32\n\n"
    "[Peer]\nPublicKey = CANARYpublickeyplaceholder\nEndpoint = 203.0.113.9:51820\n"
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name: str, text: str, mode: int | None = None) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
        return path

    def profile(self, text: str = PROFILE_TEXT, mode: int = 0o600) -> mod.ProfileConfig:
        return mod.ProfileConfig(
            name="p",
            config_path=self.write("p.conf", text, mode),
            qr_path=self.root / "qr" / "p.png",
        )


class ConfigLoadingTests(Base):
    def load(self, toml: str) -> mod.MonitorConfig:
        return mod.load_config(self.write("m.toml", toml))

    def test_missing_and_invalid_files(self) -> None:
        with self.assertRaisesRegex(mod.MonitorError, "config not found"):
            mod.load_config(self.root / "nope.toml")
        with self.assertRaisesRegex(mod.MonitorError, "invalid TOML"):
            self.load("this is = = not toml")

    def test_minimal_config_gets_defaults(self) -> None:
        cfg = self.load('[[profiles]]\nname="a"\nconfig_path="/x/a.conf"\nqr_path="/x/a.png"\n')
        self.assertEqual(cfg.host_label, "snowbridge")
        self.assertEqual(cfg.public_ip_lookup_urls, mod.DEFAULT_PUBLIC_IP_LOOKUP_URLS)
        self.assertIsNone(cfg.bind_interface)
        self.assertFalse(cfg.email.enabled)
        self.assertFalse(cfg.signal.enabled)
        self.assertEqual(cfg.state_file, mod.DEFAULT_STATE_PATH)
        self.assertEqual(cfg.profiles[0].config_path, Path("/x/a.conf"))

    def test_relative_paths_resolve_against_the_repo_and_children_against_shock_relay(self) -> None:
        cfg = self.load(
            '[monitor]\nshock_relay_root="/opt/relay"\nstate_file="state/s.json"\n'
            'host_label="  box  "\nbind_interface=" eth0 "\n'
            'public_ip_lookup_urls=[" https://a "]\n'
            '[email]\nenabled=true\nto="x@example.test"\nconfig_path="c.yaml"\n'
            '[signal]\nenabled=true\nrecipient="+10000000000"\n'
            '[[profiles]]\nname=" a "\nconfig_path="conf/a.conf"\nqr_path="/abs/a.png"\n'
        )
        self.assertEqual(cfg.host_label, "box")
        self.assertEqual(cfg.bind_interface, "eth0")
        self.assertEqual(cfg.public_ip_lookup_urls, ("https://a",))
        self.assertEqual(cfg.state_file, (mod.REPO_ROOT / "state/s.json").resolve())
        self.assertEqual(cfg.profiles[0].name, "a")
        self.assertEqual(cfg.profiles[0].config_path, (mod.REPO_ROOT / "conf/a.conf").resolve())
        self.assertEqual(cfg.email.config_path, Path("/opt/relay/c.yaml").resolve())
        self.assertEqual(cfg.email.sender_script, Path("/opt/relay") / mod.DEFAULT_GMAIL_SCRIPT)
        self.assertEqual(cfg.signal.sender_script, Path("/opt/relay") / mod.DEFAULT_SIGNAL_SCRIPT)

    def test_blank_host_label_falls_back_to_the_default(self) -> None:
        cfg = self.load('[monitor]\nhost_label="  "\n[[profiles]]\nname="a"\nconfig_path="c"\nqr_path="q"\n')
        self.assertEqual(cfg.host_label, "snowbridge")

    def test_blank_bind_interface_means_none(self) -> None:
        cfg = self.load('[monitor]\nbind_interface="  "\n[[profiles]]\nname="a"\nconfig_path="c"\nqr_path="q"\n')
        self.assertIsNone(cfg.bind_interface)

    def test_structural_errors(self) -> None:
        profile = '[[profiles]]\nname="a"\nconfig_path="c"\nqr_path="q"\n'
        cases = {
            "[monitor] must be a TOML table": 'monitor = 1\n' + profile,
            "public_ip_lookup_urls": '[monitor]\npublic_ip_lookup_urls=[]\n' + profile,
            "public_ip_lookup_urls ": '[monitor]\npublic_ip_lookup_urls="x"\n' + profile,
            "at least one": "[monitor]\n",
            "must be TOML tables": "profiles = [1]\n",
            "missing required profile key": '[[profiles]]\nname="a"\n',
            "must not be empty": '[[profiles]]\nname="  "\nconfig_path="c"\nqr_path="q"\n',
            "[email] must be": "email = 1\n" + profile,
            "[signal] must be": "signal = 1\n" + profile,
            "[email].to is required": "[email]\nenabled=true\n" + profile,
            "[signal].recipient is required": "[signal]\nenabled=true\n" + profile,
        }
        for fragment, toml in cases.items():
            with self.subTest(fragment), self.assertRaisesRegex(mod.MonitorError, re.escape(fragment.strip())):
                self.load(toml)

    def test_note_to_self_needs_no_recipient(self) -> None:
        cfg = self.load(
            '[signal]\nenabled=true\nnote_to_self=true\n'
            '[[profiles]]\nname="a"\nconfig_path="c"\nqr_path="q"\n'
        )
        self.assertTrue(cfg.signal.note_to_self)


class StateTests(Base):
    def test_load_and_save(self) -> None:
        self.assertEqual(mod.load_state(self.root / "none.json"), {})
        path = self.root / "sub" / "s.json"
        mod.save_state(path, {"b": 1, "a": 2}, dry_run=True)
        self.assertFalse(path.exists())
        mod.save_state(path, {"b": 1, "a": 2}, dry_run=False)
        self.assertEqual(path.read_text(), '{\n  "a": 2,\n  "b": 1\n}\n')
        self.assertEqual(mod.load_state(path), {"a": 2, "b": 1})

    def test_corrupt_state_is_a_clean_error(self) -> None:
        bad = self.write("bad.json", "{oops")
        with self.assertRaisesRegex(mod.MonitorError, "invalid JSON in state file"):
            mod.load_state(bad)


class PublicIpTests(unittest.TestCase):
    def response(self, text: str):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = text.encode()
        return resp

    def test_first_valid_answer_wins_and_failures_fall_through(self) -> None:
        answers = [OSError("down"), self.response("not an ip"), self.response(" 198.51.100.4\n")]
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=answers) as opener:
            self.assertEqual(mod.detect_public_ip(("http://u1", "http://u2", "http://u3")), "198.51.100.4")
        self.assertEqual(opener.call_count, 3)
        self.assertTrue(all(c.kwargs["timeout"] == 10 for c in opener.call_args_list))

    def test_ipv6_is_accepted(self) -> None:
        with mock.patch.object(mod.urllib.request, "urlopen", return_value=self.response("2001:db8::1")):
            self.assertEqual(mod.detect_public_ip(("http://u",)), "2001:db8::1")

    def test_all_failing_raises_with_the_last_error(self) -> None:
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=[OSError("a"), OSError("b")]):
            with self.assertRaisesRegex(mod.MonitorError, "unable to detect public IP: b"):
                mod.detect_public_ip(("http://u1", "http://u2"))
        with self.assertRaisesRegex(mod.MonitorError, "^unable to detect public IP$"):
            mod.detect_public_ip(())

    def test_bound_interface_uses_curl_not_urllib(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="198.51.100.5\n", stderr="")
        with (
            mock.patch.object(mod.subprocess, "run", return_value=done) as run,
            mock.patch.object(mod.urllib.request, "urlopen") as opener,
        ):
            self.assertEqual(mod.detect_public_ip(("https://u",), "eth0"), "198.51.100.5")
        opener.assert_not_called()
        self.assertEqual(
            run.call_args.args[0],
            ["curl", "-s", "--interface", "eth0", "--max-time", "10", "https://u"],
        )

    def test_curl_failure_is_reported(self) -> None:
        failed = subprocess.CompletedProcess([], 7, stdout="", stderr=" refused \n")
        with mock.patch.object(mod.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(mod.MonitorError, "curl exited 7: refused"):
                mod.detect_public_ip(("u",), "eth0")


class EndpointParsingTests(unittest.TestCase):
    def test_parse_shapes(self) -> None:
        p = mod.parse_endpoint_value
        self.assertEqual(p(" host.example.test "), mod.EndpointValue("host.example.test", None))
        self.assertEqual(p("host.example.test:5"), mod.EndpointValue("host.example.test", 5))
        self.assertEqual(p("[::1]"), mod.EndpointValue("::1", None))
        self.assertEqual(p("[::1]:7"), mod.EndpointValue("::1", 7))
        self.assertEqual(p("2001:db8::1"), mod.EndpointValue("2001:db8::1", None))
        self.assertEqual(p("host:notaport"), mod.EndpointValue("host:notaport", None))

    def test_parse_errors_are_monitor_errors(self) -> None:
        for bad in ("", "  ", "[::1", "[::1]x", "[::1]:", "[::1]:abc", "[::1]:-1", "[::1]:0", "[::1]:65536", "1.2.3.4:0", "1.2.3.4:99999", "1.2.3.4:\u0663"):
            with self.subTest(bad=bad), self.assertRaises(mod.MonitorError):
                mod.parse_endpoint_value(bad)

    def test_port_boundaries_are_inclusive(self) -> None:
        self.assertEqual(mod.parse_endpoint_value("1.2.3.4:1").port, 1)
        self.assertEqual(mod.parse_endpoint_value("1.2.3.4:65535").port, 65535)
        self.assertEqual(mod.parse_endpoint_value("[::1]:65535").port, 65535)

    def test_render(self) -> None:
        self.assertEqual(mod.EndpointValue("::1", 5).render(), "[::1]:5")
        self.assertEqual(mod.EndpointValue("::1").render(), "[::1]")
        self.assertEqual(mod.EndpointValue("1.2.3.4").render(), "1.2.3.4")
        self.assertEqual(mod.EndpointValue("h.example.test", 9).render(), "h.example.test:9")

    def test_line_parsing_preserves_formatting(self) -> None:
        prefix, endpoint, suffix = mod._parse_endpoint_line("Endpoint   =   1.2.3.4:5\n")
        self.assertEqual((prefix, endpoint, suffix), ("Endpoint   =   ", mod.EndpointValue("1.2.3.4", 5), "\n"))
        self.assertEqual(mod._parse_endpoint_line("Endpoint=1.2.3.4")[2], "")
        self.assertIsNone(mod._parse_endpoint_line("PersistentKeepalive = 25\n"))
        self.assertIsNone(mod._parse_endpoint_line("# Endpoint = 1.2.3.4\n"))
        self.assertIsNone(mod._parse_endpoint_line("no equals\n"))
        with self.assertRaisesRegex(mod.MonitorError, "Endpoint line is empty"):
            mod._parse_endpoint_line("Endpoint =   \n")

    def test_direct_ip_detection(self) -> None:
        for host in ("203.0.113.1", "::1", "vpn.example.com", "<wireguard-endpoint-hostname-or-ip>"):
            self.assertTrue(mod._is_direct_ip_endpoint(host), host)
        self.assertFalse(mod._is_direct_ip_endpoint("home.example.test"))


class UpdateProfileTests(Base):
    def test_rewrites_only_the_endpoint_line_and_keeps_mode(self) -> None:
        prof = self.profile()
        outcome = mod.update_profile_endpoint(prof, "198.51.100.7", dry_run=False)
        self.assertTrue(outcome.changed)
        self.assertEqual((outcome.old_endpoint, outcome.new_endpoint), ("203.0.113.9:51820", "198.51.100.7:51820"))
        self.assertEqual(prof.config_path.read_text(), PROFILE_TEXT.replace("203.0.113.9", "198.51.100.7"))
        self.assertEqual(stat.S_IMODE(prof.config_path.stat().st_mode), 0o600)

    def test_unchanged_endpoint_does_not_rewrite(self) -> None:
        prof = self.profile()
        before = prof.config_path.stat().st_mtime_ns
        outcome = mod.update_profile_endpoint(prof, "203.0.113.9", dry_run=False)
        self.assertFalse(outcome.changed)
        self.assertEqual(prof.config_path.stat().st_mtime_ns, before)

    def test_dry_run_reports_but_never_writes(self) -> None:
        prof = self.profile()
        outcome = mod.update_profile_endpoint(prof, "198.51.100.7", dry_run=True)
        self.assertTrue(outcome.changed)
        self.assertEqual(prof.config_path.read_text(), PROFILE_TEXT)

    def test_missing_port_defaults_to_51820_and_ipv6_is_bracketed(self) -> None:
        prof = self.profile(PROFILE_TEXT.replace("203.0.113.9:51820", "203.0.113.9"))
        out = mod.update_profile_endpoint(prof, "2001:db8::2", dry_run=False)
        self.assertEqual(out.new_endpoint, "[2001:db8::2]:51820")
        self.assertIn("Endpoint = [2001:db8::2]:51820\n", prof.config_path.read_text())

    def test_hostname_endpoints_are_left_alone(self) -> None:
        text = PROFILE_TEXT.replace("203.0.113.9:51820", "home.example.test:51820")
        prof = self.profile(text)
        out = mod.update_profile_endpoint(prof, "198.51.100.7", dry_run=False)
        self.assertFalse(out.changed)
        self.assertEqual(out.new_endpoint, "home.example.test:51820")
        self.assertEqual(prof.config_path.read_text(), text)

    def test_missing_file_or_endpoint_line(self) -> None:
        gone = mod.ProfileConfig("p", self.root / "gone.conf", self.root / "q.png")
        with self.assertRaisesRegex(mod.MonitorError, "profile config not found"):
            mod.update_profile_endpoint(gone, "198.51.100.7", dry_run=False)
        prof = self.profile("[Interface]\nAddress = 10.0.0.2/32\n")
        with self.assertRaisesRegex(mod.MonitorError, "no Endpoint line"):
            mod.update_profile_endpoint(prof, "198.51.100.7", dry_run=False)

    def test_unparseable_port_is_a_monitor_error_not_a_traceback(self) -> None:
        prof = self.profile(PROFILE_TEXT.replace("203.0.113.9:51820", "[2001:db8::1]:http"))
        with self.assertRaises(mod.MonitorError):
            mod.update_profile_endpoint(prof, "198.51.100.7", dry_run=False)

    def test_crash_while_replacing_keeps_the_original_and_leaves_no_temp_file(self) -> None:
        prof = self.profile()
        with mock.patch.object(mod.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                mod.update_profile_endpoint(prof, "198.51.100.7", dry_run=False)
        self.assertEqual(prof.config_path.read_text(), PROFILE_TEXT)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["p.conf"])

    def test_replacement_is_never_looser_than_the_original(self) -> None:
        for mode in (0o600, 0o640):
            with self.subTest(mode=oct(mode)):
                prof = self.profile(mode=mode)
                old = os.umask(0)
                try:
                    mod.update_profile_endpoint(prof, "198.51.100.7", dry_run=False)
                finally:
                    os.umask(old)
                self.assertEqual(stat.S_IMODE(prof.config_path.stat().st_mode), mode)

    def test_atomic_write_creates_a_missing_file_owner_only(self) -> None:
        target = self.root / "new.conf"
        mod._atomic_write_text(target, "x")
        self.assertEqual((target.read_text(), stat.S_IMODE(target.stat().st_mode)), ("x", 0o600))


class QrTests(Base):
    def fake_qrencode(self, command, input=None, check=None, **_kw):
        Path(command[2]).write_bytes(b"PNG:" + input)
        return subprocess.CompletedProcess(command, 0)

    def test_qr_encodes_the_profile_and_is_owner_only(self) -> None:
        prof = self.profile()
        old = os.umask(0o022)
        try:
            with mock.patch.object(mod.subprocess, "run", side_effect=self.fake_qrencode) as run:
                mod.render_profile_qr(prof, dry_run=False)
        finally:
            os.umask(old)
        self.assertEqual(run.call_args.args[0], ["qrencode", "-o", str(prof.qr_path)])
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(prof.qr_path.read_bytes(), b"PNG:" + PROFILE_TEXT.encode())
        # The QR contains the WireGuard private key.
        self.assertEqual(stat.S_IMODE(prof.qr_path.stat().st_mode), 0o600)

    def test_existing_loose_qr_is_tightened_before_new_content_is_written(self) -> None:
        prof = self.profile()
        prof.qr_path.parent.mkdir()
        prof.qr_path.write_bytes(b"old")
        prof.qr_path.chmod(0o644)
        with mock.patch.object(mod.subprocess, "run", side_effect=self.fake_qrencode):
            mod.render_profile_qr(prof, dry_run=False)
        self.assertEqual(stat.S_IMODE(prof.qr_path.stat().st_mode), 0o600)

    def test_dry_run_touches_nothing(self) -> None:
        prof = self.profile()
        with mock.patch.object(mod.subprocess, "run") as run:
            mod.render_profile_qr(prof, dry_run=True)
        run.assert_not_called()
        self.assertFalse(prof.qr_path.parent.exists())


class NotificationTests(Base):
    def outcome(self, changed=True):
        prof = mod.ProfileOutcome("p", Path("c"), Path("q"), "1.1.1.1:1", "2.2.2.2:1", changed)
        same = mod.ProfileOutcome("s", Path("c"), Path("q"), "2.2.2.2:1", "2.2.2.2:1", False)
        return mod.MonitorOutcome("2.2.2.2", changed, [prof, same], [Path("q.png")])

    def test_body_and_subject(self) -> None:
        body = mod.build_notification_body("box", self.outcome())
        self.assertIn("box detected a WireGuard endpoint change.", body)
        self.assertIn("Latest endpoint: 2.2.2.2:1", body)
        self.assertIn("- p: 1.1.1.1:1 -> 2.2.2.2:1", body)
        self.assertIn("- s: 2.2.2.2:1", body)
        self.assertIn("Regenerated QR codes:\n- q.png", body)
        self.assertNotIn("CANARY", body)
        self.assertEqual(mod._build_notification_subject("box", "e"), "[box] WireGuard endpoint updated: e")
        plain = mod.MonitorOutcome("9.9.9.9", False, [])
        self.assertNotIn("Regenerated", mod.build_notification_body("b", plain))
        self.assertEqual(plain.current_endpoint, "9.9.9.9")

    def test_email_runs_the_sender_with_secrets_suppressed(self) -> None:
        cfg = mod.EmailConfig(True, "to@example.test", Path("/c.yaml"), Path("/send.py"))
        with mock.patch.object(mod.subprocess, "run") as run:
            mod.send_email_notification(cfg, "subj", "body", dry_run=False)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[1:], ["/send.py", "to@example.test", "subj", "body", "--config", "/c.yaml"])
        self.assertEqual(run.call_args.kwargs["env"][mod.AUTO_PASS_NOTIFY_SUPPRESS_ENV], "1")
        self.assertTrue(run.call_args.kwargs["check"])
        for kwargs in ({"dry_run": True},):
            with mock.patch.object(mod.subprocess, "run") as run:
                mod.send_email_notification(cfg, "s", "b", **kwargs)
            run.assert_not_called()
        off = mod.EmailConfig(False, "to@example.test", Path("/c.yaml"), Path("/send.py"))
        with mock.patch.object(mod.subprocess, "run") as run:
            mod.send_email_notification(off, "s", "b", dry_run=False)
        run.assert_not_called()

    def signal_cfg(self, path, note=False, enabled=True):
        return mod.SignalConfig(enabled, "+10000000000", note, path, Path("/sig.py"))

    def test_signal_script_path(self) -> None:
        cfg = self.signal_cfg(Path("/s.yaml"))
        with mock.patch.object(mod.subprocess, "run") as run:
            mod.send_signal_notification(cfg, "msg", dry_run=False)
        self.assertEqual(run.call_args.args[0][1:], ["/sig.py", "+10000000000", "msg", "--config", "/s.yaml"])
        self.assertEqual(run.call_args.kwargs["env"][mod.AUTO_PASS_NOTIFY_SUPPRESS_ENV], "1")
        with mock.patch.object(mod.subprocess, "run") as run:
            mod.send_signal_notification(cfg, "msg", dry_run=True)
            mod.send_signal_notification(self.signal_cfg(Path("/s"), enabled=False), "m", dry_run=False)
        run.assert_not_called()

    def test_signal_note_to_self_uses_signal_cli_with_the_configured_account(self) -> None:
        conf = self.write(
            "s.yaml",
            'other:\n  account: "wrong"\nsignal_cli:\n  account: "+10000000001"  # c\n  bus_name: org.test.Bus\nnext:\n  account: "no"\n',
        )
        with mock.patch.object(mod.subprocess, "run") as run:
            mod.send_signal_notification(self.signal_cfg(conf, note=True), "hello", dry_run=False)
        self.assertEqual(
            run.call_args.args[0],
            ["signal-cli", "-a", "+10000000001", "--bus-name", "org.test.Bus", "send", "--note-to-self", "-m", "hello"],
        )

    def test_signal_identity_parsing(self) -> None:
        conf = self.write("a.yaml", "signal_cli:\n  account: '+1'\n")
        self.assertEqual(mod.load_signal_cli_identity(conf), ("+1", ""))
        conf = self.write("b.yaml", "signal_cli:\n  account: +2\n  bus_name: bare\n")
        self.assertEqual(mod.load_signal_cli_identity(conf), ("+2", "bare"))
        conf = self.write("c.yaml", "signal_cli:\n\n  bus_name: only\n")
        with self.assertRaisesRegex(mod.MonitorError, "missing signal_cli.account"):
            mod.load_signal_cli_identity(conf)
        conf = self.write("d.yaml", "signal_cli:\nother:\n  account: '+3'\n")
        with self.assertRaisesRegex(mod.MonitorError, "missing signal_cli.account"):
            mod.load_signal_cli_identity(conf)
        with self.assertRaisesRegex(mod.MonitorError, "unable to read Signal config"):
            mod.load_signal_cli_identity(self.root / "missing.yaml")


class RunMonitorEdgeTests(Base):
    def config(self, *, email=False, signal=False, profiles=1):
        profs = []
        for i in range(profiles):
            path = self.write(f"p{i}.conf", PROFILE_TEXT, 0o600)
            profs.append(mod.ProfileConfig(f"p{i}", path, self.root / f"p{i}.png"))
        return mod.MonitorConfig(
            config_path=self.root / "m.toml",
            host_label="box",
            shock_relay_root=self.root,
            state_file=self.root / "state.json",
            public_ip_lookup_urls=("u",),
            bind_interface=None,
            profiles=tuple(profs),
            email=mod.EmailConfig(email, "to@example.test", self.root / "c", self.root / "s"),
            signal=mod.SignalConfig(signal, "+10000000000", False, self.root / "c", self.root / "s"),
        )

    def run_it(self, config, state, ip="198.51.100.7", **kwargs):
        qrs, mails, sigs = [], [], []
        outcome = mod.run_monitor(
            config,
            state,
            public_ip_detector=lambda urls, iface: ip,
            qr_renderer=lambda p: (qrs.append(p.name), p.qr_path.write_text("png")),
            email_sender=lambda c, s, b: mails.append((s, b)),
            signal_sender=lambda c, m: sigs.append(m),
            **kwargs,
        )
        return outcome, qrs, mails, sigs

    def test_unchanged_endpoint_with_existing_qr_does_nothing_new(self) -> None:
        config = self.config(email=True, signal=True)
        state: dict = {}
        self.run_it(config, state)  # first run: change + notifications
        outcome, qrs, mails, sigs = self.run_it(config, state)
        self.assertFalse(outcome.endpoint_changed)
        self.assertEqual((qrs, mails, sigs), ([], [], []))
        self.assertFalse(outcome.email_retry_needed or outcome.signal_retry_needed)

    def test_missing_qr_is_regenerated_even_without_a_change(self) -> None:
        config = self.config(profiles=2)
        state: dict = {}
        self.run_it(config, state)
        config.profiles[1].qr_path.unlink()
        outcome, qrs, _, _ = self.run_it(config, state)
        self.assertEqual(qrs, ["p0", "p1"])
        self.assertEqual(outcome.regenerated_qrs, [config.profiles[0].qr_path, config.profiles[1].qr_path])

    def test_failed_notification_is_retried_on_the_next_run(self) -> None:
        config = self.config(email=True)
        state: dict = {}
        with self.assertRaises(RuntimeError):
            mod.run_monitor(
                config, state,
                public_ip_detector=lambda u, i: "198.51.100.7",
                qr_renderer=lambda p: p.qr_path.write_text("x"),
                email_sender=mock.Mock(side_effect=RuntimeError("smtp down")),
            )
        self.assertNotIn("last_sent_endpoint", state["notifications"]["email"])
        _, _, mails, _ = self.run_it(config, state)
        self.assertEqual(len(mails), 1)

    def test_failed_signal_is_retried_on_the_next_run(self) -> None:
        config = self.config(signal=True)
        state: dict = {}
        with self.assertRaises(RuntimeError):
            mod.run_monitor(
                config, state,
                public_ip_detector=lambda u, i: "198.51.100.7",
                qr_renderer=lambda p: p.qr_path.write_text("x"),
                signal_sender=mock.Mock(side_effect=RuntimeError("down")),
            )
        self.assertNotIn("last_sent_endpoint", state["notifications"]["signal"])
        _, _, _, sigs = self.run_it(config, state)
        self.assertEqual(len(sigs), 1)

    def test_state_records_times_ip_and_endpoint(self) -> None:
        config = self.config()
        state: dict = {}
        self.run_it(config, state)
        for key in ("last_checked_at", "last_changed_at"):
            self.assertIn(key, state)
        self.assertEqual(state["last_seen_public_ip"], "198.51.100.7")
        self.assertEqual(state["last_applied_endpoint"], "198.51.100.7:51820")
        first_changed = state["last_changed_at"]
        self.run_it(config, state)
        self.assertEqual(state["last_changed_at"], first_changed)

    def test_notification_only_for_enabled_channels(self) -> None:
        _, _, mails, sigs = self.run_it(self.config(email=True), {})
        self.assertEqual((len(mails), len(sigs)), (1, 0))
        _, _, mails, sigs = self.run_it(self.config(signal=True), {})
        self.assertEqual((len(mails), len(sigs)), (0, 1))

    def test_dry_run_default_renderer_does_not_shell_out(self) -> None:
        config = self.config()
        with mock.patch.object(mod.subprocess, "run") as run:
            outcome = mod.run_monitor(
                config, {}, public_ip_detector=lambda u, i: "198.51.100.7", dry_run=True
            )
        run.assert_not_called()
        self.assertTrue(outcome.endpoint_changed)
        self.assertEqual(config.profiles[0].config_path.read_text(), PROFILE_TEXT)


class MainTests(Base):
    def toml(self, extra=""):
        conf = self.write("p.conf", PROFILE_TEXT, 0o600)
        return self.write(
            "m.toml",
            f'[monitor]\nstate_file="{self.root}/state.json"\n{extra}'
            f'[[profiles]]\nname="a"\nconfig_path="{conf}"\nqr_path="{self.root}/a.png"\n',
        )

    def ip_response(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"198.51.100.7"
        return resp

    def run_main(self, argv):
        out = io.StringIO()
        with (
            mock.patch("sys.argv", ["prog", *argv]),
            mock.patch.object(mod.urllib.request, "urlopen", return_value=self.ip_response()),
            mock.patch.object(mod.subprocess, "run", side_effect=lambda c, **k: Path(c[2]).write_bytes(b"png") or subprocess.CompletedProcess(c, 0)),
            redirect_stdout(out),
        ):
            code = mod.main()
        return code, out.getvalue()

    def test_real_run_saves_state_and_prints_summary(self) -> None:
        config = self.toml()
        code, out = self.run_main(["--config", str(config)])
        self.assertEqual(code, 0)
        self.assertIn("detected public IP: 198.51.100.7", out)
        self.assertIn("updated WireGuard endpoint to 198.51.100.7:51820", out)
        self.assertIn("regenerated 1 QR code(s)", out)
        self.assertTrue((self.root / "state.json").exists())
        self.assertEqual(json.loads((self.root / "state.json").read_text())["last_seen_public_ip"], "198.51.100.7")

    def test_dry_run_prints_prefixed_lines_and_writes_nothing(self) -> None:
        config = self.toml()
        code, out = self.run_main(["--config", str(config), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines())
        self.assertTrue(all(line.startswith("dry-run ") for line in out.splitlines()))
        self.assertFalse((self.root / "state.json").exists())
        self.assertIn("203.0.113.9", (self.root / "p.conf").read_text())

    def test_unchanged_run_says_so(self) -> None:
        config = self.toml()
        self.run_main(["--config", str(config)])
        _, out = self.run_main(["--config", str(config)])
        self.assertIn("WireGuard endpoint unchanged: 198.51.100.7:51820", out)
        self.assertNotIn("regenerated", out)

    def test_subprocess_failure_still_saves_state_and_becomes_a_monitor_error(self) -> None:
        config = self.toml('[email]\nenabled=true\nto="x@example.test"\n')
        error = subprocess.CalledProcessError(3, ["sender"])
        with (
            mock.patch("sys.argv", ["prog", "--config", str(config)]),
            mock.patch.object(mod.urllib.request, "urlopen", return_value=self.ip_response()),
            mock.patch.object(mod.subprocess, "run", side_effect=error),
        ):
            with self.assertRaisesRegex(mod.MonitorError, "exit code 3"):
                mod.main()
        self.assertTrue((self.root / "state.json").exists())

    def test_unexpected_failure_still_saves_state(self) -> None:
        config = self.toml()
        with (
            mock.patch("sys.argv", ["prog", "--config", str(config)]),
            mock.patch.object(mod.urllib.request, "urlopen", return_value=self.ip_response()),
            mock.patch.object(mod, "render_profile_qr", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                mod.main()
        self.assertTrue((self.root / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
