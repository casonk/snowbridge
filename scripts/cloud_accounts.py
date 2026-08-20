#!/usr/bin/env python3
"""Initialize and audit private, inventory-only cloud account declarations.

The rclone checks never contact a configured storage backend. This tool
validates an owner-only Snowbridge registry and asks rclone only whether its
config is encrypted and which local remote aliases and backend types are
configured. Provider enrollment is a separate online operation. A custom
config-password helper has its own I/O boundary. Data listing, copying,
syncing, mounting, and deletion remain unsupported.
"""

from __future__ import annotations

import argparse
import csv
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config/cloud/accounts.local.toml"
DEFAULT_RCLONE_CONFIG = Path.home() / ".config/snowbridge/rclone.conf"
MACOS_PASSWORD_COMMAND = (
    "/usr/bin/security",
    "find-generic-password",
    "-a",
    "snowbridge",
    "-s",
    "snowbridge-rclone-config",
    "-w",
)
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RCLONE_CONFIG_BYTES = 4 * 1024 * 1024
MAX_ACCOUNTS = 32
SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SAFE_BACKEND_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SAFE_REMOTE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SAFE_ROOT_RE = re.compile(r"^[A-Za-z0-9._ /-]{0,512}$")
TOP_LEVEL_KEYS = {"schema_version", "rclone_config", "accounts"}
ACCOUNT_KEYS = {"id", "backend", "remote", "root", "share_target", "mode", "enabled"}


class CloudAccountError(RuntimeError):
    """A fail-closed local configuration or doctor error."""


@dataclass(frozen=True)
class CloudProvider:
    """A named cloud provider Snowbridge onboards.

    ``backend`` is the exact rclone type string. It is deliberately the only
    binding between a provider and an account: an account already declares its
    rclone type, and the doctor already cross-checks that declaration against
    the encrypted config. Adding a second, separately declared provider field
    would create a value that can disagree with the type rclone actually uses.

    ``read_only_option`` is the rclone config setting that enrolls the account
    without write permission, or ``None`` when the backend exposes no such
    setting. That distinction is the reason this table exists: it is a
    provider-permission fact that cannot be discovered offline at doctor time.
    """

    name: str
    display_name: str
    backend: str
    credential: str
    revocation: str
    read_only_option: str | None
    notes: tuple[str, ...]

    @property
    def supports_read_only_enrollment(self) -> bool:
        return self.read_only_option is not None


# Verified against rclone v1.75.0 `rclone help backend <type>`. Re-verify the
# option names and scope values when the pinned rclone version changes; a
# renamed scope silently grants more access than intended.
PROVIDERS: tuple[CloudProvider, ...] = (
    CloudProvider(
        name="google-drive",
        display_name="Google Drive",
        backend="drive",
        credential="oauth-token",
        revocation="Google Account > Security > Your connections to third-party apps",
        read_only_option="scope = drive.readonly",
        notes=(
            "Leaving scope unset requests full read/write access to every file.",
            "scope = drive.file narrows access to files rclone itself created.",
        ),
    ),
    CloudProvider(
        name="onedrive",
        display_name="Microsoft OneDrive",
        backend="onedrive",
        credential="oauth-token",
        revocation="Microsoft account > Privacy > Apps and services you have granted access",
        read_only_option=(
            "access_scopes = Files.Read Files.Read.All Sites.Read.All offline_access"
        ),
        notes=(
            "The default access_scopes value includes Files.ReadWrite and "
            "Files.ReadWrite.All, so an unset value is a read/write grant.",
        ),
    ),
    CloudProvider(
        name="icloud",
        display_name="iCloud Drive",
        backend="iclouddrive",
        credential="account-password",
        revocation="account.apple.com > Sign-In and Security > App-Specific Passwords",
        read_only_option=None,
        notes=(
            "rclone exposes no scope option for this backend, so enrollment is "
            "always read/write. Least privilege must come from the selected "
            "root folder, not from the grant.",
            "The stored secret is an account password, not a scoped token. "
            "rclone obscures it, and obscuring is reversible encoding rather "
            "than encryption; only the config encryption protects it at rest.",
            "Enroll with an app-specific password so the credential can be "
            "revoked without changing the Apple ID password.",
            "service = drive selects iCloud Drive; service = photos selects the "
            "photo library instead.",
        ),
    ),
)

