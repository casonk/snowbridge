#!/usr/bin/env python3
"""Manage the rootless Air File Browser backend without touching SMB or PF.

The public entry point is deliberately narrow: render review artifacts, perform
an explicit rootless bootstrap, start the already-provisioned backend at login,
or report status.  Every Podman operation names both the machine and connection;
the ambient default connection is never used.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config/macos/air-filebrowser.local.toml"
EXAMPLE_CONFIG = REPO_ROOT / "config/macos/air-filebrowser.example.toml"
DEFAULT_OUTPUT = REPO_ROOT / "artifacts/macos-air-filebrowser"

LAUNCHD_LABEL = "io.github.casonk.snowbridge.air-filebrowser"
CONTAINER_NAME = "snowbridge-air-filebrowser"
MANAGED_LABEL = "io.github.casonk.snowbridge.air-filebrowser.managed"
SPEC_LABEL = "io.github.casonk.snowbridge.air-filebrowser.spec-sha256"
PROXY_HEADER = "X-Snowbridge-Auth-User"
PROXY_USERNAME = "snowbridge"
LISTEN_ADDRESS = "127.0.0.1"
LISTEN_PORT = 8080
CONTAINER_PORT = 8080
STATE_MARKER_NAME = "managed-state.json"
DATABASE_NAME = "filebrowser.db"
SETTINGS_NAME = "settings.json"

SUPERVISOR_POLL_SECONDS = 5.0

MAX_CONFIG_BYTES = 128 * 1024
MAX_COMMAND_OUTPUT = 512 * 1024
SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
IMAGE_RE = re.compile(r"^docker\.io/filebrowser/filebrowser@sha256:[0-9a-f]{64}$")

TOP_LEVEL_KEYS = {
    "schema_version",
    "platform",
    "mode",
    "deployment_id",
    "host",
    "podman",
    "launchd",
}
HOST_KEYS = {"listen_address", "listen_port", "share_path", "state_directory"}
PODMAN_KEYS = {"binary", "machine", "connection", "container_name", "image"}
LAUNCHD_KEYS = {"python"}


class AirFileBrowserError(RuntimeError):
    """Fail-closed local configuration or runtime error."""


@dataclass(frozen=True)
class AirFileBrowserConfig:
    deployment_id: str
    listen_address: str
    listen_port: int
    share_path: Path
    state_directory: Path
    podman_binary: Path
    podman_machine: str
    podman_connection: str
    container_name: str
    image: str
    python_binary: Path
    home: Path

    @property
    def database_directory(self) -> Path:
        return self.state_directory / "database"

    @property
    def config_directory(self) -> Path:
        return self.state_directory / "config"

    @property
    def logs_directory(self) -> Path:
        return self.state_directory / "logs"

    @property
    def database_path(self) -> Path:
        return self.database_directory / DATABASE_NAME

    @property
    def settings_path(self) -> Path:
        return self.config_directory / SETTINGS_NAME

    @property
    def state_marker(self) -> Path:
        return self.state_directory / STATE_MARKER_NAME

    @property
    def launchagent_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


@dataclass(frozen=True)
class ContainerState:
    exists: bool
    managed: bool = False
    spec_hash: str = ""
    running: bool = False
    loopback_binding_valid: bool = False


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


def _fail(message: str) -> NoReturn:
    raise AirFileBrowserError(message)


def _require_table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{label} must be a TOML table")
    return value


def _require_exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        _fail(f"{label} is missing required field(s): {', '.join(missing)}")
    if unknown:
        _fail(f"{label} has unsupported field(s): {', '.join(unknown)}")


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    return value


def _require_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(f"{label} must be an integer")
    return value


def _validate_private_file(path: Path, label: str, maximum: int = MAX_CONFIG_BYTES) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path)))
    if lexical.is_symlink():
        _fail(f"{label} must not be a symlink")
    try:
        details = lexical.lstat()
    except FileNotFoundError:
        _fail(f"{label} does not exist: {lexical}")
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        _fail(f"{label} must be a singly-linked regular file")
    if details.st_uid != os.getuid():
        _fail(f"{label} must be owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o077:
        _fail(f"{label} must be owner-only (mode 0600 or stricter)")
    if details.st_size > maximum:
        _fail(f"{label} exceeds the {maximum}-byte limit")
    return lexical.resolve(strict=True)


def _absolute_path(value: object, label: str) -> Path:
    raw = _require_string(value, label)
    if "\x00" in raw or not Path(raw).is_absolute():
        _fail(f"{label} must be an absolute path")
    return Path(os.path.abspath(raw))


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_home_path(path: Path, home: Path, label: str) -> Path:
    canonical_home = home.resolve(strict=True)
    home_details = canonical_home.lstat()
    if home_details.st_uid != os.getuid() or stat.S_IMODE(home_details.st_mode) & 0o022:
        _fail("current home must be current-user-owned and not group/world writable")
    lexical = Path(os.path.abspath(os.fspath(path)))
    if any(character in os.fspath(lexical) for character in ("\n", "\r", ",")):
        _fail(f"{label} contains a character unsafe for a Podman bind mount")
    if not _within(lexical, canonical_home):
        _fail(f"{label} must stay beneath the current user's home directory")
    current = canonical_home
    for part in lexical.relative_to(canonical_home).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            details = current.lstat()
            if stat.S_ISLNK(details.st_mode):
                _fail(f"{label} must not traverse a symlink")
            if details.st_uid != os.getuid():
                _fail(f"{label} must stay beneath current-user-owned paths")
            if stat.S_IMODE(details.st_mode) & 0o022:
                _fail(f"{label} must not traverse a group/world-writable path")
    return lexical


def _require_executable(path: Path, label: str) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError:
        _fail(f"{label} does not exist: {lexical}")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        _fail(f"{label} is not an executable regular file: {path}")
    # Keep the configured stable Homebrew front-door path in launchd/Podman
    # argv while proving that its current target is a real executable. Pinning a
    # transient Cellar version would break the login service after `brew upgrade`.
    return lexical


def load_config(
    path: Path, *, home: Path | None = None, require_binaries: bool = True
) -> AirFileBrowserConfig:
    canonical = _validate_private_file(path, "Air File Browser local config")
    with canonical.open("rb") as handle:
        payload = tomllib.load(handle)
    root = _require_table(payload, "config")
    _require_exact_keys(root, TOP_LEVEL_KEYS, "config")
    if _require_integer(root["schema_version"], "schema_version") != 1:
        _fail("schema_version must be 1")
    if _require_string(root["platform"], "platform") != "macos":
        _fail("platform must be macos")
    if _require_string(root["mode"], "mode") != "rootless-podman":
        _fail("mode must be rootless-podman")

    deployment_id = _require_string(root["deployment_id"], "deployment_id")
    if SAFE_NAME.fullmatch(deployment_id) is None:
        _fail("deployment_id must be a safe lowercase identifier")

    host = _require_table(root["host"], "host")
    podman = _require_table(root["podman"], "podman")
    launchd = _require_table(root["launchd"], "launchd")
    _require_exact_keys(host, HOST_KEYS, "host")
    _require_exact_keys(podman, PODMAN_KEYS, "podman")
    _require_exact_keys(launchd, LAUNCHD_KEYS, "launchd")

    listen_address = _require_string(host["listen_address"], "host.listen_address")
    listen_port = _require_integer(host["listen_port"], "host.listen_port")
    if listen_address != LISTEN_ADDRESS or listen_port != LISTEN_PORT:
        _fail(f"Air File Browser must bind exactly {LISTEN_ADDRESS}:{LISTEN_PORT}")

    selected_home = (home or Path.home()).resolve(strict=True)
    share_path = _validate_home_path(
        _absolute_path(host["share_path"], "host.share_path"), selected_home, "host.share_path"
    )
    state_directory = _validate_home_path(
        _absolute_path(host["state_directory"], "host.state_directory"),
        selected_home,
        "host.state_directory",
    )
    if _within(state_directory, share_path) or _within(share_path, state_directory):
        _fail("share_path and state_directory must be separate, non-nested trees")

    machine = _require_string(podman["machine"], "podman.machine")
    connection = _require_string(podman["connection"], "podman.connection")
    if SAFE_NAME.fullmatch(machine) is None or SAFE_NAME.fullmatch(connection) is None:
        _fail("Podman machine and connection names must be safe lowercase identifiers")
    if machine != connection:
        _fail("Podman machine and connection must be explicitly pinned to the same name")

    container_name = _require_string(podman["container_name"], "podman.container_name")
    if container_name != CONTAINER_NAME:
        _fail(f"podman.container_name must remain {CONTAINER_NAME}")
    image = _require_string(podman["image"], "podman.image")
    if IMAGE_RE.fullmatch(image) is None:
        _fail("podman.image must be the official File Browser image pinned by sha256 digest")

    podman_binary = _absolute_path(podman["binary"], "podman.binary")
    python_binary = _absolute_path(launchd["python"], "launchd.python")
    if require_binaries:
        podman_binary = _require_executable(podman_binary, "Podman binary")
        python_binary = _require_executable(python_binary, "launchd Python binary")

    return AirFileBrowserConfig(
        deployment_id=deployment_id,
        listen_address=listen_address,
        listen_port=listen_port,
        share_path=share_path,
        state_directory=state_directory,
        podman_binary=podman_binary,
        podman_machine=machine,
        podman_connection=connection,
        container_name=container_name,
        image=image,
        python_binary=python_binary,
        home=selected_home,
    )


def _has_extended_acl(path: Path) -> bool:
    if platform.system() != "Darwin":
        return False
    try:
        result = subprocess.run(
            ["/bin/ls", "-lde", os.fspath(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AirFileBrowserError("could not verify directory ACL state") from exc
    if result.returncode != 0 or not result.stdout:
        _fail("could not verify directory ACL state")
    return result.stdout.splitlines()[0].split()[0].endswith("+")


def ensure_owner_directory(path: Path, *, create: bool, home: Path, label: str) -> Path:
    lexical = _validate_home_path(path, home, label)
    if not lexical.exists():
        if not create:
            _fail(f"{label} does not exist: {lexical}")
        lexical.mkdir(mode=0o700, parents=True)
        lexical.chmod(0o700)
    details = lexical.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        _fail(f"{label} must be a real directory")
    if details.st_uid != os.getuid() or details.st_gid != os.getgid():
        _fail(f"{label} must be owned by the current uid/gid")
    if stat.S_IMODE(details.st_mode) != 0o700:
        _fail(f"{label} must have exact mode 0700")
    if _has_extended_acl(lexical):
        _fail(f"{label} must not have an extended ACL")
    return lexical.resolve(strict=True)


def ensure_runtime_directories(config: AirFileBrowserConfig, *, create: bool) -> None:
    ensure_owner_directory(
        config.share_path, create=create, home=config.home, label="Snowbridge share path"
    )
    ensure_owner_directory(
        config.state_directory,
        create=create,
        home=config.home,
        label="File Browser state directory",
    )
    for path, label in (
        (config.database_directory, "File Browser database directory"),
        (config.config_directory, "File Browser config directory"),
        (config.logs_directory, "File Browser log directory"),
    ):
        ensure_owner_directory(path, create=create, home=config.home, label=label)


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    if path.exists() or path.is_symlink():
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            _fail(f"refusing to replace non-regular file: {path}")
        if details.st_uid != os.getuid() or details.st_nlink != 1:
            _fail(f"refusing to replace unowned or multiply-linked file: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _container_spec_payload(config: AirFileBrowserConfig) -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment_id": config.deployment_id,
        "image": config.image,
        "container_name": config.container_name,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "host_publish": f"{LISTEN_ADDRESS}:{LISTEN_PORT}:{CONTAINER_PORT}/tcp",
        "share_path": os.fspath(config.share_path),
        "database_directory": os.fspath(config.database_directory),
        "config_directory": os.fspath(config.config_directory),
        "auth_method": "proxy",
        "auth_header": PROXY_HEADER,
        "proxy_username": PROXY_USERNAME,
        "security": {
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "read_only_root": True,
            "implicit_read_only_tmpfs": False,
            "host_proxy_environment": False,
            "restart_policy": "launchd-supervised",
            "tmpfs": ["/tmp:rw,nodev,nosuid,noexec,size=64m"],
            "userns": f"keep-id:uid={os.getuid()},gid={os.getgid()}",
        },
    }


def container_spec_hash(config: AirFileBrowserConfig) -> str:
    canonical = json.dumps(
        _container_spec_payload(config), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _podman_prefix(config: AirFileBrowserConfig) -> list[str]:
    return [os.fspath(config.podman_binary), "--connection", config.podman_connection]


def _mount(source: Path, destination: str, *, read_only: bool) -> str:
    mode = "ro" if read_only else "rw"
    return f"type=bind,src={source},dst={destination},{mode}"


def _keep_id_userns() -> str:
    return f"keep-id:uid={os.getuid()},gid={os.getgid()}"


def build_filebrowser_config_command(
    config: AirFileBrowserConfig, *, initialize: bool
) -> list[str]:
    action = "init" if initialize else "set"
    return [
        *_podman_prefix(config),
        "run",
        "--rm",
        "--pull=never",
        f"--user={os.getuid()}:{os.getgid()}",
        f"--userns={_keep_id_userns()}",
        "--read-only",
        "--read-only-tmpfs=false",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--network=none",
        "--http-proxy=false",
        "--tmpfs=/tmp:rw,nodev,nosuid,noexec,size=64m",
        f"--mount={_mount(config.share_path, '/srv', read_only=True)}",
        f"--mount={_mount(config.database_directory, '/database', read_only=False)}",
        f"--mount={_mount(config.config_directory, '/config', read_only=True)}",
        "--entrypoint=/bin/filebrowser",
        config.image,
        "config",
        action,
        "--database=/database/filebrowser.db",
        "--root=/srv",
        "--address=0.0.0.0",
        "--port=8080",
        "--auth.method=proxy",
        f"--auth.header={PROXY_HEADER}",
        "--hideLoginButton=true",
        "--signup=false",
        "--disableExec=true",
        "--followExternalSymlinks=false",
        "--perm.admin=false",
        "--perm.execute=false",
        "--perm.create=true",
        "--perm.rename=true",
        "--perm.modify=true",
        "--perm.delete=true",
        "--perm.share=false",
        "--perm.download=true",
    ]


def build_container_create_command(config: AirFileBrowserConfig) -> list[str]:
    spec_hash = container_spec_hash(config)
    return [
        *_podman_prefix(config),
        "create",
        f"--name={config.container_name}",
        "--pull=never",
        "--restart=no",
        f"--user={os.getuid()}:{os.getgid()}",
        f"--userns={_keep_id_userns()}",
        "--read-only",
        "--read-only-tmpfs=false",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--http-proxy=false",
        "--pids-limit=256",
        "--memory=512m",
        "--tmpfs=/tmp:rw,nodev,nosuid,noexec,size=64m",
        f"--publish={LISTEN_ADDRESS}:{LISTEN_PORT}:{CONTAINER_PORT}/tcp",
        f"--mount={_mount(config.share_path, '/srv', read_only=False)}",
        f"--mount={_mount(config.database_directory, '/database', read_only=False)}",
        f"--mount={_mount(config.config_directory, '/config', read_only=True)}",
        "--env=FB_ADDRESS=0.0.0.0",
        "--env=FB_PORT=8080",
        f"--label={MANAGED_LABEL}=true",
        f"--label={SPEC_LABEL}={spec_hash}",
        config.image,
    ]


def generate_launchagent(config: AirFileBrowserConfig, config_path: Path) -> bytes:
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            os.fspath(config.python_binary),
            os.fspath(Path(__file__).resolve()),
            "--config",
            os.fspath(config_path.resolve(strict=True)),
            "serve",
        ],
        "WorkingDirectory": os.fspath(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ExitTimeOut": 15,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": os.fspath(config.logs_directory / "launchd.stdout.log"),
        "StandardErrorPath": os.fspath(config.logs_directory / "launchd.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _ensure_private_output(path: Path, *, enforce_repo_boundary: bool) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path)))
    if lexical.is_symlink():
        _fail("render output must not be a symlink")
    if enforce_repo_boundary:
        artifacts = (REPO_ROOT / "artifacts").resolve(strict=False)
        if not _within(lexical.resolve(strict=False), artifacts):
            _fail("render output must remain beneath the ignored artifacts directory")
    if not lexical.exists():
        lexical.mkdir(parents=True, mode=0o700)
        lexical.chmod(0o700)
    details = lexical.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        _fail("render output must be a current-user-owned real directory")
    if stat.S_IMODE(details.st_mode) != 0o700:
        _fail("render output must have exact mode 0700")
    return lexical.resolve(strict=True)


def render_bundle(
    config: AirFileBrowserConfig,
    config_path: Path,
    output: Path,
    *,
    enforce_repo_boundary: bool = True,
) -> tuple[Path, Path, Path]:
    destination = _ensure_private_output(output, enforce_repo_boundary=enforce_repo_boundary)
    plist_path = destination / f"{LAUNCHD_LABEL}.plist"
    spec_path = destination / "container-spec.json"
    manifest_path = destination / "manifest.json"
    spec = _container_spec_payload(config)
    spec["spec_sha256"] = container_spec_hash(config)
    spec["reviewed_create_argv"] = build_container_create_command(config)
    spec["reviewed_database_init_argv"] = build_filebrowser_config_command(config, initialize=True)
    manifest = {
        "schema_version": 1,
        "deployment_id": config.deployment_id,
        "platform": "macos",
        "activation": "explicit-bootstrap",
        "requires_root": False,
        "touches_smb_or_pf": False,
        "podman": {
            "machine": config.podman_machine,
            "connection": config.podman_connection,
            "ambient_connection_allowed": False,
        },
        "backend": {
            "host": LISTEN_ADDRESS,
            "port": LISTEN_PORT,
            "direct_wireguard_bind": False,
            "direct_public_bind": False,
            "expected_edge": "wiring-harness snowbridge mTLS role on 10.99.0.254:8444",
        },
        "authentication": {
            "method": "proxy",
            "header": PROXY_HEADER,
            "header_value": PROXY_USERNAME,
            "edge_must_overwrite_client_header": True,
            "password_material": "none",
        },
        "launchd": {
            "label": LAUNCHD_LABEL,
            "live_path": os.fspath(config.launchagent_path),
            "run_at_load": True,
            "starts_named_machine_and_container": True,
            "supervises_container_exit": True,
            "persistent_health_supervisor": True,
            "termination_stops_managed_container": True,
            "runtime_or_health_loss_exits_unsuccessfully": True,
        },
        "bootstrap_mutations": [
            "create owner-only share and state directories when absent",
            "start only the configured rootless Podman machine",
            "pull only the digest-pinned official image when absent",
            "initialize or reconcile the dedicated File Browser database",
            "create or replace only the labeled Snowbridge container",
            "install or reconcile only the labeled user LaunchAgent",
        ],
    }
    _atomic_write(plist_path, generate_launchagent(config, config_path))
    _atomic_write(spec_path, _json_bytes(spec))
    _atomic_write(manifest_path, _json_bytes(manifest))
    return manifest_path, spec_path, plist_path


class CommandRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        acceptable: set[int] | None = None,
        timeout: float | None = 60,
    ) -> subprocess.CompletedProcess[str]:
        accepted = acceptable or {0}
        try:
            result = subprocess.run(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={
                    "HOME": os.fspath(Path.home()),
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    "LC_ALL": "C",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AirFileBrowserError("a required local runtime command could not run") from exc
        if len(result.stdout) + len(result.stderr) > MAX_COMMAND_OUTPUT:
            _fail("a local runtime command exceeded the output safety limit")
        if result.returncode not in accepted:
            _fail("a required local runtime command failed; inspect owner-only service logs")
        return result


class PodmanManager:
    def __init__(self, config: AirFileBrowserConfig, runner: CommandRunner | None = None):
        self.config = config
        self.runner = runner or CommandRunner()

    def _podman(self, *arguments: str) -> list[str]:
        return [*_podman_prefix(self.config), *arguments]

    @staticmethod
    def _json(output: str, label: str) -> object:
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise AirFileBrowserError(f"{label} returned invalid JSON") from exc

    def machine_state(self) -> str:
        result = self.runner.run(
            [
                os.fspath(self.config.podman_binary),
                "machine",
                "inspect",
                self.config.podman_machine,
            ],
            timeout=20,
        )
        payload = self._json(result.stdout, "Podman machine inspect")
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict):
            _fail("Podman machine inspect returned an unexpected document")
        if item.get("Rootful") is not False:
            _fail("configured Podman machine must remain rootless")
        state = item.get("State")
        if not isinstance(state, str):
            _fail("Podman machine inspect omitted State")
        return state.lower()

    def verify_connection(self) -> None:
        result = self.runner.run(
            [
                os.fspath(self.config.podman_binary),
                "system",
                "connection",
                "list",
                "--format",
                "json",
            ],
            timeout=20,
        )
        payload = self._json(result.stdout, "Podman connection list")
        if not isinstance(payload, list):
            _fail("Podman connection list returned an unexpected document")
        names = {item.get("Name") for item in payload if isinstance(item, dict)}
        if self.config.podman_connection not in names:
            _fail("configured Podman connection does not exist")

    def ensure_machine_running(self) -> None:
        self.verify_connection()
        state = self.machine_state()
        if state == "running":
            return
        if state not in {"stopped", "exited"}:
            _fail(f"Podman machine is in unsupported state: {state}")
        self.runner.run(
            [
                os.fspath(self.config.podman_binary),
                "machine",
                "start",
                "--update-connection=false",
                self.config.podman_machine,
            ],
            timeout=120,
        )
        if self.machine_state() != "running":
            _fail("Podman machine did not reach running state")
        self.runner.run(self._podman("info", "--format", "json"), timeout=30)

    def image_exists(self) -> bool:
        result = self.runner.run(
            self._podman("image", "exists", self.config.image),
            acceptable={0, 1},
            timeout=20,
        )
        return result.returncode == 0

    def ensure_image(self) -> None:
        if not self.image_exists():
            self.runner.run(self._podman("pull", self.config.image), timeout=300)
        if not self.image_exists():
            _fail("digest-pinned File Browser image is unavailable after pull")

    def container_state(self) -> ContainerState:
        exists = self.runner.run(
            self._podman("container", "exists", self.config.container_name),
            acceptable={0, 1},
            timeout=20,
        )
        if exists.returncode != 0:
            return ContainerState(exists=False)
        result = self.runner.run(
            self._podman("container", "inspect", self.config.container_name), timeout=20
        )
        payload = self._json(result.stdout, "Podman container inspect")
        item = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(item, dict):
            _fail("Podman container inspect returned an unexpected document")
        config = item.get("Config")
        state = item.get("State")
        network = item.get("NetworkSettings")
        labels = config.get("Labels", {}) if isinstance(config, dict) else {}
        running = bool(state.get("Running")) if isinstance(state, dict) else False
        ports = network.get("Ports", {}) if isinstance(network, dict) else {}
        binding = ports.get(f"{CONTAINER_PORT}/tcp") if isinstance(ports, dict) else None
        binding_ok = False
        if isinstance(binding, list) and len(binding) == 1 and isinstance(binding[0], dict):
            binding_ok = binding[0].get("HostIp") == LISTEN_ADDRESS and str(
                binding[0].get("HostPort")
            ) == str(LISTEN_PORT)
        return ContainerState(
            exists=True,
            managed=isinstance(labels, dict) and labels.get(MANAGED_LABEL) == "true",
            spec_hash=labels.get(SPEC_LABEL, "") if isinstance(labels, dict) else "",
            running=running,
            loopback_binding_valid=binding_ok,
        )

    def stop_managed_container(self) -> ContainerState:
        state = self.container_state()
        if not state.exists:
            return state
        if not state.managed:
            _fail("refusing to mutate an existing container without the Snowbridge managed label")
        if state.running:
            self.runner.run(self._podman("stop", "--time=10", self.config.container_name))
        return self.container_state()

    def reconcile_container(self, *, replace_stale: bool) -> None:
        expected = container_spec_hash(self.config)
        state = self.container_state()
        if state.exists and not state.managed:
            _fail("refusing to use an existing container without the Snowbridge managed label")
        if state.exists and state.spec_hash != expected:
            if not replace_stale:
                _fail("managed container spec is stale; run the explicit bootstrap command")
            if state.running:
                self.runner.run(self._podman("stop", "--time=10", self.config.container_name))
            self.runner.run(self._podman("rm", self.config.container_name))
            state = ContainerState(exists=False)
        if not state.exists:
            self.runner.run(build_container_create_command(self.config))
        current = self.container_state()
        if not current.managed or current.spec_hash != expected:
            _fail("created container does not match the reviewed Snowbridge spec")
        if not current.running:
            self.runner.run(self._podman("start", self.config.container_name))
        final = self.container_state()
        if not final.running or not final.loopback_binding_valid:
            _fail("File Browser container did not start on the exact loopback binding")


def _settings_payload() -> bytes:
    return _json_bytes(
        {
            "address": "0.0.0.0",
            "baseURL": "",
            "database": "/database/filebrowser.db",
            "log": "stdout",
            "port": 8080,
            "root": "/srv",
        }
    )


def _validate_managed_file(path: Path, label: str) -> Path:
    return _validate_private_file(path, label, maximum=4 * 1024 * 1024)


def _state_marker_payload(config: AirFileBrowserConfig) -> dict[str, object]:
    return {
        "schema_version": 1,
        "managed_by": LAUNCHD_LABEL,
        "deployment_id": config.deployment_id,
        "image": config.image,
        "auth_method": "proxy",
        "auth_header": PROXY_HEADER,
        "proxy_username": PROXY_USERNAME,
        "password_material": "none",
    }


def validate_managed_state(config: AirFileBrowserConfig) -> None:
    marker = _validate_managed_file(config.state_marker, "File Browser state marker")
    database = _validate_managed_file(config.database_path, "File Browser database")
    settings = _validate_managed_file(config.settings_path, "File Browser settings")
    del database, settings
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AirFileBrowserError("File Browser state marker is invalid") from exc
    if payload != _state_marker_payload(config):
        _fail("File Browser state marker does not match the reviewed local config")


def provision_database(config: AirFileBrowserConfig, manager: PodmanManager) -> None:
    ensure_runtime_directories(config, create=True)
    if config.state_marker.exists():
        marker = _validate_private_file(config.state_marker, "File Browser state marker")
        try:
            existing_marker = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AirFileBrowserError("File Browser state marker is invalid") from exc
        if (
            not isinstance(existing_marker, dict)
            or existing_marker.get("managed_by") != LAUNCHD_LABEL
        ):
            _fail("refusing unrecognized File Browser state")
    elif config.database_path.exists() or config.settings_path.exists():
        _fail("refusing pre-existing File Browser state without the Snowbridge marker")

    if config.database_path.exists() or config.database_path.is_symlink():
        details = config.database_path.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.getuid()
        ):
            _fail("existing File Browser database is not a safe managed file")
        # File Browser may create the database before a later initialization
        # step fails. Tightening its mode is safe only after the owner-only
        # Snowbridge marker above has claimed this dedicated state tree.
        os.chmod(config.database_path, 0o600)
        _validate_managed_file(config.database_path, "existing File Browser database")

    stopped = manager.stop_managed_container()
    del stopped
    _atomic_write(config.settings_path, _settings_payload())
    # Claim this dedicated state before invoking the one-shot initializer. If
    # the process is interrupted after creating the database, a later bootstrap
    # can safely recognize and reconcile its own partial state instead of
    # requiring an unsafe manual deletion.
    _atomic_write(config.state_marker, _json_bytes(_state_marker_payload(config)))
    initialize = not config.database_path.exists()
    manager.runner.run(build_filebrowser_config_command(config, initialize=initialize), timeout=120)
    if not config.database_path.exists():
        _fail("File Browser did not create its managed database")
    os.chmod(config.database_path, 0o600)
    _validate_managed_file(config.database_path, "File Browser database")
    validate_managed_state(config)


def probe_backend(timeout: float = 2.0) -> bool:
    connection = http.client.HTTPConnection(LISTEN_ADDRESS, LISTEN_PORT, timeout=timeout)
    try:
        connection.request("GET", "/health", headers={"Host": LISTEN_ADDRESS})
        response = connection.getresponse()
        response.read(4096)
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        connection.close()


def wait_for_backend(
    seconds: float = 20.0,
    *,
    stop_event: StopEvent | None = None,
    health_probe: Callable[[float], bool] = probe_backend,
) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        if health_probe(1.0):
            return True
        if stop_event is None:
            time.sleep(0.25)
        elif stop_event.wait(0.25):
            return False
    _fail("File Browser loopback health check did not become ready")


def _supervision_failure(config: AirFileBrowserConfig, state: ContainerState) -> str | None:
    if not state.exists:
        return "managed File Browser container disappeared"
    if not state.managed:
        return "File Browser container lost the Snowbridge managed label"
    if state.spec_hash != container_spec_hash(config):
        return "managed File Browser container spec changed"
    if not state.running:
        return "managed File Browser container stopped"
    if not state.loopback_binding_valid:
        return "managed File Browser container lost its exact loopback binding"
    return None


def _stop_then_fail(
    manager: PodmanManager,
    message: str,
    *,
    cause: BaseException | None = None,
) -> NoReturn:
    try:
        manager.stop_managed_container()
    except AirFileBrowserError as stop_error:
        raise AirFileBrowserError(
            f"{message}; the managed container could not be confirmed stopped"
        ) from stop_error
    if cause is None:
        _fail(message)
    raise AirFileBrowserError(message) from cause


def supervise_backend(
    manager: PodmanManager,
    *,
    stop_event: StopEvent,
    health_probe: Callable[[], bool] = probe_backend,
    poll_seconds: float = SUPERVISOR_POLL_SECONDS,
) -> None:
    """Own the managed container until termination or a fail-closed restart.

    ``Event.wait`` keeps termination prompt without a sleeping polling process.
    A launchd restart is requested by raising on any runtime or health loss;
    graceful SIGTERM/SIGINT stops only the labeled managed container and returns.
    """
    if poll_seconds <= 0:
        _fail("supervisor poll interval must be positive")

    while not stop_event.is_set():
        try:
            state = manager.container_state()
        except AirFileBrowserError as error:
            _stop_then_fail(manager, "Podman runtime supervision failed", cause=error)
        failure = _supervision_failure(manager.config, state)
        if failure is not None:
            _stop_then_fail(manager, failure)
        try:
            healthy = health_probe()
        except Exception as error:
            _stop_then_fail(manager, "File Browser health probe failed", cause=error)
        if not healthy:
            _stop_then_fail(manager, "File Browser loopback health check failed")
        if stop_event.wait(poll_seconds):
            break

    manager.stop_managed_container()


@contextlib.contextmanager
def _termination_event() -> Iterator[threading.Event]:
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    previous_handlers: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, request_stop)
    try:
        yield stop_event
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _launchctl_loaded(runner: CommandRunner) -> bool:
    domain = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    result = runner.run(
        ["/bin/launchctl", "print", domain], acceptable={0, 1, 3, 64, 113}, timeout=20
    )
    return result.returncode == 0


def install_launchagent(
    config: AirFileBrowserConfig,
    config_path: Path,
    runner: CommandRunner | None = None,
) -> Path:
    command_runner = runner or CommandRunner()
    directory = config.launchagent_path.parent
    if directory.is_symlink():
        _fail("Library/LaunchAgents must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = directory.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        _fail("Library/LaunchAgents must be a current-user-owned directory")

    target = config.launchagent_path
    new_payload = generate_launchagent(config, config_path)
    old_payload: bytes | None = None
    if target.exists() or target.is_symlink():
        existing = _validate_private_file(
            target, "existing Snowbridge LaunchAgent", maximum=1024 * 1024
        )
        old_payload = existing.read_bytes()
        try:
            parsed = plistlib.loads(old_payload)
        except plistlib.InvalidFileException as exc:
            raise AirFileBrowserError("existing Snowbridge LaunchAgent is invalid") from exc
        if parsed.get("Label") != LAUNCHD_LABEL:
            _fail("existing LaunchAgent does not carry the managed Snowbridge label")

    loaded = _launchctl_loaded(command_runner)
    domain = f"gui/{os.getuid()}"
    if loaded:
        command_runner.run(
            ["/bin/launchctl", "bootout", domain, os.fspath(target)],
            acceptable={0, 3, 64, 113},
            timeout=20,
        )
    try:
        _atomic_write(target, new_payload)
        command_runner.run(["/bin/launchctl", "enable", f"{domain}/{LAUNCHD_LABEL}"])
        command_runner.run(["/bin/launchctl", "bootstrap", domain, os.fspath(target)])
    except AirFileBrowserError:
        if old_payload is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write(target, old_payload)
            if loaded:
                command_runner.run(
                    ["/bin/launchctl", "bootstrap", domain, os.fspath(target)],
                    acceptable={0, 5},
                )
        raise
    return target


def quiesce_launchagent(config: AirFileBrowserConfig, runner: CommandRunner) -> None:
    if not _launchctl_loaded(runner):
        return
    target = _validate_private_file(
        config.launchagent_path,
        "loaded Snowbridge LaunchAgent",
        maximum=1024 * 1024,
    )
    try:
        payload = plistlib.loads(target.read_bytes())
    except plistlib.InvalidFileException as exc:
        raise AirFileBrowserError("loaded Snowbridge LaunchAgent is invalid") from exc
    if payload.get("Label") != LAUNCHD_LABEL:
        _fail("loaded LaunchAgent does not carry the managed Snowbridge label")
    runner.run(
        ["/bin/launchctl", "bootout", f"gui/{os.getuid()}", os.fspath(target)],
        acceptable={0, 3, 64, 113},
        timeout=20,
    )


def bootstrap(config: AirFileBrowserConfig, config_path: Path) -> None:
    if platform.system() != "Darwin":
        _fail("Air File Browser bootstrap is supported only on macOS")
    if os.getuid() == 0:
        _fail("Air File Browser bootstrap must run as the login user, never root or sudo")
    ensure_runtime_directories(config, create=True)
    runner = CommandRunner()
    quiesce_launchagent(config, runner)
    manager = PodmanManager(config, runner=runner)
    manager.ensure_machine_running()
    manager.ensure_image()
    provision_database(config, manager)
    manager.reconcile_container(replace_stale=True)
    wait_for_backend()
    installed = install_launchagent(config, config_path, runner=runner)
    print(f"Air File Browser is healthy on {LISTEN_ADDRESS}:{LISTEN_PORT}")
    print(f"installed rootless login service: {installed}")
    print("SMB, the Public Folder, PF, WireGuard, and the mTLS edge were not changed")


def serve(config: AirFileBrowserConfig) -> None:
    if platform.system() != "Darwin" or os.getuid() == 0:
        _fail("the Air File Browser login runner requires an unprivileged macOS user")
    ensure_runtime_directories(config, create=False)
    validate_managed_state(config)
    manager = PodmanManager(config)
    with _termination_event() as stop_event:
        manager.ensure_machine_running()
        if not manager.image_exists():
            _fail("digest-pinned image is absent; run bootstrap while network access is available")
        manager.reconcile_container(replace_stale=False)
        if stop_event.is_set():
            manager.stop_managed_container()
            return
        if not wait_for_backend(stop_event=stop_event):
            manager.stop_managed_container()
            return
        supervise_backend(manager, stop_event=stop_event)


def status(config: AirFileBrowserConfig) -> tuple[dict[str, object], bool]:
    report: dict[str, object] = {
        "schema_version": 1,
        "deployment_id": config.deployment_id,
        "expected_backend": f"{LISTEN_ADDRESS}:{LISTEN_PORT}",
        "podman_machine": config.podman_machine,
        "podman_connection": config.podman_connection,
        "paths_ready": False,
        "machine_state": "unknown",
        "image_present": False,
        "container_exists": False,
        "container_running": False,
        "loopback_binding_valid": False,
        "health_ready": False,
        "launchagent_installed": False,
        "launchagent_loaded": False,
        "smb_or_pf_checked": False,
    }
    try:
        ensure_runtime_directories(config, create=False)
        validate_managed_state(config)
        report["paths_ready"] = True
    except AirFileBrowserError:
        pass
    manager = PodmanManager(config)
    try:
        manager.verify_connection()
        machine_state = manager.machine_state()
        report["machine_state"] = machine_state
        if machine_state == "running":
            report["image_present"] = manager.image_exists()
            container = manager.container_state()
            report["container_exists"] = container.exists
            report["container_running"] = container.running
            report["loopback_binding_valid"] = container.loopback_binding_valid
            report["health_ready"] = probe_backend()
    except AirFileBrowserError:
        pass
    target = config.launchagent_path
    if target.exists() and not target.is_symlink():
        try:
            payload = plistlib.loads(target.read_bytes())
            report["launchagent_installed"] = payload.get("Label") == LAUNCHD_LABEL
        except (OSError, plistlib.InvalidFileException):
            pass
    with contextlib.suppress(AirFileBrowserError):
        report["launchagent_loaded"] = _launchctl_loaded(CommandRunner())
    ready = all(
        report[key]
        for key in (
            "paths_ready",
            "image_present",
            "container_running",
            "loopback_binding_valid",
            "health_ready",
            "launchagent_installed",
            "launchagent_loaded",
        )
    )
    return report, ready


def initialize_config(destination: Path, *, home: Path | None = None) -> Path:
    lexical = Path(os.path.abspath(os.fspath(destination)))
    if lexical.exists() or lexical.is_symlink():
        _fail(f"refusing to overwrite existing local config: {lexical}")
    selected_home = (home or Path.home()).resolve(strict=True)
    podman = shutil.which("podman") or "/opt/homebrew/bin/podman"
    python = shutil.which("python3") or os.path.abspath(sys.executable)
    payload = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    payload = payload.replace("/Users/<account>", os.fspath(selected_home))
    payload = payload.replace("<podman-binary>", podman)
    payload = payload.replace("<python-binary>", python)
    lexical.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(lexical, payload.encode("utf-8"))
    return lexical.resolve(strict=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render and manage the rootless Air File Browser Podman backend."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create the ignored owner-only local config.")
    subparsers.add_parser("validate", help="Validate the local contract without mutation.")
    render = subparsers.add_parser("render", help="Render inert owner-only review artifacts.")
    render.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    subparsers.add_parser(
        "bootstrap",
        help="Provision and activate the reviewed rootless container and user LaunchAgent.",
    )
    subparsers.add_parser("serve", help="Internal login runner used by the LaunchAgent.")
    subparsers.add_parser("status", help="Report read-only backend and login-service status.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "init":
            created = initialize_config(arguments.config)
            print(f"created owner-only local config: {created}")
            print("review it before running bootstrap; no service state changed")
            return 0
        config = load_config(arguments.config)
        if arguments.command == "validate":
            print(
                f"valid rootless Air File Browser config: {LISTEN_ADDRESS}:{LISTEN_PORT}, "
                f"Podman {config.podman_connection}"
            )
            return 0
        if arguments.command == "render":
            manifest, spec, plist = render_bundle(config, arguments.config, arguments.output)
            print(f"rendered review manifest: {manifest}")
            print(f"rendered container spec: {spec}")
            print(f"rendered inert LaunchAgent: {plist}")
            print("run bootstrap explicitly after review; no service state changed")
            return 0
        if arguments.command == "bootstrap":
            bootstrap(config, arguments.config.resolve(strict=True))
            return 0
        if arguments.command == "serve":
            serve(config)
            return 0
        report, ready = status(config)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if ready else 3
    except (
        AirFileBrowserError,
        OSError,
        ValueError,
        tomllib.TOMLDecodeError,
        plistlib.InvalidFileException,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
