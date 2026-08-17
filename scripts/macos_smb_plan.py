#!/usr/bin/env python3
"""Validate and render a fail-closed native macOS SMB activation plan.

This tool never enables File Sharing, edits a share point, or loads PF rules.
It produces owner-only, ignored review artifacts after proving that the local
configuration and the observed host state meet the narrow WireGuard-only
contract. Live activation remains intentionally unsupported until the repo has
an atomic, tested rollback for both macOS share-point state and PF state.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import pwd
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config/macos/air-smb.local.toml"
EXAMPLE_CONFIG = REPO_ROOT / "config/macos/air-smb.example.toml"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts/macos-air-smb"
PF_ANCHOR_NAME = "com.apple/snowbridge"
MAX_CONFIG_BYTES = 128 * 1024
MAX_INVENTORY_BYTES = 128 * 1024
MAX_ACCOUNTS = 1
MAX_CLIENTS = 64
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

TOP_LEVEL_KEYS = {
    "schema_version",
    "platform",
    "mode",
    "deployment_id",
    "share",
    "wireguard",
    "safety",
}
SHARE_KEYS = {
    "name",
    "path",
    "expected_accounts",
    "guest_access",
    "read_only",
    "smb3_encryption_required",
}
WIREGUARD_KEYS = {"interface", "host_address", "allowed_client_addresses"}
SAFETY_KEYS = {
    "refuse_any_guest_share",
    "refuse_non_target_shares",
    "refuse_non_wireguard_listener",
    "require_pf_default_deny",
}
INVENTORY_KEYS = {
    "schema_version",
    "platform",
    "guest_smb_share_count",
    "non_target_smb_share_count",
    "target_share_state",
    "tcp_445_listeners",
    "pf_boundary_verified",
    "wireguard_interface_present",
    "wireguard_addresses",
}

SAFE_DEPLOYMENT_RE = re.compile(r"^[a-z][a-z0-9-]{2,47}$")
SAFE_SHARE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
SAFE_ACCOUNT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
SAFE_INTERFACE_RE = re.compile(r"^utun[0-9]{1,3}$")
PLACEHOLDER_RE = re.compile(r"<[^>]+>")


class MacOSSMBPlanError(RuntimeError):
    """A fail-closed configuration, inventory, or render error."""


@dataclass(frozen=True)
class ShareConfig:
    name: str
    path: Path
    expected_accounts: tuple[str, ...]


@dataclass(frozen=True)
class WireGuardConfig:
    interface: str
    host_address: ipaddress.IPv4Interface
    allowed_client_addresses: tuple[ipaddress.IPv4Interface, ...]


@dataclass(frozen=True)
class PlanConfig:
    deployment_id: str
    share: ShareConfig
    wireguard: WireGuardConfig


@dataclass(frozen=True)
class ShareRecord:
    name: str
    path: str
    smb_enabled: bool
    guest_access: bool


@dataclass(frozen=True)
class HostInventory:
    guest_smb_share_count: int
    non_target_smb_share_count: int
    target_share_state: str
    tcp_445_listeners: tuple[str, ...]
    pf_boundary_verified: bool
    wireguard_interface_present: bool
    wireguard_addresses: tuple[str, ...]
    provenance: str


def _fail(message: str) -> NoReturn:
    raise MacOSSMBPlanError(message)


def _require_exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        _fail(f"{label} has unsupported field(s): {', '.join(unknown)}")
    if missing:
        _fail(f"{label} is missing required field(s): {', '.join(missing)}")


def _require_table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{label} must be a TOML table")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be a string")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be true or false")
    return value


def _require_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(f"{label} must be an integer")
    return value


def _require_string_list(value: object, label: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _fail(f"{label} must be a non-empty array")
    if len(value) > maximum:
        _fail(f"{label} exceeds the {maximum}-entry limit")
    if not all(isinstance(item, str) for item in value):
        _fail(f"{label} entries must be strings")
    result = tuple(value)
    if len(set(result)) != len(result):
        _fail(f"{label} must not contain duplicates")
    return result


def _validate_private_file(path: Path, label: str, maximum: int) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        details = absolute.lstat()
    except FileNotFoundError:
        _fail(f"{label} does not exist")
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        _fail(f"{label} must be a regular, non-symlink file")
    if details.st_nlink != 1:
        _fail(f"{label} must not have multiple hard links")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        _fail(f"{label} must be owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o077:
        _fail(f"{label} must be owner-only (mode 0600 or stricter)")
    if details.st_size > maximum:
        _fail(f"{label} exceeds the {maximum}-byte limit")
    canonical = absolute.resolve(strict=True)
    canonical_details = canonical.lstat()
    if (details.st_dev, details.st_ino) != (canonical_details.st_dev, canonical_details.st_ino):
        _fail(f"{label} changed while resolving its canonical path")
    _validate_trusted_ancestors(canonical.parent, label)
    return canonical


def _validate_trusted_ancestors(path: Path, label: str) -> None:
    current = path.resolve(strict=True)
    while True:
        details = current.lstat()
        if not stat.S_ISDIR(details.st_mode):
            _fail(f"{label} has a non-directory ancestor")
        if hasattr(os, "getuid") and details.st_uid not in {0, os.getuid()}:
            _fail(f"{label} has an ancestor owned by another user")
        permissions = stat.S_IMODE(details.st_mode)
        if permissions & 0o022 and not permissions & stat.S_ISVTX:
            _fail(f"{label} has an ancestor writable by another user")
        if current.parent == current:
            return
        current = current.parent


def initialize_config(path: Path, example_path: Path = EXAMPLE_CONFIG) -> Path:
    """Create an owner-only local config without overwriting existing state."""

    destination = Path(os.path.abspath(os.fspath(path)))
    parent = destination.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        _fail("local config parent must be an existing, non-symlink directory")
    if destination.exists() or destination.is_symlink():
        _fail("refusing to overwrite the existing local config")
    payload = example_path.read_bytes()
    if len(payload) > MAX_CONFIG_BYTES:
        _fail("example config exceeds the size limit")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return _validate_private_file(destination, "local config", MAX_CONFIG_BYTES)


def _parse_private_ipv4_interface(value: str, label: str) -> ipaddress.IPv4Interface:
    try:
        interface = ipaddress.ip_interface(value)
    except ValueError:
        _fail(f"{label} must be a valid IPv4 /32")
    if not isinstance(interface, ipaddress.IPv4Interface) or interface.network.prefixlen != 32:
        _fail(f"{label} must be an IPv4 /32")
    address = interface.ip
    if not any(address in network for network in RFC1918_NETWORKS):
        _fail(f"{label} must use an RFC1918 IPv4 address")
    return interface


def load_config(path: Path) -> PlanConfig:
    canonical = _validate_private_file(path, "local config", MAX_CONFIG_BYTES)
    with canonical.open("rb") as handle:
        payload = tomllib.load(handle)
    root = _require_table(payload, "config")
    _require_exact_keys(root, TOP_LEVEL_KEYS, "config")
    if _require_int(root["schema_version"], "schema_version") != 1:
        _fail("schema_version must be 1")
    if _require_string(root["platform"], "platform") != "macos":
        _fail("platform must be macos")
    if _require_string(root["mode"], "mode") != "render-only":
        _fail("mode must remain render-only")

    deployment_id = _require_string(root["deployment_id"], "deployment_id")
    if not SAFE_DEPLOYMENT_RE.fullmatch(deployment_id) or PLACEHOLDER_RE.search(deployment_id):
        _fail("deployment_id must be a lowercase local identifier")

    share = _require_table(root["share"], "share")
    wireguard = _require_table(root["wireguard"], "wireguard")
    safety = _require_table(root["safety"], "safety")
    _require_exact_keys(share, SHARE_KEYS, "share")
    _require_exact_keys(wireguard, WIREGUARD_KEYS, "wireguard")
    _require_exact_keys(safety, SAFETY_KEYS, "safety")

    share_name = _require_string(share["name"], "share.name")
    if not SAFE_SHARE_RE.fullmatch(share_name):
        _fail("share.name must be a conservative SMB share identifier")
    raw_share_path = _require_string(share["path"], "share.path")
    if PLACEHOLDER_RE.search(raw_share_path) or "\x00" in raw_share_path:
        _fail("share.path still contains a placeholder or invalid character")
    share_path = Path(raw_share_path)
    if not share_path.is_absolute() or share_path == Path("/"):
        _fail("share.path must be an absolute non-root path")
    if share_path == REPO_ROOT or REPO_ROOT in share_path.parents:
        _fail("share.path must remain outside the Git repository")

    accounts = _require_string_list(
        share["expected_accounts"], "share.expected_accounts", MAX_ACCOUNTS
    )
    if any(not SAFE_ACCOUNT_RE.fullmatch(account) for account in accounts):
        _fail("share.expected_accounts contains an invalid account name")
    if _require_bool(share["guest_access"], "share.guest_access"):
        _fail("share.guest_access must be false")
    if _require_bool(share["read_only"], "share.read_only"):
        _fail("share.read_only must be false for authenticated read/write access")
    if not _require_bool(share["smb3_encryption_required"], "share.smb3_encryption_required"):
        _fail("share.smb3_encryption_required must be true")

    interface_name = _require_string(wireguard["interface"], "wireguard.interface")
    if not SAFE_INTERFACE_RE.fullmatch(interface_name):
        _fail("wireguard.interface must be an exact macOS utun interface name")
    host_address = _parse_private_ipv4_interface(
        _require_string(wireguard["host_address"], "wireguard.host_address"),
        "wireguard.host_address",
    )
    raw_clients = _require_string_list(
        wireguard["allowed_client_addresses"],
        "wireguard.allowed_client_addresses",
        MAX_CLIENTS,
    )
    clients = tuple(
        _parse_private_ipv4_interface(value, "wireguard.allowed_client_addresses entry")
        for value in raw_clients
    )
    if host_address.ip in {client.ip for client in clients}:
        _fail("wireguard host and client addresses must be distinct")

    for field in sorted(SAFETY_KEYS):
        if not _require_bool(safety[field], f"safety.{field}"):
            _fail(f"safety.{field} must be true")

    return PlanConfig(
        deployment_id=deployment_id,
        share=ShareConfig(
            name=share_name,
            path=share_path,
            expected_accounts=accounts,
        ),
        wireguard=WireGuardConfig(
            interface=interface_name,
            host_address=host_address,
            allowed_client_addresses=clients,
        ),
    )


def validate_share_path_runtime(config: PlanConfig) -> Path:
    path = config.share.path
    try:
        details = path.lstat()
    except FileNotFoundError:
        _fail("configured share path does not exist")
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        _fail("configured share path must be a real directory, not a symlink")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        _fail("configured share path must be owned by the current user")
    if (stat.S_IMODE(details.st_mode) & 0o777) != 0o700:
        _fail("configured share path must be owner-only mode 0700")
    owner_name = pwd.getpwuid(details.st_uid).pw_name
    if config.share.expected_accounts != (owner_name,):
        _fail("share.expected_accounts must contain only the share-directory owner")
    if platform.system() == "Darwin":
        acl_output = _run_read_only(["/bin/ls", "-lde", os.fspath(path)])
        first_line = acl_output.splitlines()[0] if acl_output.splitlines() else ""
        permission_field = first_line.split(maxsplit=1)[0] if first_line else ""
        if permission_field.endswith("+"):
            _fail("configured share path has an extended ACL that the renderer cannot prove safe")
    canonical = path.resolve(strict=True)
    if canonical == REPO_ROOT or REPO_ROOT in canonical.parents:
        _fail("configured share path must remain outside the Git repository")
    return canonical


def parse_sharing_list(output: str) -> tuple[ShareRecord, ...]:
    """Parse the stable text fields emitted by macOS ``sharing -l``."""

    if "List of Share Points" not in output:
        _fail("macOS sharing inventory could not be enumerated")
    records: list[ShareRecord] = []
    current_name = ""
    current_path = ""
    in_smb = False
    shared: bool | None = None
    guest: bool | None = None

    def finish_record() -> None:
        nonlocal current_name, current_path, in_smb, shared, guest
        if in_smb:
            if shared is None or guest is None:
                _fail("macOS sharing inventory omitted required SMB safety fields")
            records.append(
                ShareRecord(
                    name=current_name,
                    path=current_path,
                    smb_enabled=shared,
                    guest_access=guest,
                )
            )
        current_name = ""
        current_path = ""
        in_smb = False
        shared = None
        guest = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("name:") and not in_smb:
            if current_name or current_path:
                finish_record()
            current_name = line.partition(":")[2].strip()
        elif line.startswith("path:") and not in_smb:
            current_path = line.partition(":")[2].strip()
        elif line.startswith("smb:"):
            in_smb = True
        elif in_smb and line.startswith("shared:"):
            value = line.partition(":")[2].strip()
            if value not in {"0", "1"}:
                _fail("macOS sharing inventory returned an invalid SMB enabled flag")
            shared = value == "1"
        elif in_smb and line.startswith("guest access:"):
            value = line.partition(":")[2].strip()
            if value not in {"0", "1"}:
                _fail("macOS sharing inventory returned an invalid guest flag")
            guest = value == "1"
        elif in_smb and line == "}":
            finish_record()
    if current_name or current_path or in_smb:
        finish_record()
    if "smb:" in output and not records:
        _fail("unable to parse the macOS SMB share inventory")
    return tuple(records)


def _run_read_only(command: Sequence[str], acceptable: set[int] | None = None) -> str:
    accepted = acceptable or {0}
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail("a required read-only macOS inventory command could not run")
    if result.returncode not in accepted:
        _fail("a required read-only macOS inventory command failed")
    return result.stdout


def _collect_wireguard_addresses(interface_name: str) -> tuple[bool, tuple[str, ...]]:
    try:
        output = _run_read_only(["/sbin/ifconfig", interface_name])
    except MacOSSMBPlanError:
        return False, ()
    addresses: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^\s*inet\s+([0-9.]+).*\bnetmask\s+(0x[0-9a-fA-F]+|[0-9.]+)", line)
        if not match:
            continue
        address = ipaddress.ip_address(match.group(1))
        raw_mask = match.group(2)
        if raw_mask.startswith("0x"):
            mask_integer = int(raw_mask, 16)
            mask = ipaddress.ip_address(mask_integer)
        else:
            mask = ipaddress.ip_address(raw_mask)
        network = ipaddress.IPv4Network(f"0.0.0.0/{mask}")
        addresses.append(f"{address}/{network.prefixlen}")
    return True, tuple(addresses)


def _collect_tcp_445_listeners() -> tuple[str, ...]:
    output = _run_read_only(
        ["/usr/sbin/lsof", "-nP", "-Fn", "-iTCP:445", "-sTCP:LISTEN"],
        acceptable={0, 1},
    )
    listeners: list[str] = []
    for line in output.splitlines():
        if not line.startswith("n"):
            continue
        endpoint = line[1:].strip()
        match = re.search(r"(?:\[([^]]+)\]|([^:]+)):445(?:\s|$)", endpoint)
        if match:
            listeners.append(match.group(1) or match.group(2))
    return tuple(dict.fromkeys(listeners))


def _collect_pf_boundary_verified(config: PlanConfig) -> bool:
    """Prove the active Snowbridge anchor when PF permits read-only inspection."""

    try:
        command_results = tuple(
            subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
            )
            for command in (
                ["/sbin/pfctl", "-s", "info"],
                ["/sbin/pfctl", "-n", "-sr"],
                ["/sbin/pfctl", "-n", "-a", PF_ANCHOR_NAME, "-sr"],
            )
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if any(result.returncode != 0 for result in command_results):
        return False
    info_result, root_rules_result, anchor_result = command_results
    if not any(
        line.strip().startswith("Status: Enabled") for line in info_result.stdout.splitlines()
    ):
        return False
    if 'anchor "com.apple/*"' not in root_rules_result.stdout:
        return False
    host_ip = config.wireguard.host_address.ip
    expected_rules = tuple(
        [
            f"pass in quick on {config.wireguard.interface} inet proto tcp "
            f"from {client.ip} to {host_ip} port = 445 flags S/SA keep state"
            for client in config.wireguard.allowed_client_addresses
        ]
        + [
            "block drop in quick inet proto tcp from any to any port = 445",
            "block drop in quick inet6 proto tcp from any to any port = 445",
        ]
    )
    active_rules = tuple(line.strip() for line in anchor_result.stdout.splitlines() if line.strip())
    return active_rules == expected_rules


def collect_host_inventory(config: PlanConfig) -> HostInventory:
    if platform.system() != "Darwin":
        _fail("live host inventory is supported only on macOS")
    share_output = _run_read_only(["/usr/sbin/sharing", "-l"])
    records = tuple(record for record in parse_sharing_list(share_output) if record.smb_enabled)
    canonical_target = config.share.path.resolve(strict=False)
    exact_records = tuple(
        record
        for record in records
        if record.name == config.share.name
        and Path(record.path).resolve(strict=False) == canonical_target
    )
    conflicts = tuple(
        record
        for record in records
        if record.name == config.share.name
        or Path(record.path).resolve(strict=False) == canonical_target
    )
    if exact_records:
        target_state = "exact"
    elif conflicts:
        target_state = "conflict"
    else:
        target_state = "absent"
    interface_present, addresses = _collect_wireguard_addresses(config.wireguard.interface)
    return HostInventory(
        guest_smb_share_count=sum(record.guest_access for record in records),
        non_target_smb_share_count=len(records) - len(exact_records),
        target_share_state=target_state,
        tcp_445_listeners=_collect_tcp_445_listeners(),
        pf_boundary_verified=_collect_pf_boundary_verified(config),
        wireguard_interface_present=interface_present,
        wireguard_addresses=addresses,
        provenance="live",
    )


def load_inventory_fixture(path: Path) -> HostInventory:
    canonical = _validate_private_file(path, "inventory fixture", MAX_INVENTORY_BYTES)
    with canonical.open("rb") as handle:
        value = json.load(handle)
    root = _require_table(value, "inventory fixture")
    _require_exact_keys(root, INVENTORY_KEYS, "inventory fixture")
    if _require_int(root["schema_version"], "inventory schema_version") != 1:
        _fail("inventory schema_version must be 1")
    if _require_string(root["platform"], "inventory platform") != "macos":
        _fail("inventory platform must be macos")
    guest_count = _require_int(root["guest_smb_share_count"], "guest_smb_share_count")
    non_target_count = _require_int(
        root["non_target_smb_share_count"], "non_target_smb_share_count"
    )
    if guest_count < 0 or non_target_count < 0:
        _fail("inventory share counts must be non-negative")
    target_state = _require_string(root["target_share_state"], "target_share_state")
    if target_state not in {"absent", "exact", "conflict"}:
        _fail("target_share_state must be absent, exact, or conflict")
    listeners = _require_string_list_allow_empty(root["tcp_445_listeners"], "tcp_445_listeners")
    addresses = _require_string_list_allow_empty(root["wireguard_addresses"], "wireguard_addresses")
    interface_present = _require_bool(
        root["wireguard_interface_present"], "wireguard_interface_present"
    )
    pf_boundary_verified = _require_bool(root["pf_boundary_verified"], "pf_boundary_verified")
    return HostInventory(
        guest_smb_share_count=guest_count,
        non_target_smb_share_count=non_target_count,
        target_share_state=target_state,
        tcp_445_listeners=listeners,
        pf_boundary_verified=pf_boundary_verified,
        wireguard_interface_present=interface_present,
        wireguard_addresses=addresses,
        provenance="fixture",
    )


def _require_string_list_allow_empty(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_CLIENTS:
        _fail(f"{label} must be an array with at most {MAX_CLIENTS} entries")
    if not all(isinstance(item, str) for item in value):
        _fail(f"{label} entries must be strings")
    result = tuple(value)
    if len(set(result)) != len(result):
        _fail(f"{label} must not contain duplicates")
    return result


def audit_inventory(config: PlanConfig, inventory: HostInventory) -> None:
    if inventory.guest_smb_share_count:
        _fail(
            "refusing to continue while an enabled macOS SMB share permits guest access "
            f"({inventory.guest_smb_share_count} found)"
        )
    if inventory.non_target_smb_share_count:
        _fail(
            "refusing to apply a global SMB firewall boundary while non-target SMB shares "
            f"are enabled ({inventory.non_target_smb_share_count} found)"
        )
    if inventory.target_share_state == "conflict":
        _fail("the configured SMB share name or path conflicts with an existing share point")
    if not inventory.wireguard_interface_present:
        _fail("the configured WireGuard interface is not present")
    expected_address = str(config.wireguard.host_address)
    if inventory.wireguard_addresses != (expected_address,):
        _fail("the WireGuard interface does not have exactly the configured IPv4 /32")
    host_ip = str(config.wireguard.host_address.ip)
    wildcard_listeners = {"*", "0.0.0.0", "::"}
    explicit_non_wireguard = tuple(
        listener
        for listener in inventory.tcp_445_listeners
        if listener != host_ip and listener not in wildcard_listeners
    )
    if explicit_non_wireguard:
        _fail("TCP 445 is listening on an explicit non-WireGuard address; refusing broad exposure")
    wildcard = tuple(
        listener for listener in inventory.tcp_445_listeners if listener in wildcard_listeners
    )
    if wildcard and not inventory.pf_boundary_verified:
        _fail("TCP 445 has a wildcard listener without a verified WireGuard-only PF boundary")


def render_pf_anchor(config: PlanConfig) -> str:
    clients = ", ".join(str(client.ip) for client in config.wireguard.allowed_client_addresses)
    return (
        "# Snowbridge native macOS SMB boundary. Rendered; not loaded.\n"
        f'wg_if = "{config.wireguard.interface}"\n'
        f'wg_host = "{config.wireguard.host_address.ip}"\n'
        f'allowed_clients = "{{ {clients} }}"\n'
        "\n"
        "pass in quick on $wg_if inet proto tcp from $allowed_clients "
        "to $wg_host port 445 flags S/SA keep state\n"
        "block drop in quick inet proto tcp from any to any port 445\n"
        "block drop in quick inet6 proto tcp from any to any port 445\n"
    )


def proposed_sharing_command(
    config: PlanConfig, target_state: str, share_path: Path | None = None
) -> list[str]:
    if target_state == "exact":
        command = ["/usr/sbin/sharing", "-e", config.share.name]
    else:
        command = [
            "/usr/sbin/sharing",
            "-a",
            os.fspath(share_path or config.share.path),
            "-n",
            config.share.name,
        ]
    return [
        *command,
        "-S",
        config.share.name,
        "-s",
        "001",
        "-g",
        "000",
        "-R",
        "0",
        "-E",
        "1",
    ]


def _ensure_private_output_directory(path: Path, enforce_repo_boundary: bool) -> Path:
    lexical_output = Path(os.path.abspath(os.fspath(path)))
    if lexical_output.is_symlink():
        _fail("render output must not be a symlink")
    output = lexical_output.resolve(strict=False)
    if enforce_repo_boundary:
        artifacts_root = (REPO_ROOT / "artifacts").resolve(strict=False)
        try:
            output.relative_to(artifacts_root)
        except ValueError:
            _fail("render output must stay under the ignored artifacts directory")
    if output.exists():
        details = output.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            _fail("render output must be a real directory")
    else:
        output.mkdir(mode=0o700, parents=True)
        details = output.lstat()
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        _fail("render output must be owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o077:
        _fail("render output must be owner-only (mode 0700 or stricter)")
    return output.resolve(strict=True)


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            _fail("refusing to replace a non-regular render artifact")
        if details.st_nlink != 1:
            _fail("refusing to replace a multiply-linked render artifact")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            _fail("render artifact must be owned by the current user")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def render_plan(
    config: PlanConfig,
    inventory: HostInventory,
    output_directory: Path,
    *,
    enforce_repo_boundary: bool = True,
    syntax_check: bool = True,
) -> tuple[Path, Path]:
    canonical_share_path = validate_share_path_runtime(config)
    audit_inventory(config, inventory)
    output = _ensure_private_output_directory(output_directory, enforce_repo_boundary)
    anchor_path = output / "snowbridge-smb.pf"
    plan_path = output / "activation-plan.json"
    anchor_text = render_pf_anchor(config)
    _atomic_write(anchor_path, anchor_text.encode("utf-8"))

    if syntax_check and platform.system() == "Darwin":
        result = subprocess.run(
            ["/sbin/pfctl", "-n", "-a", PF_ANCHOR_NAME, "-f", os.fspath(anchor_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            anchor_path.unlink(missing_ok=True)
            _fail("macOS pfctl rejected the rendered anchor syntax")

    sharing_command = proposed_sharing_command(
        config, inventory.target_share_state, canonical_share_path
    )
    plan = {
        "schema_version": 1,
        "deployment_id": config.deployment_id,
        "platform": "macos",
        "mode": "render-only",
        "activation_supported": False,
        "future_activation_gate": {
            "explicit_operator_confirmation_required": True,
            "root_required": True,
        },
        "inventory_provenance": inventory.provenance,
        "share": {
            "name": config.share.name,
            "path": os.fspath(canonical_share_path),
            "expected_accounts": list(config.share.expected_accounts),
            "native_account_authorization": "activation-precondition-not-mutated",
            "filesystem_authorization": "verified-owner-only-directory",
            "guest_access": False,
            "read_only": False,
            "smb3_encryption_required": True,
        },
        "wireguard_boundary": {
            "interface": config.wireguard.interface,
            "host_address": str(config.wireguard.host_address),
            "allowed_client_addresses": [
                str(client) for client in config.wireguard.allowed_client_addresses
            ],
            "deny_tcp_445_on_every_other_interface": True,
            "interface_allocation_policy": (
                "configured host /32 is source of truth; rediscover and rerender "
                "if macOS reallocates the utun name"
            ),
        },
        "review_only_commands": {
            "account_preflight": [
                ["/usr/bin/id", account] for account in config.share.expected_accounts
            ],
            "pf_syntax_check": [
                "/sbin/pfctl",
                "-n",
                "-a",
                PF_ANCHOR_NAME,
                "-f",
                os.fspath(anchor_path),
            ],
            "proposed_share_change": sharing_command,
        },
        "required_activation_order": [
            "capture restorable PF and share-point state",
            "install and verify the WireGuard-only PF boundary",
            "create or edit the share with guest access disabled",
            "enable macOS File Sharing",
            "verify TCP 445 succeeds through WireGuard and fails everywhere else",
            "roll back every prior step if any verification fails",
        ],
        "required_rollback_order": [
            "disable File Sharing only if this transaction enabled it",
            "restore or remove only the target share-point record",
            "restore the prior Snowbridge PF anchor rules",
            "release only the PF enable token acquired by this transaction",
            "verify TCP 445 is not exposed on a non-WireGuard boundary",
        ],
        "blocked_live_steps": [
            "PF rule loading and enable-token lifecycle",
            "macOS File Sharing service activation",
            "share-point mutation",
            "account or credential mutation",
            "the macOS Windows File Sharing account authorization toggle",
        ],
        "rollback_status": "not-implemented",
    }
    _atomic_write(
        plan_path,
        (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return plan_path, anchor_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and render a fail-closed native macOS Snowbridge SMB plan."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create the ignored owner-only local config.")
    subparsers.add_parser("validate", help="Validate local configuration only.")

    audit_parser = subparsers.add_parser("audit", help="Validate read-only host state.")
    audit_parser.add_argument("--inventory-file", type=Path)

    render_parser = subparsers.add_parser("render", help="Render owner-only review artifacts.")
    render_parser.add_argument("--inventory-file", type=Path)
    render_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "init":
            created = initialize_config(arguments.config)
            print(f"created owner-only local config: {created}")
            print("edit every placeholder before validation; no live state changed")
            return 0
        config = load_config(arguments.config)
        if arguments.command == "validate":
            print("valid render-only native macOS SMB configuration")
            return 0
        inventory = (
            load_inventory_fixture(arguments.inventory_file)
            if arguments.inventory_file
            else collect_host_inventory(config)
        )
        if arguments.command == "audit":
            validate_share_path_runtime(config)
            audit_inventory(config, inventory)
            print(
                "macOS SMB host audit passed: no guest share or non-WireGuard "
                "TCP 445 exposure detected"
            )
            return 0
        plan_path, anchor_path = render_plan(config, inventory, arguments.output)
        print(f"rendered owner-only review plan: {plan_path}")
        print(f"rendered owner-only PF anchor: {anchor_path}")
        print("live activation remains intentionally unsupported")
        return 0
    except (
        MacOSSMBPlanError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