PROVIDERS_BY_BACKEND = {provider.backend: provider for provider in PROVIDERS}
PROVIDERS_BY_NAME = {provider.name: provider for provider in PROVIDERS}


def provider_for_backend(backend: str) -> CloudProvider | None:
    """Return the named provider for an rclone backend type, if it is one."""

    return PROVIDERS_BY_BACKEND.get(backend)


def describe_accounts(accounts: Sequence["CloudAccount"]) -> tuple[str, ...]:
    """Summarize declared accounts by provider without revealing aliases.

    Account ids, remote aliases, and roots are owner-only metadata, so the
    summary reports counts per provider rather than naming any account.
    """

    lines: list[str] = []
    for provider in PROVIDERS:
        matching = [a for a in accounts if a.backend == provider.backend]
        if not matching:
            continue
        enabled = sum(account.enabled for account in matching)
        detail = "read-only enrollment available"
        if not provider.supports_read_only_enrollment:
            detail = "no read-only enrollment; grant is always read/write"
        lines.append(
            f"{provider.name}: {len(matching)} declared, {enabled} enabled "
            f"({provider.credential}, {detail})"
        )
    unlisted = sum(1 for a in accounts if a.backend not in PROVIDERS_BY_BACKEND)
    if unlisted:
        lines.append(
            f"unlisted backends: {unlisted} declared "
            "(inventory-only; not a Snowbridge-onboarded provider)"
        )
    return tuple(lines)


@dataclass(frozen=True)
class CloudAccount:
    account_id: str
    backend: str
    remote: str
    root: str
    share_target: Path
    mode: str
    enabled: bool


@dataclass(frozen=True)
class CloudRegistry:
    rclone_config: Path
    accounts: tuple[CloudAccount, ...]


def _fail(message: str) -> NoReturn:
    raise CloudAccountError(message)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        _fail(f"{label} has unsupported field(s): {', '.join(unknown)}")
    if missing:
        _fail(f"{label} is missing required field(s): {', '.join(missing)}")


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be a string")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be true or false")
    return value


def _validate_private_file(
    path: Path, label: str, maximum: int
) -> tuple[Path, os.stat_result]:
    lexical = _absolute(path)
    try:
        lexical_details = lexical.lstat()
    except FileNotFoundError:
        _fail(f"{label} does not exist: {lexical}")
    _validate_private_stat(lexical_details, label, maximum)
    canonical = lexical.resolve(strict=True)
    _validate_private_directory(canonical.parent, f"{label} parent directory")
    canonical_details = canonical.lstat()
    _validate_private_stat(canonical_details, label, maximum)
    if (lexical_details.st_dev, lexical_details.st_ino) != (
        canonical_details.st_dev,
        canonical_details.st_ino,
    ):
        _fail(f"{label} changed while its canonical path was resolved")
    return canonical, canonical_details


