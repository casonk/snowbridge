#!/usr/bin/env python3
"""Generate an iPhone-installable configuration profile for Caddy's local root CA."""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import shutil
import sys
from pathlib import Path

try:  # imported as part of the package (tests) ...
    from scripts.caddy_profile_common import (
        Ownership,
        SetupError,
        fail,
        load_certificate_der,
        log,
        resolve_ownership,
        stable_uuid,
    )
except ModuleNotFoundError:  # ... or run directly: sudo python3 scripts/<name>.py
    from caddy_profile_common import (  # type: ignore[no-redef]
        Ownership,
        SetupError,
        fail,
        load_certificate_der,
        log,
        resolve_ownership,
        stable_uuid,
    )


DEFAULT_CERT_PATH = Path("/var/lib/snowbridge/caddy/data/caddy/pki/authorities/local/root.crt")
DEFAULT_SHARE_TMP = Path("/srv/snowbridge/share/tmp")
DEFAULT_PROFILE_OUTPUT = DEFAULT_SHARE_TMP / "snowbridge-caddy-local-root.mobileconfig"
DEFAULT_CERT_COPY_OUTPUT = DEFAULT_SHARE_TMP / "snowbridge-caddy-local-root.crt"
DEFAULT_OWNER = "snowbridge"
DEFAULT_GROUP = "snowbridge"
DEFAULT_IDENTIFIER = "local.snowbridge.caddy-root-ca"
DEFAULT_DISPLAY_NAME = "Snowbridge Caddy Local Root CA"
DEFAULT_ORGANIZATION = "snowbridge"


def require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        fail("run as root so the share tmp directory and staged profile can be owned by the snowbridge account")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an iPhone-installable .mobileconfig for Caddy's local root CA and stage it in the SMB share.",
    )
    parser.add_argument(
        "--cert",
        default=str(DEFAULT_CERT_PATH),
        help="Path to Caddy's root certificate. Default: /var/lib/snowbridge/caddy/data/caddy/pki/authorities/local/root.crt",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_PROFILE_OUTPUT),
        help="Output .mobileconfig path. Default: /srv/snowbridge/share/tmp/snowbridge-caddy-local-root.mobileconfig",
    )
    parser.add_argument(
        "--cert-copy-output",
        default=str(DEFAULT_CERT_COPY_OUTPUT),
        help="Optional raw certificate copy path. Default: /srv/snowbridge/share/tmp/snowbridge-caddy-local-root.crt",
    )
    parser.add_argument(
        "--skip-cert-copy",
        action="store_true",
        help="Do not also copy the raw root certificate next to the generated profile.",
    )
    parser.add_argument(
        "--owner",
        default=DEFAULT_OWNER,
        help="Owner for staged files and directories. Default: snowbridge",
    )
    parser.add_argument(
        "--group",
        default=DEFAULT_GROUP,
        help="Group for staged files and directories. Default: snowbridge",
    )
    parser.add_argument(
        "--profile-identifier",
        default=DEFAULT_IDENTIFIER,
        help="Top-level profile identifier. Default: local.snowbridge.caddy-root-ca",
    )
    parser.add_argument(
        "--profile-name",
        default=DEFAULT_DISPLAY_NAME,
        help="Human-readable profile name shown on iPhone. Default: Snowbridge Caddy Local Root CA",
    )
    parser.add_argument(
        "--organization",
        default=DEFAULT_ORGANIZATION,
        help="Profile organization string shown on iPhone. Default: snowbridge",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions that would run without changing the host.",
    )
    return parser.parse_args()


def ensure_parent_directory(path: Path, ownership: Ownership, dry_run: bool) -> None:
    parent = path.parent
    if dry_run:
        log(f"would ensure directory {parent} mode=2770 owner={ownership.owner_name}:{ownership.group_name}")
        return
    parent.mkdir(parents=True, exist_ok=True)
    os.chown(parent, ownership.uid, ownership.gid)
    os.chmod(parent, 0o2770)


def write_file(path: Path, content: bytes, mode: int, ownership: Ownership, dry_run: bool) -> None:
    ensure_parent_directory(path, ownership, dry_run)
    if dry_run:
        log(f"would write {path} mode={mode:o}")
        return
    path.write_bytes(content)
    os.chown(path, ownership.uid, ownership.gid)
    os.chmod(path, mode)


def copy_file(source: Path, target: Path, mode: int, ownership: Ownership, dry_run: bool) -> None:
    ensure_parent_directory(target, ownership, dry_run)
    if dry_run:
        log(f"would copy {source} -> {target} mode={mode:o}")
        return
    shutil.copy2(source, target)
    os.chown(target, ownership.uid, ownership.gid)
    os.chmod(target, mode)


def build_mobileconfig(
    cert_der: bytes,
    profile_identifier: str,
    profile_name: str,
    organization: str,
    certificate_file_name: str,
) -> bytes:
    digest = hashlib.sha256(cert_der).hexdigest()
    payload_identifier = f"{profile_identifier}.root"
    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": profile_identifier,
        "PayloadUUID": stable_uuid("snowbridge-profile", digest),
        "PayloadDisplayName": profile_name,
        "PayloadDescription": "Trust profile for Snowbridge's private Caddy HTTPS certificate authority.",
        "PayloadOrganization": organization,
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [
            {
                "PayloadType": "com.apple.security.root",
                "PayloadVersion": 1,
                "PayloadIdentifier": payload_identifier,
                "PayloadUUID": stable_uuid("snowbridge-payload", digest),
                "PayloadDisplayName": profile_name,
                "PayloadDescription": "Installs the local Snowbridge Caddy root CA so iPhone can trust private HTTPS access.",
                "PayloadCertificateFileName": certificate_file_name,
                "PayloadContent": cert_der,
            }
        ],
    }
    return plistlib.dumps(profile, fmt=plistlib.FMT_XML, sort_keys=False)


def summarize_install_steps(profile_path: Path) -> None:
    log("next iPhone steps:")
    log(f"  1. Open {profile_path.name} from the snowbridge SMB share in Files.")
    log("  2. Tap Allow if iPhone asks to download the profile.")
    log("  3. Open Settings, then tap Profile Downloaded.")
    log("  4. Install the profile.")
    log("  5. Go to Settings > General > About > Certificate Trust Settings.")
    log("  6. Enable full trust for the Snowbridge Caddy root certificate.")


def main() -> int:
    args = parse_args()
    cert_path = Path(args.cert).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    cert_copy_path = Path(args.cert_copy_output).expanduser().resolve()

    try:
        if not args.dry_run:
            require_root()
        ownership = resolve_ownership(args.owner, args.group)
        cert_der = load_certificate_der(cert_path)
        profile_bytes = build_mobileconfig(
            cert_der=cert_der,
            profile_identifier=args.profile_identifier,
            profile_name=args.profile_name,
            organization=args.organization,
            certificate_file_name=cert_path.name,
        )

        write_file(output_path, profile_bytes, 0o644, ownership, args.dry_run)
        log(f"{'would stage' if args.dry_run else 'staged'} {output_path}")

        if not args.skip_cert_copy:
            copy_file(cert_path, cert_copy_path, 0o644, ownership, args.dry_run)
            log(f"{'would stage' if args.dry_run else 'staged'} {cert_copy_path}")

        summarize_install_steps(output_path)
        return 0
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
