"""Helpers shared by the two Caddy profile exporters.

``export_caddy_root_profile.py`` and ``export_caddy_mtls_profile.py`` both read a CA certificate,
look up the account that should own staged files, and build stable profile identifiers. They are
run directly (``sudo python3 scripts/export_caddy_*.py``) and imported by the tests, so each loads
this module as ``scripts.caddy_profile_common`` when it is a package member and as a sibling
module when it is run as a script.
"""

from __future__ import annotations

import grp
import pwd
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn


class SetupError(RuntimeError):
    """Raised when profile generation cannot continue safely."""


@dataclass(frozen=True)
class Ownership:
    uid: int
    gid: int
    owner_name: str
    group_name: str


def log(message: str) -> None:
    print(message)


def fail(message: str) -> NoReturn:
    raise SetupError(message)


def resolve_ownership(owner_name: str, group_name: str) -> Ownership:
    try:
        owner = pwd.getpwnam(owner_name)
    except KeyError:
        fail(f"owner account not found: {owner_name}")
    try:
        group = grp.getgrnam(group_name)
    except KeyError:
        fail(f"group not found: {group_name}")
    return Ownership(uid=owner.pw_uid, gid=group.gr_gid, owner_name=owner_name, group_name=group_name)


def load_certificate_der(cert_path: Path) -> bytes:
    if not cert_path.is_file():
        fail(f"certificate not found: {cert_path}")

    raw = cert_path.read_bytes()
    # The profile and the raw copy are staged on a share other devices can read, so a CA private
    # key (for example root.key next to root.crt) must never be accepted by mistake.
    if b"PRIVATE KEY" in raw:
        fail(f"refusing to stage {cert_path}: it contains private key material, not a certificate")

    if b"-----BEGIN CERTIFICATE-----" not in raw:
        return _require_certificate_structure(raw, cert_path)

    try:
        pem_text = raw.decode("ascii")
    except UnicodeDecodeError:
        fail(f"certificate looks PEM-encoded but is not ASCII-readable: {cert_path}")

    try:
        der_bytes = ssl.PEM_cert_to_DER_cert(pem_text)
    except ValueError as exc:
        fail(f"unable to parse PEM certificate at {cert_path}: {exc}")

    return _require_certificate_structure(der_bytes, cert_path)


def _require_certificate_structure(der: bytes, source: Path) -> bytes:
    """Accept DER only if it is shaped like an X.509 certificate.

    A certificate is SEQUENCE { tbsCertificate SEQUENCE, ... }; a DER private key (PKCS#8, RSA)
    is SEQUENCE { INTEGER version, ... }, and arbitrary text is neither.
    """
    if len(der) >= 3 and der[0] == 0x30:
        offset = 2 + (der[1] & 0x7F if der[1] & 0x80 else 0)
        if offset < len(der) and der[offset] == 0x30:
            return der
    fail(f"not a PEM or DER certificate: {source}")


def stable_uuid(label: str, digest: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{label}:{digest}")).upper()
