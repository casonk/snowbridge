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


def test_fail_raises_the_modules_own_error():
    with pytest.raises(mod.SetupError, match="boom"):
        mod.fail("boom")


def test_publishing_requires_root_unless_it_is_a_dry_run(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    with pytest.raises(mod.SetupError, match="run as root"):
        mod.require_root()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    mod.require_root()


def test_owner_and_group_must_exist():
    got = mod.resolve_ownership(ME, MY_GROUP)
    assert (got.uid, got.gid) == (os.getuid(), os.getgid())
    with pytest.raises(mod.SetupError, match="owner account not found: no-such-user-xyz"):
        mod.resolve_ownership("no-such-user-xyz", MY_GROUP)
    with pytest.raises(mod.SetupError, match="group not found: no-such-group-xyz"):
        mod.resolve_ownership(ME, "no-such-group-xyz")


def test_stable_ids_depend_on_the_certificate_and_look_like_uuids():
    a = mod.stable_uuid("label", "digest-a")
    assert a == mod.stable_uuid("label", "digest-a") == a.upper()
    assert a != mod.stable_uuid("label", "digest-b") and a != mod.stable_uuid("other", "digest-a")
    assert str(uuid.UUID(a)).upper() == a


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


def test_a_missing_certificate_is_an_error(tmp_path):
    with pytest.raises(mod.SetupError, match="certificate not found"):
        mod.load_certificate_der(tmp_path / "absent.crt")
    with pytest.raises(mod.SetupError, match="certificate not found"):
        mod.load_certificate_der(tmp_path)  # a directory is not a certificate


def test_a_pem_certificate_is_converted_to_der(tmp_path):
    path = tmp_path / "root.crt"
    path.write_text(PEM)
    assert mod.load_certificate_der(path) == DER


def test_a_der_certificate_is_used_as_is(tmp_path):
    path = tmp_path / "root.der"
    path.write_bytes(DER)
    assert mod.load_certificate_der(path) == DER


def test_a_pem_that_cannot_be_decoded_is_reported(tmp_path):
    bad = tmp_path / "bad.crt"
    bad.write_bytes(b"-----BEGIN CERTIFICATE-----\n\xff\xfe\n-----END CERTIFICATE-----\n")
    with pytest.raises(mod.SetupError, match="not ASCII-readable"):
        mod.load_certificate_der(bad)
    worse = tmp_path / "worse.crt"
    worse.write_text("-----BEGIN CERTIFICATE-----\n!!!notbase64!!!\n-----END CERTIFICATE-----\n")
    with pytest.raises(mod.SetupError, match="unable to parse PEM certificate"):
        mod.load_certificate_der(worse)


@pytest.mark.parametrize(
    "body",
    [
        PRIVATE_KEY_PEM,
        PRIVATE_KEY_PEM.replace("PRIVATE KEY", "RSA PRIVATE KEY"),
        PRIVATE_KEY_PEM.replace("PRIVATE KEY", "EC PRIVATE KEY"),
        PRIVATE_KEY_PEM.replace("PRIVATE KEY", "ENCRYPTED PRIVATE KEY"),
        PEM + PRIVATE_KEY_PEM,  # a certificate bundled with its key
    ],
)
def test_private_key_material_is_never_accepted_as_a_certificate(tmp_path, body):
    path = tmp_path / "root.key"
    path.write_text(body)
    with pytest.raises(mod.SetupError, match="private key"):
        mod.load_certificate_der(path)


@pytest.mark.parametrize(
    "key_der",
    [
        b"\x30\x82\x01\x0a\x02\x01\x00" + b"\x01" * 20,  # PKCS#8 / RSA: SEQUENCE { INTEGER ...
        b"\x30\x13\x02\x01\x00" + b"\x01" * 20,  # same with a short-form length
    ],
)
def test_a_binary_der_private_key_is_refused_too(tmp_path, key_der):
    path = tmp_path / "root.key.der"
    path.write_bytes(key_der)
    with pytest.raises(mod.SetupError, match="not a PEM or DER certificate"):
        mod.load_certificate_der(path)


def test_a_der_certificate_with_a_short_form_length_is_accepted(tmp_path):
    path = tmp_path / "tiny.der"
    path.write_bytes(b"\x30\x07\x30\x03\x02\x01\x02\x05\x00")
    assert mod.load_certificate_der(path).startswith(b"\x30\x07")


def test_a_truncated_der_header_is_refused(tmp_path):
    for data in (b"\x30", b"\x30\x82", b"\x30\x82\x01\x0a"):
        path = tmp_path / "short.der"
        path.write_bytes(data)
        with pytest.raises(mod.SetupError, match="not a PEM or DER certificate"):
            mod.load_certificate_der(path)


def test_something_that_is_not_a_certificate_at_all_is_refused(tmp_path):
    for name, data in (("notes.txt", b"just some text\n"), ("empty", b"")):
        path = tmp_path / name
        path.write_bytes(data)
        with pytest.raises(mod.SetupError, match="not a PEM or DER certificate"):
            mod.load_certificate_der(path)


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


def test_der_must_start_with_a_sequence_not_merely_contain_one(tmp_path):
    path = tmp_path / "set.der"
    path.write_bytes(b"\x31\x03\x30\x00\x00" + b"\x00" * 8)  # SET { SEQUENCE ... }, not a certificate
    with pytest.raises(mod.SetupError, match="not a PEM or DER certificate"):
        mod.load_certificate_der(path)


def test_a_pem_wrapper_around_something_that_is_not_a_certificate_is_refused(tmp_path):
    key_shaped = b"\x30\x82\x01\x0a\x02\x01\x00" + b"\x01" * 20  # SEQUENCE { INTEGER ...
    path = tmp_path / "labelled-certificate.pem"
    path.write_text(ssl.DER_cert_to_PEM_cert(key_shaped))  # labelled CERTIFICATE, but a key inside
    with pytest.raises(mod.SetupError, match="not a PEM or DER certificate"):
        mod.load_certificate_der(path)


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
