"""The root-CA profile exporter: what it reads, what it publishes, and who owns the result.

The profile and a raw certificate copy are staged on an SMB share that other devices can read, so
the one thing that must never happen is the CA *private key* ending up there. The tests pin that
only a certificate is accepted, that the profile embeds exactly that certificate with stable
identifiers, that a dry run changes nothing, and that staged files get the intended owner and
modes. Ownership is the running user (so no root is needed) and everything lives in ``tmp_path``.
"""

from __future__ import annotations

import grp
import os
import plistlib
import pwd
import ssl
import stat
import uuid
from pathlib import Path

import pytest

from scripts import export_caddy_root_profile as mod

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_caddy_root_profile.py"

# Not a real certificate, but shaped like one: SEQUENCE { SEQUENCE { ... } ... }.
DER = b"\x30\x82\x01\x0a\x30\x03\x02\x01\x02" + bytes(range(1, 11)) + b"\x00" * 10
PEM = ssl.DER_cert_to_PEM_cert(DER)
ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


def _pem(label: str, body: str) -> str:
    # Assembled at runtime so that no literal private-key block sits in the source, which the
    # repository's secret scanners (rightly) refuse to commit.
    dashes = "-" * 5
    return f"{dashes}BEGIN {label}{dashes}\n{body}\n{dashes}END {label}{dashes}\n"


PRIVATE_KEY_PEM = _pem("PRIVATE KEY", "not a real key - test data only")


@pytest.fixture
def me():
    return mod.Ownership(os.getuid(), os.getgid(), ME, MY_GROUP)


@pytest.fixture(autouse=True)
def not_root(monkeypatch):
    # The tests run as an ordinary user; the root check is covered on its own below.
    monkeypatch.setattr(mod, "require_root", lambda: None)


def run(tmp_path, *args, cert=None):
    cert_path = cert or tmp_path / "root.crt"
    if cert is None:
        cert_path.write_text(PEM)
    out = tmp_path / "share" / "tmp" / "profile.mobileconfig"
    copy = tmp_path / "share" / "tmp" / "root.crt"
    argv = ["prog", "--cert", str(cert_path), "--output", str(out), "--cert-copy-output", str(copy),
            "--owner", ME, "--group", MY_GROUP, *args]  # fmt: skip
    import sys
    from unittest import mock

    with mock.patch.object(sys, "argv", argv):
        code = mod.main()
    return code, out, copy


# --- small helpers ------------------------------------------------------------------------


