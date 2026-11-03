"""The mTLS client-identity exporter: issuing a client certificate and staging it for a phone.

This script creates a certificate authority key and a client private key, wraps the client key in
a PKCS#12 file, and stages a profile on a share that other devices read. The tests pin where
secrets may and may not appear: the passphrase goes to ``openssl`` through the environment (never
the command line), every key and the passphrase file are owner-only, only the encrypted PKCS#12 is
staged (group-readable, never the bare key), a private key is never accepted in place of the
root certificate, and a re-run reuses what exists unless asked to rotate. ``openssl`` is replaced
by a fake that writes the files it would; ownership is the running user so no root is needed.
"""

from __future__ import annotations

import grp
import os
import plistlib
import pwd
import ssl
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from scripts import export_caddy_mtls_profile as mod

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_caddy_mtls_profile.py"
ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
DER = b"\x30\x82\x01\x0a\x30\x03\x02\x01\x02" + bytes(range(1, 11)) + b"\x00" * 10
PEM = ssl.DER_cert_to_PEM_cert(DER)
P12 = b"P12-BYTES-FROM-FAKE-OPENSSL"


def _pem(label: str, body: str) -> str:
    # Assembled at runtime so that no literal private-key block sits in the source.
    dashes = "-" * 5
    return f"{dashes}BEGIN {label}{dashes}\n{body}\n{dashes}END {label}{dashes}\n"


PRIVATE_KEY_PEM = _pem("PRIVATE KEY", "not a real key - test data only")


def flag(command, name):
    return command[command.index(name) + 1]


@pytest.fixture
def me():
    return mod.Ownership(os.getuid(), os.getgid(), ME, MY_GROUP)


@pytest.fixture(autouse=True)
def not_root(monkeypatch):
    monkeypatch.setattr(mod, "require_root", lambda: None)
    monkeypatch.setattr(mod, "command_exists", lambda name: True)


@pytest.fixture
def openssl(monkeypatch):
    """Replace run_command with a fake openssl that writes the files each command would."""
    state = SimpleNamespace(calls=[], fail_on=None)

    def fake(command, *, env=None):
        state.calls.append((list(command), env))
        if state.fail_on is not None and state.fail_on(command):
            raise mod.SetupError(f"command failed: {' '.join(command[:3])}: boom")
        head = command[:3]
        if head == ["openssl", "req", "-x509"]:
            Path(flag(command, "-keyout")).write_text("CA-KEY\n")
            Path(flag(command, "-out")).write_text(PEM)
        elif head == ["openssl", "req", "-new"]:
            Path(flag(command, "-keyout")).write_text("CLIENT-KEY\n")
            Path(flag(command, "-out")).write_text("CSR\n")
        elif head == ["openssl", "x509", "-req"]:
            Path(flag(command, "-out")).write_text(PEM)
        elif head == ["openssl", "pkcs12", "-export"]:
            Path(flag(command, "-out")).write_bytes(P12)
        else:
            raise AssertionError(command)

    monkeypatch.setattr(mod, "run_command", fake)
    state.names = lambda: [" ".join(c[1:3]) for c, _ in state.calls]
    return state


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "pki"
    root.mkdir()
    server_root = root / "server-root.crt"
    server_root.write_text(PEM)
    return SimpleNamespace(
        tmp=tmp_path,
        server_root=server_root,
        ca_cert=root / "client-ca.crt",
        ca_key=root / "client-ca.key",
        issued=root / "issued",
        profile=tmp_path / "share" / "profile.mobileconfig",
        p12=tmp_path / "share" / "identity.p12",
    )


def argv(world, *extra, device="iPhone 15"):
    return [
        "prog", "--device-name", device, "--server-root-cert", str(world.server_root),
        "--client-ca-cert", str(world.ca_cert), "--client-ca-key", str(world.ca_key),
        "--issued-dir", str(world.issued), "--output", str(world.profile),
        "--p12-output", str(world.p12), "--owner", ME, "--group", MY_GROUP, *extra,
    ]  # fmt: skip


def run_main(world, *extra, **kw):
    with mock.patch.object(sys, "argv", argv(world, *extra, **kw)):
        return mod.main()


def identity_files(world, slug="iphone-15"):
    prefix = world.issued / f"snowbridge-caddy-mtls-{slug}"
    return SimpleNamespace(
        cert=prefix.with_suffix(".crt"), key=prefix.with_suffix(".key"),
        p12=prefix.with_suffix(".p12"), passphrase=prefix.with_suffix(".passphrase"),
    )  # fmt: skip