def _validate_private_stat(details: os.stat_result, label: str, maximum: int) -> None:
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        _fail(f"{label} must be a regular, non-symlink file")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        _fail(f"{label} must be owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o077:
        _fail(f"{label} must be owner-only (mode 0600 or stricter)")
    if details.st_size > maximum:
        _fail(f"{label} exceeds the {maximum}-byte limit")


def _validate_private_directory(
    path: Path, label: str
) -> tuple[Path, os.stat_result]:
    lexical = _absolute(path)
    try:
        lexical_details = lexical.lstat()
    except FileNotFoundError:
        _fail(f"{label} does not exist: {lexical}")
    if stat.S_ISLNK(lexical_details.st_mode) or not stat.S_ISDIR(lexical_details.st_mode):
        _fail(f"{label} must be a real directory")
    canonical = lexical.resolve(strict=True)
    details = canonical.lstat()
    if (lexical_details.st_dev, lexical_details.st_ino) != (details.st_dev, details.st_ino):
        _fail(f"{label} changed while its canonical path was resolved")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        _fail(f"{label} must be owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o077:
        _fail(f"{label} must be owner-only (mode 0700 or stricter)")
    _validate_trusted_ancestors(canonical.parent, label)
    return canonical, details


def _validate_trusted_ancestors(path: Path, label: str) -> None:
    """Reject replaceable ancestors above a resolved owner-only directory."""

    current = _absolute(path).resolve(strict=True)
    while True:
        details = current.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            _fail(f"{label} has a symlinked or non-directory ancestor")
        if hasattr(os, "getuid") and details.st_uid not in {0, os.getuid()}:
            _fail(f"{label} has an ancestor owned by an untrusted user")
        permissions = stat.S_IMODE(details.st_mode)
        writable_by_others = bool(permissions & 0o022)
        sticky = bool(permissions & stat.S_ISVTX)
        if writable_by_others and not sticky:
            _fail(f"{label} has an untrusted writable ancestor")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
    )


def _validate_remote_root(value: object, label: str) -> str:
    root = _require_string(value, label)
    if root != root.strip():
        _fail(f"{label} must not have leading or trailing whitespace")
    if not SAFE_ROOT_RE.fullmatch(root):
        _fail(f"{label} contains unsupported characters")
    if root.startswith("/") or ":" in root or "\\" in root:
        _fail(f"{label} must be a relative path inside the configured remote")
    normalized = PurePosixPath(root)
    if root == ".":
        _fail(f"{label} must select a subfolder, not the remote root")
    if root and str(normalized) != root:
        _fail(f"{label} must be a normalized relative path")
    parts = normalized.parts
    if any(part in {".", ".."} for part in parts):
        _fail(f"{label} must not contain dot traversal components")
    return root


def _validate_external_absolute_path(value: object, label: str) -> Path:
    raw = _require_string(value, label)
    candidate = Path(raw)
    if not candidate.is_absolute():
        _fail(f"{label} must be an absolute path")
    absolute = candidate.resolve(strict=False)
    if _is_inside(absolute, REPO_ROOT):
        _fail(f"{label} must stay outside the Git repository")
    return absolute


def load_registry(path: Path) -> CloudRegistry:
    absolute, expected = _validate_private_file(
        path, "cloud account registry", MAX_CONFIG_BYTES
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(absolute, flags)
    try:
        before = os.fstat(descriptor)
        _validate_private_stat(before, "cloud account registry", MAX_CONFIG_BYTES)
        if (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino):
            _fail("cloud account registry changed before it could be opened")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(MAX_CONFIG_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_CONFIG_BYTES:
        _fail(f"cloud account registry exceeds the {MAX_CONFIG_BYTES}-byte limit")
    if not _same_file_state(before, after):
        _fail("cloud account registry changed while it was being read")
    raw = tomllib.load(io.BytesIO(payload))
    if not isinstance(raw, dict):
        _fail("cloud account registry must be a TOML table")
    _require_exact_keys(raw, TOP_LEVEL_KEYS, "cloud account registry")
    schema_version = raw["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        _fail("schema_version must be the integer 1")
    rclone_config = _validate_external_absolute_path(raw["rclone_config"], "rclone_config")
    raw_accounts = raw["accounts"]
    if not isinstance(raw_accounts, list):
        _fail("accounts must be an array of tables")
    if len(raw_accounts) > MAX_ACCOUNTS:
        _fail(f"accounts may contain at most {MAX_ACCOUNTS} entries")

    accounts: list[CloudAccount] = []
    ids: set[str] = set()
    remotes: set[str] = set()
    targets: set[Path] = set()
    for index, value in enumerate(raw_accounts):
        label = f"accounts[{index}]"
        if not isinstance(value, dict):
            _fail(f"{label} must be a table")
        _require_exact_keys(value, ACCOUNT_KEYS, label)
        account_id = _require_string(value["id"], f"{label}.id")
        backend = _require_string(value["backend"], f"{label}.backend")
        remote = _require_string(value["remote"], f"{label}.remote")
        if not SAFE_ID_RE.fullmatch(account_id):
            _fail(f"{label}.id must match {SAFE_ID_RE.pattern}")
        if not SAFE_BACKEND_RE.fullmatch(backend):
            _fail(f"{label}.backend must match {SAFE_BACKEND_RE.pattern}")
        if not SAFE_REMOTE_RE.fullmatch(remote):
            _fail(f"{label}.remote must match {SAFE_REMOTE_RE.pattern}")
        root = _validate_remote_root(value["root"], f"{label}.root")
        share_target = _validate_external_absolute_path(
            value["share_target"], f"{label}.share_target"
        )
        mode = _require_string(value["mode"], f"{label}.mode")
        if mode != "inventory":
            _fail(f"{label}.mode must be inventory; data mutation is not implemented")
        enabled = _require_bool(value["enabled"], f"{label}.enabled")
        if enabled and not root:
            _fail(f"{label}.root must select a folder when the account is enabled")
        if account_id in ids:
            _fail(f"duplicate account id: {account_id}")
        if remote in remotes:
            _fail(f"duplicate rclone remote: {remote}")
        if share_target in targets:
            _fail(f"duplicate share target: {share_target}")
        ids.add(account_id)
        remotes.add(remote)
        targets.add(share_target)
        accounts.append(
            CloudAccount(
                account_id=account_id,
                backend=backend,
                remote=remote,
                root=root,
                share_target=share_target,
                mode=mode,
                enabled=enabled,
            )
        )
    return CloudRegistry(rclone_config=rclone_config, accounts=tuple(accounts))


def initialize_registry(path: Path, rclone_config: Path) -> Path:
    absolute = _absolute(path)
    if not rclone_config.is_absolute():
        _fail("rclone config must be an absolute path outside the Git repository")
    rclone_absolute = _absolute(rclone_config).resolve(strict=False)
    if _is_inside(rclone_absolute, REPO_ROOT):
        _fail("rclone config must be an absolute path outside the Git repository")
    parent = absolute.parent
    if parent.exists():
        details = parent.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            _fail("cloud config directory must be a real directory")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            _fail("cloud config directory must be owned by the current user")
        if stat.S_IMODE(details.st_mode) & 0o077:
            if parent != DEFAULT_CONFIG.parent:
                _fail("existing cloud config directory must already be owner-only")
            os.chmod(parent, 0o700)
    else:
        parent.mkdir(mode=0o700, parents=True)
        os.chmod(parent, 0o700)
    canonical_parent, _ = _validate_private_directory(parent, "cloud config directory")
    absolute = canonical_parent / absolute.name
    payload = (
        "# Owner-only local registry. OAuth tokens belong only in the encrypted rclone config.\n"
        "schema_version = 1\n"
        f"rclone_config = {json.dumps(os.fspath(rclone_absolute))}\n"
        "accounts = []\n"
    ).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute, flags, 0o600)
    except FileExistsError:
        _fail(f"refusing to overwrite existing cloud account registry: {absolute}")
    created = os.fstat(descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            current = absolute.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                absolute.unlink()
        raise
    return absolute


def _resolve_executable(value: str) -> Path:
    candidate = shutil.which(value)
    if candidate is None:
        _fail(f"rclone executable was not found: {value}")
    resolved = Path(candidate).resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        _fail("rclone must resolve to an executable regular file")
    return resolved


def _validate_password_command(command: Sequence[str]) -> str:
    if isinstance(command, (str, bytes)) or not command or len(command) > 32:
        _fail("password command must contain between 1 and 32 argument fields")
    parts: list[str] = []
    for part in command:
        if not isinstance(part, str) or not part:
            _fail("password command fields must be non-empty strings")
        if len(part) > 1024 or any(character in part for character in "\x00\n\r"):
            _fail("password command fields must be bounded single-line strings")
        parts.append(part)
    if not Path(parts[0]).is_absolute():
        _fail("password command executable must be an absolute path")
    executable = Path(parts[0]).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        _fail("password command executable is not available")
    parts[0] = os.fspath(executable)
    encoded = io.StringIO()
    csv.writer(encoded, delimiter=" ", lineterminator="").writerow(parts)
    value = encoded.getvalue()
    if not value or len(value) > 8192:
        _fail("encoded password command exceeds the local safety limit")
    return value


def _rclone_environment() -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", os.fspath(Path.home())),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    if "TMPDIR" in os.environ:
        environment["TMPDIR"] = os.environ["TMPDIR"]
    return environment


def _run_local_rclone(
    rclone: Path,
    config_path: Path,
    password_command: str,
    arguments: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    command = [
        os.fspath(rclone),
        "--config",
        os.fspath(config_path),
        "--password-command",
        password_command,
        "--ask-password=false",
        *arguments,
    ]
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=_rclone_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise CloudAccountError("rclone local configuration check timed out") from error


def doctor_registry(
    registry: CloudRegistry,
    *,
    rclone_binary: str,
    password_command: Sequence[str],
) -> tuple[int, int]:
    config_path, config_before = _validate_private_file(
        registry.rclone_config, "encrypted rclone config", MAX_RCLONE_CONFIG_BYTES
    )
    rclone = _resolve_executable(rclone_binary)
    checked_password_command = _validate_password_command(password_command)
    encryption = _run_local_rclone(
        rclone, config_path, checked_password_command, ["config", "encryption", "check"]
    )
    if encryption.returncode != 0:
        _fail("rclone config encryption check failed; plaintext configs are refused")
    remotes_result = _run_local_rclone(
        rclone, config_path, checked_password_command, ["listremotes", "--json"]
    )
    if remotes_result.returncode != 0:
        _fail("rclone could not list configured remote aliases and backend types")
    try:
        remote_records = json.loads(remotes_result.stdout)
    except json.JSONDecodeError as error:
        raise CloudAccountError("rclone returned an invalid remote inventory") from error
    if not isinstance(remote_records, list) or len(remote_records) > 256:
        _fail("rclone returned an unexpected remote inventory")
    configured: dict[str, str] = {}
    for record in remote_records:
        if not isinstance(record, dict):
            _fail("rclone returned an unexpected remote inventory entry")
        _require_exact_keys(
            record,
            {"name", "type", "source", "description"},
            "rclone remote inventory entry",
        )
        remote = record.get("name")
        backend = record.get("type")
        source = record.get("source")
        description = record.get("description")
        if not isinstance(remote, str) or not SAFE_REMOTE_RE.fullmatch(remote):
            _fail("rclone returned an invalid remote alias")
        if not isinstance(backend, str) or not SAFE_BACKEND_RE.fullmatch(backend):
            _fail("rclone returned an invalid backend type")
        if source != "file":
            _fail("rclone returned a remote outside the encrypted config file")
        if (
            not isinstance(description, str)
            or len(description) > 1024
            or any(ord(character) < 32 for character in description)
        ):
            _fail("rclone returned an invalid remote description")
        if remote in configured:
            _fail("rclone returned a duplicate remote alias")
        configured[remote] = backend
    enabled_accounts = tuple(account for account in registry.accounts if account.enabled)
    enabled = {account.remote for account in enabled_accounts}
    missing = sorted(enabled - set(configured))
    if missing:
        _fail(
            "one or more enabled account remotes are not configured in rclone "
            f"({len(missing)} missing)"
        )
    mismatched = [
        account
        for account in enabled_accounts
        if configured[account.remote] != account.backend
    ]
    if mismatched:
        _fail(
            "one or more enabled account remotes use a different rclone backend "
            f"({len(mismatched)} mismatch(es))"
        )
    config_path_after, config_after = _validate_private_file(
        registry.rclone_config, "encrypted rclone config", MAX_RCLONE_CONFIG_BYTES
    )
    if config_path_after != config_path or not _same_file_state(config_before, config_after):
        _fail("encrypted rclone config changed during the offline doctor")
    return len(enabled), len(configured)


def render_providers(*, as_json: bool = False) -> str:
    """Render the provider table. This is local-only and contacts nothing."""

    if as_json:
        return json.dumps(
            [
                {
                    "name": provider.name,
                    "display_name": provider.display_name,
                    "backend": provider.backend,
                    "credential": provider.credential,
                    "revocation": provider.revocation,
                    "read_only_option": provider.read_only_option,
                    "supports_read_only_enrollment": (
                        provider.supports_read_only_enrollment
                    ),
                    "notes": list(provider.notes),
                }
                for provider in PROVIDERS
            ],
            indent=2,
            sort_keys=True,
        )
    blocks: list[str] = []
    for provider in PROVIDERS:
        read_only = provider.read_only_option or "unavailable for this backend"
        lines = [
            f"{provider.name} ({provider.display_name})",
            f"  rclone backend : {provider.backend}",
            f"  credential     : {provider.credential}",
            f"  read-only      : {read_only}",
            f"  revoke at      : {provider.revocation}",
        ]
        lines.extend(f"  note           : {note}" for note in provider.notes)
        blocks.append("\n".join(lines))
    blocks.append(
        "All providers are inventory-only. Enrollment scope is chosen once, at "
        "rclone config time; Snowbridge cannot narrow it afterwards."
    )
    return "\n\n".join(blocks)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage a private, inventory-only Snowbridge cloud account registry."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create an owner-only empty registry.")
    init_parser.add_argument("--rclone-config", type=Path, default=DEFAULT_RCLONE_CONFIG)

    subparsers.add_parser("validate", help="Validate the owner-only local registry.")

    providers_parser = subparsers.add_parser(
        "providers",
        help="List the onboarded cloud providers and their enrollment permissions.",
    )
    providers_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the provider table as JSON.",
    )

    doctor_parser = subparsers.add_parser(
        "doctor", help="Check encryption and remote aliases without contacting cloud storage."
    )
    doctor_parser.add_argument("--rclone", default="rclone")
    doctor_parser.add_argument(
        "--password-command-executable",
        type=Path,
        help="Absolute executable for the local rclone config-password provider.",
    )
    doctor_parser.add_argument(
        "--password-command-argument",
        action="append",
        default=None,
        help="One literal password-provider argument; repeat for each argument.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            created = initialize_registry(arguments.config, arguments.rclone_config)
            print(f"created owner-only cloud account registry: {created}")
            return 0
        if arguments.command == "providers":
            print(render_providers(as_json=arguments.as_json))
            return 0
        registry = load_registry(arguments.config)
        if arguments.command == "validate":
            enabled = sum(account.enabled for account in registry.accounts)
            print(
                "valid inventory-only cloud account registry "
                f"({len(registry.accounts)} account(s), {enabled} enabled)"
            )
            for line in describe_accounts(registry.accounts):
                print(f"  {line}")
            return 0
        if (
            arguments.password_command_executable is None
            and arguments.password_command_argument is None
            and sys.platform == "darwin"
        ):
            password_command = MACOS_PASSWORD_COMMAND
        else:
            if arguments.password_command_executable is None:
                _fail("doctor requires --password-command-executable on this platform")
            password_command = (
                os.fspath(arguments.password_command_executable),
                *(arguments.password_command_argument or ()),
            )
        enabled, configured = doctor_registry(
            registry,
            rclone_binary=arguments.rclone,
            password_command=password_command,
        )
        print(
            "cloud account doctor passed without storage-backend access "
            f"({enabled} enabled account(s), {configured} configured remote(s))"
        )
        for line in describe_accounts(registry.accounts):
            print(f"  {line}")
        return 0
    except (CloudAccountError, OSError, tomllib.TOMLDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