def test_publishing_requires_root_unless_it_is_a_dry_run(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    with pytest.raises(mod.SetupError, match="run as root"):
        mod.require_root()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    mod.require_root()


# --- staging files ------------------------------------------------------------------------


def test_a_dry_run_creates_nothing_and_says_what_it_would_do(tmp_path, me, capsys):
    target = tmp_path / "deep" / "dir" / "file"
    mod.write_file(target, b"x", 0o644, me, dry_run=True)
    mod.copy_file(tmp_path / "src", target, 0o644, me, dry_run=True)
    assert not (tmp_path / "deep").exists()
    out = capsys.readouterr().out
    assert "would ensure directory" in out and "mode=2770" in out
    assert f"would write {target} mode=644" in out and "would copy" in out


def test_staged_directories_are_group_writable_and_setgid_and_files_get_the_given_mode(tmp_path, me):
    target = tmp_path / "share" / "tmp" / "profile"
    mod.write_file(target, b"payload", 0o644, me, dry_run=False)
    assert target.read_bytes() == b"payload"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o2770
    assert target.stat().st_uid == os.getuid()


def test_copying_keeps_the_content_and_applies_the_mode(tmp_path, me):
    source = tmp_path / "src.crt"
    source.write_bytes(b"cert bytes")
    source.chmod(0o600)
    target = tmp_path / "share" / "copy.crt"
    mod.copy_file(source, target, 0o644, me, dry_run=False)
    assert target.read_bytes() == b"cert bytes"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


# --- reading the certificate --------------------------------------------------------------


# --- the profile --------------------------------------------------------------------------


def test_the_profile_embeds_exactly_the_certificate_with_stable_identifiers():
    data = mod.build_mobileconfig(DER, "local.example.ca", "Example CA", "ExampleOrg", "root.crt")
    profile = plistlib.loads(data)
    assert profile["PayloadType"] == "Configuration" and profile["PayloadVersion"] == 1
    assert profile["PayloadIdentifier"] == "local.example.ca"
    assert profile["PayloadDisplayName"] == "Example CA"
    assert profile["PayloadOrganization"] == "ExampleOrg"
    assert profile["PayloadRemovalDisallowed"] is False  # the user can always remove the trust
    (payload,) = profile["PayloadContent"]
    assert payload["PayloadType"] == "com.apple.security.root"
    assert payload["PayloadIdentifier"] == "local.example.ca.root"
    assert payload["PayloadContent"] == DER
    assert payload["PayloadCertificateFileName"] == "root.crt"
    assert profile["PayloadUUID"] != payload["PayloadUUID"]
    again = plistlib.loads(mod.build_mobileconfig(DER, "local.example.ca", "Example CA", "ExampleOrg", "root.crt"))
    assert again["PayloadUUID"] == profile["PayloadUUID"]  # re-exporting does not change identity
    other = plistlib.loads(mod.build_mobileconfig(DER + b"x", "local.example.ca", "Example CA", "ExampleOrg", "root.crt"))
    assert other["PayloadUUID"] != profile["PayloadUUID"]


def test_the_install_steps_name_the_staged_profile(capsys):
    mod.summarize_install_steps(Path("/srv/share/tmp/snowbridge.mobileconfig"))
    out = capsys.readouterr().out
    assert "next iPhone steps:" in out and "snowbridge.mobileconfig" in out
    assert "Certificate Trust Settings" in out


# --- the command --------------------------------------------------------------------------


def test_a_dry_run_changes_nothing(tmp_path, capsys):
    code, out, copy = run(tmp_path, "--dry-run")
    text = capsys.readouterr().out
    assert code == 0
    assert not out.exists() and not copy.exists() and not out.parent.exists()
    assert f"would stage {out}" in text and f"would stage {copy}" in text


def test_a_real_run_stages_the_profile_and_a_certificate_copy(tmp_path, capsys):
    code, out, copy = run(tmp_path)
    assert code == 0
    profile = plistlib.loads(out.read_bytes())
    assert profile["PayloadContent"][0]["PayloadContent"] == DER
    assert copy.read_text() == PEM  # the raw copy is the public certificate, byte for byte
    assert stat.S_IMODE(out.stat().st_mode) == 0o644 and stat.S_IMODE(copy.stat().st_mode) == 0o644
    assert f"staged {out}" in capsys.readouterr().out


def test_the_certificate_copy_can_be_skipped(tmp_path):
    code, out, copy = run(tmp_path, "--skip-cert-copy")
    assert code == 0 and out.exists() and not copy.exists()


def test_a_private_key_passed_as_the_certificate_publishes_nothing(tmp_path, capsys):
    key = tmp_path / "root.key"
    key.write_text(PRIVATE_KEY_PEM)
    code, out, copy = run(tmp_path, cert=key)
    assert code == 1
    assert "private key" in capsys.readouterr().err
    assert not out.exists() and not copy.exists() and not out.parent.exists()


def test_errors_are_one_line_messages_not_tracebacks(tmp_path, capsys):
    code, out, copy = run(tmp_path, cert=tmp_path / "absent.crt")
    err = capsys.readouterr().err
    assert code == 1 and err.startswith("error: certificate not found") and "Traceback" not in err
    assert not out.exists()


def test_an_unknown_owner_is_refused_before_anything_is_written(tmp_path, capsys, monkeypatch):
    import sys

    cert = tmp_path / "root.crt"
    cert.write_text(PEM)
    out = tmp_path / "out" / "p.mobileconfig"
    argv = ["prog", "--cert", str(cert), "--output", str(out), "--owner", "no-such-user-xyz"]
    monkeypatch.setattr(sys, "argv", argv)
    assert mod.main() == 1
    assert "owner account not found" in capsys.readouterr().err
    assert not out.parent.exists()


def test_the_defaults_point_at_the_share_and_the_local_root(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["prog"])
    args = mod.parse_args()
    assert args.cert == str(mod.DEFAULT_CERT_PATH)
    assert Path(args.output).parent == mod.DEFAULT_SHARE_TMP
    assert Path(args.cert_copy_output).parent == mod.DEFAULT_SHARE_TMP
    assert args.dry_run is False and args.skip_cert_copy is False
    assert (args.owner, args.group) == ("snowbridge", "snowbridge")


def test_running_the_script_directly_exits_through_main(tmp_path, monkeypatch):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["prog", "--cert", str(tmp_path / "absent.crt"), "--dry-run"])
    with pytest.raises(SystemExit) as info:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert info.value.code == 1