# --- small helpers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [("iPhone 15", "iphone-15"), ("  My   Phone!! ", "my-phone"), ("a_b.c", "a-b-c"), ("X", "x")],
)
def test_device_names_become_safe_file_name_slugs(name, slug):
    assert mod.slugify_device_name(name) == slug


@pytest.mark.parametrize("name", ["", "   ", "!!!", "../.."])
def test_a_device_name_needs_an_alphanumeric_character(name):
    with pytest.raises(mod.SetupError, match="at least one alphanumeric"):
        mod.slugify_device_name(name)


def test_a_slug_can_never_contain_a_path_separator():
    assert "/" not in mod.slugify_device_name("../../etc/passwd") and ".." not in mod.slugify_device_name("../../etc/passwd")


def test_publishing_requires_root(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    with pytest.raises(mod.SetupError, match="run as root"):
        mod.require_root()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    mod.require_root()


# --- running openssl and installing it ----------------------------------------------------


def test_a_failing_command_reports_its_stderr_then_stdout_then_the_exception(monkeypatch):
    def failing(stdout, stderr):
        def run(command, **kwargs):
            raise subprocess.CalledProcessError(1, command, output=stdout, stderr=stderr)

        return run

    monkeypatch.setattr(mod.subprocess, "run", failing("out text", " err text "))
    with pytest.raises(mod.SetupError, match="command failed: openssl x: err text"):
        mod.run_command(["openssl", "x"])
    monkeypatch.setattr(mod.subprocess, "run", failing(" out text ", ""))
    with pytest.raises(mod.SetupError, match="openssl x: out text"):
        mod.run_command(["openssl", "x"])
    monkeypatch.setattr(mod.subprocess, "run", failing("", ""))
    with pytest.raises(mod.SetupError, match="command failed: openssl x: "):
        mod.run_command(["openssl", "x"])


def test_run_command_captures_output_and_passes_the_environment(monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(mod.subprocess, "run", run)
    mod.run_command(["openssl", "version"], env={"A": "1"})
    assert seen["check"] is True and seen["text"] is True and seen["env"] == {"A": "1"}
    assert seen["stdout"] == subprocess.PIPE and seen["stderr"] == subprocess.PIPE


def test_the_first_supported_package_manager_is_chosen(monkeypatch):
    present = {"yum"}
    monkeypatch.setattr(mod, "command_exists", lambda name: name in present)
    assert mod.find_package_manager() == "yum"
    present.add("apt-get")
    assert mod.find_package_manager() == "apt-get"
    present.add("dnf")
    assert mod.find_package_manager() == "dnf"
    present.clear()
    with pytest.raises(mod.SetupError, match="no supported package manager"):
        mod.find_package_manager()


@pytest.mark.parametrize(
    ("manager", "expected"),
    [
        ("dnf", [["dnf", "install", "-y", "openssl"]]),
        ("yum", [["yum", "install", "-y", "openssl"]]),
        ("apt-get", [["apt-get", "update"], ["apt-get", "install", "-y", "openssl"]]),
    ],
)
def test_packages_are_installed_with_the_detected_manager(monkeypatch, manager, expected):
    calls = []
    monkeypatch.setattr(mod, "find_package_manager", lambda: manager)
    monkeypatch.setattr(mod.subprocess, "run", lambda command, **kw: calls.append((command, kw)))
    mod.install_os_packages("openssl")
    assert [c for c, _ in calls] == expected
    if manager == "apt-get":
        assert calls[1][1]["env"]["DEBIAN_FRONTEND"] == "noninteractive"
    assert all(kw["check"] is True for _, kw in calls)


def test_an_unsupported_manager_is_an_error(monkeypatch):
    monkeypatch.setattr(mod, "find_package_manager", lambda: "pacman")
    with pytest.raises(mod.SetupError, match="unsupported package manager"):
        mod.install_os_packages("openssl")


def test_openssl_is_installed_only_when_missing(monkeypatch):
    installs = []
    monkeypatch.setattr(mod, "install_os_packages", lambda *p: installs.append(p))
    mod.ensure_openssl()  # command_exists is True from the fixture
    assert installs == []

    present = {"openssl": False}
    monkeypatch.setattr(mod, "command_exists", lambda name: present["openssl"])

    def install(*packages):
        installs.append(packages)
        present["openssl"] = True

    monkeypatch.setattr(mod, "install_os_packages", install)
    mod.ensure_openssl()
    assert installs == [("openssl",)]

    present["openssl"] = False
    monkeypatch.setattr(mod, "install_os_packages", lambda *p: None)
    with pytest.raises(mod.SetupError, match="still missing after package install"):
        mod.ensure_openssl()


# --- staging files ------------------------------------------------------------------------


def test_a_dry_run_changes_nothing_on_disk(tmp_path, me, capsys):
    target = tmp_path / "a" / "b" / "file"
    mod.ensure_directory(target.parent, 0o750, me, dry_run=True)
    mod.write_file(target, b"x", 0o644, me, dry_run=True)
    mod.copy_file(tmp_path / "src", target, 0o640, me, dry_run=True)
    assert not (tmp_path / "a").exists()
    out = capsys.readouterr().out
    assert "mode=750 owner=" in out and f"would write {target} mode=644" in out
    assert "would copy" in out and "mode=640" in out


def test_directories_and_files_get_their_modes_and_owner(tmp_path, me):
    target = tmp_path / "share" / "profile"
    mod.write_file(target, b"data", 0o644, me, dry_run=False)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o2770  # shared with the group, setgid
    source = tmp_path / "src.p12"
    source.write_bytes(b"p12")
    mod.copy_file(source, tmp_path / "share" / "copy.p12", 0o640, me, dry_run=False)
    assert stat.S_IMODE((tmp_path / "share" / "copy.p12").stat().st_mode) == 0o640
    private = tmp_path / "private" / "file"
    mod.write_file(private, b"x", 0o600, None, dry_run=False)  # no owner given: a private directory
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o750


def test_modes_hold_even_under_a_restrictive_umask(tmp_path, me):
    old = os.umask(0o077)
    try:
        mod.write_file(tmp_path / "s" / "profile", b"x", 0o644, me, dry_run=False)
        source = tmp_path / "src"
        source.write_bytes(b"x")
        mod.copy_file(source, tmp_path / "s" / "copy", 0o640, me, dry_run=False)
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "s" / "profile").stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "s" / "copy").stat().st_mode) == 0o640


