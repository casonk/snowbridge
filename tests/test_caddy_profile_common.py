"""The helpers the two Caddy profile exporters share: certificates, owners and identifiers.

Both exporters stage a CA certificate on a share that other devices read, so the one thing that
must never happen is a CA *private key* being accepted as the certificate. These are the only
tests of that logic (the exporters' own tests check that they use it and refuse a key end to end).
"""

from __future__ import annotations

import grp
import os
import pwd
import ssl
import uuid
from pathlib import Path

import pytest

from scripts import caddy_profile_common as common

# Not a real certificate, but shaped like one: SEQUENCE { SEQUENCE { ... } ... }.
DER = b"\x30\x82\x01\x0a\x30\x03\x02\x01\x02" + bytes(range(1, 11)) + b"\x00" * 10
PEM = ssl.DER_cert_to_PEM_cert(DER)
ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


def _pem(label: str, body: str) -> str:
    # Assembled at runtime so that no literal private-key block sits in the source.
    dashes = "-" * 5
    return f"{dashes}BEGIN {label}{dashes}\n{body}\n{dashes}END {label}{dashes}\n"


PRIVATE_KEY_PEM = _pem("PRIVATE KEY", "not a real key - test data only")


# --- small helpers ------------------------------------------------------------------------


def test_fail_raises_the_shared_error_and_log_prints(capsys):
    with pytest.raises(common.SetupError, match="boom"):
        common.fail("boom")
    common.log("hello")
    assert capsys.readouterr().out == "hello\n"
    assert issubclass(common.SetupError, RuntimeError)


def test_owner_and_group_must_exist():
    got = common.resolve_ownership(ME, MY_GROUP)
    assert (got.uid, got.gid, got.owner_name, got.group_name) == (os.getuid(), os.getgid(), ME, MY_GROUP)
    with pytest.raises(common.SetupError, match="owner account not found: no-such-user-xyz"):
        common.resolve_ownership("no-such-user-xyz", MY_GROUP)
    with pytest.raises(common.SetupError, match="group not found: no-such-group-xyz"):
        common.resolve_ownership(ME, "no-such-group-xyz")


def test_ownership_is_immutable():
    owner = common.Ownership(1, 2, "a", "b")
    with pytest.raises(AttributeError):
        owner.uid = 5  # type: ignore[misc]


def test_stable_ids_depend_on_both_inputs_and_look_like_uuids():
    a = common.stable_uuid("label", "digest-a")
    assert a == common.stable_uuid("label", "digest-a") == a.upper()
    assert a != common.stable_uuid("label", "digest-b") and a != common.stable_uuid("other", "digest-a")
    assert str(uuid.UUID(a)).upper() == a


# --- reading the certificate --------------------------------------------------------------


def test_a_missing_certificate_is_an_error(tmp_path):
    with pytest.raises(common.SetupError, match="certificate not found"):
        common.load_certificate_der(tmp_path / "absent.crt")
    with pytest.raises(common.SetupError, match="certificate not found"):
        common.load_certificate_der(tmp_path)  # a directory is not a certificate


def test_a_pem_certificate_is_converted_to_der_and_der_is_used_as_is(tmp_path):
    pem = tmp_path / "root.crt"
    pem.write_text(PEM)
    der = tmp_path / "root.der"
    der.write_bytes(DER)
    assert common.load_certificate_der(pem) == DER
    assert common.load_certificate_der(der) == DER


def test_a_pem_that_cannot_be_decoded_is_reported(tmp_path):
    bad = tmp_path / "bad.crt"
    bad.write_bytes(b"-----BEGIN CERTIFICATE-----\n\xff\xfe\n-----END CERTIFICATE-----\n")
    with pytest.raises(common.SetupError, match="not ASCII-readable"):
        common.load_certificate_der(bad)
    worse = tmp_path / "worse.crt"
    worse.write_text("-----BEGIN CERTIFICATE-----\n!!!notbase64!!!\n-----END CERTIFICATE-----\n")
    with pytest.raises(common.SetupError, match="unable to parse PEM certificate"):
        common.load_certificate_der(worse)
    empty = tmp_path / "empty.crt"
    empty.write_text("-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n")
    with pytest.raises(common.SetupError, match="not a PEM or DER certificate"):
        common.load_certificate_der(empty)


@pytest.mark.parametrize(
    "body",
    [
        PRIVATE_KEY_PEM,
        PRIVATE_KEY_PEM.replace("PRIVATE KEY", "RSA PRIVATE KEY"),
        PRIVATE_KEY_PEM.replace("PRIVATE KEY", "EC PRIVATE KEY"),
        PRIVATE_KEY_PEM.replace("PRIVATE KEY", "ENCRYPTED PRIVATE KEY"),
        PRIVATE_KEY_PEM.replace("PRIVATE KEY", "OPENSSH PRIVATE KEY"),
        PEM + PRIVATE_KEY_PEM,  # a certificate bundled with its key
    ],
)
def test_private_key_material_is_never_accepted_as_a_certificate(tmp_path, body):
    path = tmp_path / "root.key"
    path.write_text(body)
    with pytest.raises(common.SetupError, match="private key"):
        common.load_certificate_der(path)


@pytest.mark.parametrize(
    "data",
    [
        b"\x30\x82\x01\x0a\x02\x01\x00" + b"\x01" * 20,  # PKCS#8 / RSA: SEQUENCE { INTEGER ...
        b"\x30\x13\x02\x01\x00" + b"\x01" * 20,  # the same with a short-form length
        b"\x31\x03\x30\x00\x00" + b"\x00" * 8,  # SET { SEQUENCE ... }, not a certificate
        b"\x30\x82",  # truncated headers
        b"\x30\x82\x01\x0a",
        b"\x30",
        b"just some text\n",
        b"",
    ],
)
def test_der_that_is_not_shaped_like_a_certificate_is_refused(tmp_path, data):
    path = tmp_path / "thing.der"
    path.write_bytes(data)
    with pytest.raises(common.SetupError, match="not a PEM or DER certificate"):
        common.load_certificate_der(path)


def test_a_key_hidden_inside_a_pem_certificate_wrapper_is_refused(tmp_path):
    path = tmp_path / "labelled-certificate.pem"
    path.write_text(ssl.DER_cert_to_PEM_cert(b"\x30\x82\x01\x0a\x02\x01\x00" + b"\x01" * 20))
    with pytest.raises(common.SetupError, match="not a PEM or DER certificate"):
        common.load_certificate_der(path)


def test_a_der_certificate_with_a_short_form_length_is_accepted(tmp_path):
    path = tmp_path / "tiny.der"
    path.write_bytes(b"\x30\x07\x30\x03\x02\x01\x02\x05\x00")
    assert common.load_certificate_der(path).startswith(b"\x30\x07")


def test_the_error_names_the_file_that_was_refused(tmp_path):
    path = tmp_path / "ca.key"
    path.write_text(PRIVATE_KEY_PEM)
    with pytest.raises(common.SetupError) as info:
        common.load_certificate_der(path)
    assert str(path) in str(info.value) and "refusing" in str(info.value)
    assert isinstance(path, Path)