def test_a_real_run_without_root_is_refused_before_anything_is_written(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.undo()  # drop the autouse bypass so the real root check runs
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    cert = tmp_path / "root.crt"
    cert.write_text(PEM)
    out = tmp_path / "share" / "profile.mobileconfig"
    monkeypatch.setattr(sys, "argv", ["prog", "--cert", str(cert), "--output", str(out),
                                      "--owner", ME, "--group", MY_GROUP])  # fmt: skip
    assert mod.main() == 1
    assert "run as root" in capsys.readouterr().err
    assert not out.parent.exists()


def test_the_certificate_file_name_in_the_profile_is_the_source_files_name(tmp_path):
    cert = tmp_path / "my-root.pem"
    cert.write_text(PEM)
    code, out, copy = run(tmp_path, cert=cert)
    payload = plistlib.loads(out.read_bytes())["PayloadContent"][0]
    assert code == 0 and payload["PayloadCertificateFileName"] == "my-root.pem"


def test_staged_files_get_their_mode_even_under_a_restrictive_umask(tmp_path, me):
    target = tmp_path / "share" / "profile"
    old = os.umask(0o077)
    try:
        mod.write_file(target, b"x", 0o644, me, dry_run=False)
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644  # readable by the devices that fetch it

    source = tmp_path / "src.crt"
    source.write_bytes(b"cert")
    old = os.umask(0o077)
    try:
        mod.copy_file(source, tmp_path / "share" / "copy.crt", 0o644, me, dry_run=False)
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "share" / "copy.crt").stat().st_mode) == 0o644


def test_the_certificate_copy_keeps_the_original_timestamp(tmp_path, me):
    source = tmp_path / "src.crt"
    source.write_bytes(b"cert")
    os.utime(source, (1_000_000_000, 1_000_000_000))
    mod.copy_file(source, tmp_path / "share" / "copy.crt", 0o644, me, dry_run=False)
    assert int((tmp_path / "share" / "copy.crt").stat().st_mtime) == 1_000_000_000


def test_the_exporter_uses_the_shared_helpers_not_private_copies():
    from scripts import caddy_profile_common as common

    for name in ("SetupError", "Ownership", "fail", "log", "resolve_ownership",
                 "load_certificate_der", "stable_uuid"):  # fmt: skip
        assert getattr(mod, name) is getattr(common, name), name


def test_the_script_runs_when_executed_directly_as_the_install_docs_do(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run", "--cert", str(tmp_path / "absent.crt")],
        capture_output=True, text=True, cwd=tmp_path, timeout=60,
    )  # fmt: skip
    assert result.returncode == 1 and "error: certificate not found" in result.stderr
    assert "Traceback" not in result.stderr  # the sibling import works outside the package