# --- reading the root certificate ---------------------------------------------------------


# --- paths --------------------------------------------------------------------------------


def test_identity_paths_follow_the_device_slug(tmp_path):
    paths = mod.build_identity_paths("iPhone 15", tmp_path / "issued", None, None)
    assert paths.slug == "iphone-15"
    assert paths.cert_path == tmp_path / "issued" / "snowbridge-caddy-mtls-iphone-15.crt"
    assert paths.key_path.suffix == ".key" and paths.p12_path.suffix == ".p12"
    assert paths.passphrase_path.suffix == ".passphrase"
    assert paths.serial_path == tmp_path / "issued" / "client-ca.srl"
    assert paths.staged_profile_path == (mod.DEFAULT_SHARE_TMP / "snowbridge-caddy-mtls-iphone-15.mobileconfig").resolve()
    assert paths.staged_p12_path == (mod.DEFAULT_SHARE_TMP / "snowbridge-caddy-mtls-iphone-15.p12").resolve()


def test_explicit_output_paths_win(tmp_path):
    paths = mod.build_identity_paths("a", tmp_path / "i", str(tmp_path / "x.mobileconfig"), str(tmp_path / "y.p12"))
    assert paths.staged_profile_path == (tmp_path / "x.mobileconfig").resolve()
    assert paths.staged_p12_path == (tmp_path / "y.p12").resolve()


# --- the client CA ------------------------------------------------------------------------


def test_an_existing_client_ca_is_left_alone(world, openssl):
    world.ca_cert.write_text(PEM)
    world.ca_key.write_text("KEY")
    mod.ensure_client_ca(world.ca_cert, world.ca_key, dry_run=False)
    assert openssl.calls == []


@pytest.mark.parametrize("which", ["cert", "key"])
def test_half_a_client_ca_is_never_silently_replaced(world, openssl, which):
    (world.ca_cert if which == "cert" else world.ca_key).write_text("x")
    with pytest.raises(mod.SetupError, match="incomplete mTLS client CA state"):
        mod.ensure_client_ca(world.ca_cert, world.ca_key, dry_run=False)
    assert openssl.calls == []


def test_a_dry_run_does_not_create_the_client_ca(world, openssl, capsys):
    mod.ensure_client_ca(world.ca_cert, world.ca_key, dry_run=True)
    assert openssl.calls == [] and not world.ca_cert.exists()
    assert "would generate mTLS client CA" in capsys.readouterr().out


def test_a_new_client_ca_is_a_long_lived_unencrypted_ca_with_an_owner_only_key(world, openssl, capsys):
    mod.ensure_client_ca(world.ca_cert, world.ca_key, dry_run=False)
    (command, env), = openssl.calls
    assert command[:3] == ["openssl", "req", "-x509"] and "-nodes" in command
    assert flag(command, "-keyout") == str(world.ca_key) and flag(command, "-out") == str(world.ca_cert)
    assert flag(command, "-days") == "3650" and flag(command, "-newkey") == "rsa:4096"
    assert stat.S_IMODE(world.ca_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(world.ca_cert.stat().st_mode) == 0o644
    config = Path(flag(command, "-config"))
    assert not config.exists()  # the temporary request config is removed
    assert "generated mTLS client CA" in capsys.readouterr().out


def test_the_temporary_config_is_removed_even_when_openssl_fails(world, openssl):
    openssl.fail_on = lambda command: True
    with pytest.raises(mod.SetupError):
        mod.ensure_client_ca(world.ca_cert, world.ca_key, dry_run=False)
    config = Path(flag(openssl.calls[0][0], "-config"))
    assert not config.exists()


# --- the identity passphrase --------------------------------------------------------------


def test_an_explicit_passphrase_is_stored_owner_only_and_must_not_be_empty(world):
    path = identity_files(world).passphrase
    with pytest.raises(mod.SetupError, match="cannot be empty"):
        mod.load_or_create_passphrase(path, "", rotate=False, dry_run=False)
    assert mod.load_or_create_passphrase(path, "chosen", rotate=False, dry_run=False) == "chosen"
    assert path.read_text() == "chosen\n" and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_generated_passphrase_is_long_url_safe_and_stored_owner_only(world):
    path = identity_files(world).passphrase
    first = mod.load_or_create_passphrase(path, None, rotate=False, dry_run=False)
    assert len(first) >= 30 and set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text() == first + "\n"
    assert mod.load_or_create_passphrase(path, None, rotate=False, dry_run=False) == first  # reused
    rotated = mod.load_or_create_passphrase(path, None, rotate=True, dry_run=False)
    assert rotated != first and path.read_text() == rotated + "\n"


def test_an_empty_stored_passphrase_is_an_error_not_a_blank_secret(world):
    path = identity_files(world).passphrase
    path.parent.mkdir(parents=True)
    path.write_text("  \n")
    with pytest.raises(mod.SetupError, match="stored passphrase file is empty"):
        mod.load_or_create_passphrase(path, None, rotate=False, dry_run=False)


def test_a_dry_run_never_writes_a_passphrase(world):
    path = identity_files(world).passphrase
    assert mod.load_or_create_passphrase(path, None, rotate=False, dry_run=True)
    assert mod.load_or_create_passphrase(path, "explicit", rotate=False, dry_run=True) == "explicit"
    assert not path.exists() and not path.parent.exists()


# --- issuing the client identity ----------------------------------------------------------


def issue(world, **kw):
    paths = mod.build_identity_paths("iPhone 15", world.issued, str(world.profile), str(world.p12))
    world.ca_cert.write_text(PEM)
    world.ca_key.write_text("KEY")
    args = dict(client_ca_cert=world.ca_cert, client_ca_key=world.ca_key, identity=paths,
                device_name="iPhone 15", passphrase="s3cret-pass", rotate=False, dry_run=False)  # fmt: skip
    args.update(kw)
    mod.ensure_client_identity(**args)
    return paths


def test_a_new_identity_is_issued_in_three_steps_with_owner_only_files(world, openssl):
    paths = issue(world)
    assert openssl.names() == ["req -new", "x509 -req", "pkcs12 -export"]
    assert (paths.key_path.read_text(), paths.p12_path.read_bytes()) == ("CLIENT-KEY\n", P12)
    assert stat.S_IMODE(paths.key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.p12_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.cert_path.stat().st_mode) == 0o644
    assert not paths.cert_path.with_suffix(".csr").exists()  # the signing request is removed


def test_the_client_certificate_is_signed_by_the_ca_for_client_auth_only(world, openssl):
    paths = issue(world)
    sign = openssl.calls[1][0]
    assert flag(sign, "-CA") == str(world.ca_cert) and flag(sign, "-CAkey") == str(world.ca_key)
    assert flag(sign, "-CAserial") == str(paths.serial_path) and "-CAcreateserial" in sign
    assert flag(sign, "-days") == "825" and flag(sign, "-extensions") == "v3_client"
    request = openssl.calls[0][0]
    assert flag(request, "-subj") == "/CN=Snowbridge iPhone 15 Client/O=snowbridge"
    assert "-nodes" in request and flag(request, "-newkey") == "rsa:2048"


def test_the_extension_file_restricts_the_certificate_to_client_authentication(world, openssl):
    captured = {}
    real = openssl

    def spying(command, *, env=None):
        if command[:3] == ["openssl", "x509", "-req"]:
            captured["ext"] = Path(flag(command, "-extfile")).read_text()
        return real_run(command, env=env)

    real_run = mod.run_command
    with mock.patch.object(mod, "run_command", spying):
        issue(world)
    assert "basicConstraints = critical,CA:FALSE" in captured["ext"]
    assert "extendedKeyUsage = clientAuth" in captured["ext"]
    assert "keyUsage = critical,digitalSignature,keyEncipherment" in captured["ext"]


def test_the_passphrase_reaches_openssl_through_the_environment_never_the_command_line(world, openssl):
    issue(world, passphrase="very-secret-phrase")
    for command, _ in openssl.calls:
        assert "very-secret-phrase" not in " ".join(command)
    pkcs12, env = openssl.calls[2]
    assert flag(pkcs12, "-passout") == "env:SNOWBRIDGE_P12_PASS"
    assert env["SNOWBRIDGE_P12_PASS"] == "very-secret-phrase"
    assert flag(pkcs12, "-name") == "Snowbridge iPhone 15 Client"
    assert flag(pkcs12, "-certfile") == str(world.ca_cert)


def test_a_complete_existing_identity_is_reused(world, openssl):
    paths = issue(world)
    openssl.calls.clear()
    paths.passphrase_path.parent.mkdir(parents=True, exist_ok=True)
    paths.passphrase_path.write_text("s3cret-pass\n")
    issue(world)
    assert openssl.calls == []


def test_a_partial_identity_is_refused_unless_rotating(world, openssl):
    paths = mod.build_identity_paths("iPhone 15", world.issued, None, None)
    world.issued.mkdir(parents=True)
    paths.cert_path.write_text("CERT")
    with pytest.raises(mod.SetupError, match="existing client identity state is incomplete"):
        issue(world)
    assert openssl.calls == []
    issue(world, rotate=True)  # --rotate repairs it
    assert openssl.names() == ["req -new", "x509 -req", "pkcs12 -export"]


def test_complete_files_without_a_passphrase_count_as_incomplete(world, openssl):
    paths = issue(world)
    openssl.calls.clear()
    with pytest.raises(mod.SetupError, match="incomplete"):
        issue(world)
    assert paths.passphrase_path.exists() is False


def test_a_stored_passphrase_alone_is_a_valid_start_for_a_new_device(world, openssl):
    paths = mod.build_identity_paths("iPhone 15", world.issued, None, None)
    paths.passphrase_path.parent.mkdir(parents=True)
    paths.passphrase_path.write_text("s3cret-pass\n")
    issue(world)  # does not raise: it is the normal bootstrap order
    assert paths.p12_path.exists()


def test_rotating_replaces_the_old_files(world, openssl):
    paths = issue(world)
    paths.key_path.write_text("OLD-KEY")
    paths.passphrase_path.parent.mkdir(parents=True, exist_ok=True)
    paths.passphrase_path.write_text("old\n")
    issue(world, rotate=True)
    assert paths.key_path.read_text() == "CLIENT-KEY\n"
    assert openssl.names().count("req -new") == 2


def test_a_dry_run_issues_nothing(world, openssl, capsys):
    issue(world, dry_run=True)
    assert openssl.calls == []
    assert "would issue a fresh mTLS client identity for iPhone 15" in capsys.readouterr().out


def test_a_failed_step_still_removes_the_temporary_files(world, openssl):
    openssl.fail_on = lambda command: command[1:3] == ["x509", "-req"]
    with pytest.raises(mod.SetupError):
        issue(world)
    request = openssl.calls[0][0]
    assert not Path(flag(request, "-out")).exists()  # the signing request is gone
    sign = openssl.calls[1][0]
    assert not Path(flag(sign, "-extfile")).exists()


# --- the profile --------------------------------------------------------------------------


def profile_for(root=DER, p12=P12, **kw):
    args = dict(root_cert_der=root, p12_bytes=p12, profile_identifier="local.example.mtls.iphone",
                profile_name="Example", organization="Org", device_name="iPhone",
                p12_file_name="id.p12", root_cert_file_name="root.crt")  # fmt: skip
    args.update(kw)
    return plistlib.loads(mod.build_mobileconfig(**args))


def test_the_profile_carries_the_root_and_the_identity_with_stable_distinct_ids():
    profile = profile_for()
    root, identity = profile["PayloadContent"]
    assert root["PayloadType"] == "com.apple.security.root" and root["PayloadContent"] == DER
    assert identity["PayloadType"] == "com.apple.security.pkcs12" and identity["PayloadContent"] == P12
    assert root["PayloadIdentifier"] == "local.example.mtls.iphone.root"
    assert identity["PayloadIdentifier"] == "local.example.mtls.iphone.identity"
    assert root["PayloadCertificateFileName"] == "root.crt" and identity["PayloadCertificateFileName"] == "id.p12"
    assert len({profile["PayloadUUID"], root["PayloadUUID"], identity["PayloadUUID"]}) == 3
    assert profile["PayloadRemovalDisallowed"] is False
    assert "iPhone" in profile["PayloadDescription"]
    assert profile_for()["PayloadUUID"] == profile["PayloadUUID"]
    assert profile_for(p12=P12 + b"x")["PayloadUUID"] != profile["PayloadUUID"]
    assert profile_for(root=DER + b"x")["PayloadUUID"] != profile["PayloadUUID"]


def test_the_install_steps_name_the_staged_files(capsys):
    mod.summarize_install_steps(Path("/s/p.mobileconfig"), Path("/s/p.p12"), Path("/i/p.passphrase"), "iPhone")
    out = capsys.readouterr().out
    assert "p.mobileconfig" in out and "fallback artifact: /s/p.p12" in out
    assert "stored local passphrase metadata: /i/p.passphrase" in out and "device label: iPhone" in out


# --- the command --------------------------------------------------------------------------


def test_a_full_run_issues_and_stages_everything(world, openssl, capsys):
    code = run_main(world, "--identity-passphrase", "chosen-pass")
    out = capsys.readouterr().out
    files = identity_files(world)
    assert code == 0
    assert openssl.names() == ["req -x509", "req -new", "x509 -req", "pkcs12 -export"]
    profile = plistlib.loads(world.profile.read_bytes())
    root, identity = profile["PayloadContent"]
    assert root["PayloadContent"] == DER and identity["PayloadContent"] == P12
    assert world.p12.read_bytes() == P12
    assert stat.S_IMODE(world.profile.stat().st_mode) == 0o644
    assert stat.S_IMODE(world.p12.stat().st_mode) == 0o640  # group-readable, encrypted
    assert files.passphrase.read_text() == "chosen-pass\n"
    assert out.count("identity import password:") == 1 and "  chosen-pass" in out
    assert f"staged {world.profile}" in out and f"staged {world.p12}" in out


def test_only_the_encrypted_identity_is_staged_never_a_bare_key(world, openssl):
    run_main(world)
    staged = [world.profile, world.p12]
    for path in staged:
        data = path.read_bytes()
        assert b"CLIENT-KEY" not in data and b"CA-KEY" not in data
    assert b"CLIENT-KEY" not in world.profile.read_bytes()
    assert list(world.p12.parent.iterdir()) and {p.name for p in world.p12.parent.iterdir()} == {
        world.profile.name, world.p12.name
    }


def test_a_second_run_reuses_the_identity_and_the_passphrase(world, openssl, capsys):
    run_main(world)
    first_pass = identity_files(world).passphrase.read_text()
    calls_after_first = len(openssl.calls)
    capsys.readouterr()
    assert run_main(world) == 0
    assert len(openssl.calls) == calls_after_first  # nothing re-issued
    assert identity_files(world).passphrase.read_text() == first_pass
    assert first_pass.strip() in capsys.readouterr().out


def test_rotating_issues_a_new_identity_and_passphrase(world, openssl):
    run_main(world)
    before = identity_files(world).passphrase.read_text()
    assert run_main(world, "--rotate") == 0
    assert identity_files(world).passphrase.read_text() != before
    assert openssl.names().count("req -new") == 2


def test_a_dry_run_shows_the_plan_and_changes_nothing(world, openssl, capsys):
    assert run_main(world, "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"would stage {world.profile}" in out and f"would stage {world.p12}" in out
    assert "identity import password:" in out
    assert openssl.calls == [] and not world.profile.exists() and not world.issued.exists()
    assert not world.ca_cert.exists()


def test_a_missing_server_root_certificate_is_a_clear_error(world, openssl, capsys):
    world.server_root.unlink()
    assert run_main(world) == 1
    err = capsys.readouterr().err
    assert "server root certificate not found" in err and "Start the private Caddy stack" in err
    assert not world.profile.exists()


def test_a_private_key_given_as_the_root_certificate_publishes_nothing(world, openssl, capsys):
    world.server_root.write_text(PRIVATE_KEY_PEM)
    assert run_main(world) == 1
    assert "private key" in capsys.readouterr().err
    assert not world.profile.exists() and not world.p12.exists()


def test_a_real_run_without_root_is_refused_before_anything_is_created(world, openssl, monkeypatch, capsys):
    monkeypatch.undo()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(mod, "command_exists", lambda name: True)
    assert run_main(world) == 1
    assert "run as root" in capsys.readouterr().err
    assert openssl.calls == []
    assert not world.issued.exists() and not world.ca_key.exists()


@pytest.mark.parametrize("device", ["", "!!!"])
def test_a_bad_device_name_is_a_one_line_error_not_a_traceback(world, openssl, capsys, device):
    assert run_main(world, device=device) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: device name must contain at least one alphanumeric") and "Traceback" not in err


def test_an_unknown_owner_is_a_one_line_error_not_a_traceback(world, openssl, capsys):
    with mock.patch.object(sys, "argv", argv(world)[:-4] + ["--owner", "no-such-user-xyz", "--group", MY_GROUP]):
        assert mod.main() == 1
    err = capsys.readouterr().err
    assert err.startswith("error: owner account not found") and "Traceback" not in err
    assert not world.issued.exists()


def test_the_profile_name_and_identifier_follow_the_device(world, openssl):
    run_main(world, "--profile-name", "Custom Name", "--organization", "Acme",
             "--profile-identifier-prefix", "local.acme.mtls")  # fmt: skip
    profile = plistlib.loads(world.profile.read_bytes())
    assert profile["PayloadDisplayName"] == "Custom Name" and profile["PayloadOrganization"] == "Acme"
    assert profile["PayloadIdentifier"] == "local.acme.mtls.iphone-15"
    run_main(world, "--rotate")
    default = plistlib.loads(world.profile.read_bytes())
    assert default["PayloadDisplayName"] == "Snowbridge Caddy mTLS (iPhone 15)"


def test_running_the_script_directly_exits_through_main(world, monkeypatch):
    import runpy

    world.server_root.unlink()
    monkeypatch.setattr(sys, "argv", argv(world, "--dry-run"))
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(SystemExit) as info:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert info.value.code == 0


def test_a_rotation_that_fails_halfway_does_not_leave_the_old_certificate_next_to_a_new_key(world, openssl):
    paths = issue(world)
    openssl.calls.clear()
    openssl.fail_on = lambda command: command[1:3] == ["x509", "-req"]
    with pytest.raises(mod.SetupError):
        issue(world, rotate=True)
    assert not paths.cert_path.exists() and not paths.p12_path.exists()  # old pair is gone, not mismatched


def test_the_exporter_uses_the_shared_helpers_not_private_copies():
    from scripts import caddy_profile_common as common

    for name in ("SetupError", "Ownership", "fail", "log", "resolve_ownership",
                 "load_certificate_der", "stable_uuid"):  # fmt: skip
        assert getattr(mod, name) is getattr(common, name), name


def test_the_script_runs_when_executed_directly_as_the_install_docs_do(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--device-name", "!!!"],
        capture_output=True, text=True, cwd=tmp_path, timeout=60,
    )  # fmt: skip
    assert result.returncode == 1 and "error: device name must contain" in result.stderr
    assert "Traceback" not in result.stderr  # the sibling import works outside the package
