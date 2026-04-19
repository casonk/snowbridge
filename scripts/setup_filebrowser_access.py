#!/usr/bin/env python3
"""Manage File Browser root and users from a local TOML config."""

from __future__ import annotations

import argparse
import configparser
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn


REPO_ROOT = Path(__file__).resolve().parent.parent
AUTO_PASS_ROOT = REPO_ROOT.parent / "auto-pass"
DEFAULT_CONFIG = REPO_ROOT / "config" / "web" / "filebrowser" / "access.local.toml"
DEFAULT_EXAMPLE = REPO_ROOT / "config" / "web" / "filebrowser" / "access.example.toml"
AUTO_PASS_CONFIG = REPO_ROOT / "config" / "auto-pass.ini"
DEFAULT_WEB_ENV = REPO_ROOT / "config" / "web" / "filebrowser" / "filebrowser.env.local"
DEFAULT_WEB_SETUP = REPO_ROOT / "scripts" / "setup_caddy_filebrowser.sh"
DEFAULT_FILEBROWSER_IMAGE = "docker.io/filebrowser/filebrowser:latest"


class SetupError(RuntimeError):
    pass


@dataclass
class UserSpec:
    username: str
    password: str
    scope: str
    admin: bool


@dataclass
class RuntimeSpec:
    web_env_file: Path
    web_setup_script: Path
    mode: str
    container_runtime: str
    filebrowser_image: str | None
    container_name: str
    share_mount_path: str
    database_path: str
    run_as_account: str
    run_as_group: str | None
    sync_web_env_uid_gid: bool
    restart_strategy: str


@dataclass
class AppSpec:
    root: str


@dataclass
class AuthSpec:
    method: str
    proxy_header: str | None
    proxy_username: str | None
    hide_login_button: bool


ENTRY_NOT_FOUND_MARKERS = (
    "not found",
    "no entry",
    "could not find",
)
DEFAULT_KEEPASS_PROFILE = "infra"


def log(message: str) -> None:
    print(message)


def fail(message: str) -> NoReturn:
    raise SetupError(message)


def _candidate_keepass_entries(entry: str) -> tuple[str, ...]:
    normalized = entry.strip()
    if not normalized:
        return ()
    candidates = [normalized]
    if "/" not in normalized:
        candidates.append(f"snowbridge/{normalized}")
    return tuple(dict.fromkeys(candidates))


def _resolve_keepass_value(
    entry: str,
    field: str,
    profile: str = DEFAULT_KEEPASS_PROFILE,
) -> str:
    """Resolve a single field from a KeePassXC entry via the auto-pass sibling repo."""
    if not entry:
        return ""
    try:
        _src = str(AUTO_PASS_ROOT / "src")
        if _src not in sys.path:
            sys.path.insert(0, _src)
        from auto_pass.envfile import load_config_environment  # noqa: PLC0415
        from auto_pass.keepassxc import KeepassCommandError  # noqa: PLC0415
        from auto_pass.keepassxc import resolve_keepassxc_entry  # noqa: PLC0415
        _ap_env = AUTO_PASS_ROOT / "config" / "auto-pass.env.local"
        if _ap_env.is_file():
            load_config_environment(_ap_env, profile=profile or None)
        last_error: KeepassCommandError | None = None
        for candidate in _candidate_keepass_entries(entry):
            try:
                result = resolve_keepassxc_entry(candidate, attrs_map={"value": field})
            except KeepassCommandError as exc:
                last_error = exc
                lowered = str(exc).lower()
                if any(marker in lowered for marker in ENTRY_NOT_FOUND_MARKERS):
                    continue
                raise
            return result.get("value", "")
        if last_error is not None:
            raise last_error
        return ""
    except Exception as exc:
        raise SetupError(
            f"auto-pass lookup failed for entry {entry!r} field {field!r}: {exc}"
        ) from exc


def _normalize_auto_pass_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")


def load_repo_auto_pass_config() -> dict[str, str]:
    if not AUTO_PASS_CONFIG.is_file():
        return {}
    try:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        with AUTO_PASS_CONFIG.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        fail(f"cannot read auto-pass config {AUTO_PASS_CONFIG}: {exc}")

    defaults: dict[str, str] = {}
    if parser.has_section("auto_pass"):
        profile = parser.get("auto_pass", "profile", fallback="").strip()
        if profile:
            defaults["profile"] = profile
    if parser.has_section("filebrowser"):
        for key, value in parser.items("filebrowser"):
            text = value.strip()
            if text:
                defaults[key] = text
    return defaults


def require_root() -> None:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        fail("run as root for File Browser database and container operations")


def resolve_repo_path(raw_path: str, base: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply File Browser root and user settings from a local TOML config.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Local TOML config to apply. Default: config/web/filebrowser/access.local.toml",
    )
    parser.add_argument(
        "--init-local-configs",
        action="store_true",
        help="Copy the example TOML config to the local-only path if it does not already exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions that would run without changing the host.",
    )
    return parser.parse_args()


def copy_if_missing(source: Path, target: Path, dry_run: bool) -> None:
    if target.exists():
        log(f"keep existing {target}")
        return
    if dry_run:
        log(f"would create {target} from {source}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(0o600)
    log(f"created {target}")


def try_parse_toml_string(value_text: str) -> str | None:
    try:
        parsed = tomllib.loads(f"value = {value_text}\n")
    except tomllib.TOMLDecodeError:
        return None
    value = parsed.get("value")
    return value if isinstance(value, str) else None


def normalize_password_assignment_line(line: str, line_number: int, config_path: Path) -> tuple[str, bool]:
    if "=" not in line:
        return line, False

    key_part, value_part = line.split("=", 1)
    if key_part.strip() != "password":
        return line, False

    parsed = try_parse_toml_string(value_part.strip())
    if parsed is not None:
        return line, False

    if re.search(r"\s+#", value_part):
        fail(
            f"{config_path}:{line_number}: password value is not valid TOML and also contains "
            "an inline comment marker; quote it manually so the intended password is unambiguous"
        )

    stripped = value_part.strip()
    if not stripped:
        fail(f"{config_path}:{line_number}: password value is empty")

    if stripped[0] in ("'", '"'):
        quote = stripped[0]
        if len(stripped) < 2 or stripped[-1] != quote:
            fail(
                f"{config_path}:{line_number}: password value has an unmatched quote and cannot "
                "be normalized safely; close the quote or switch to a plain unquoted password"
            )
        literal_value = stripped[1:-1]
    else:
        literal_value = stripped

    normalized_value = json.dumps(literal_value)
    return f"{key_part}= {normalized_value}", True


def normalize_password_assignments(config_path: Path, dry_run: bool) -> str:
    original_text = config_path.read_text(encoding="utf-8")
    normalized_lines: list[str] = []
    changed_line_numbers: list[int] = []

    for index, line in enumerate(original_text.splitlines(), start=1):
        normalized_line, changed = normalize_password_assignment_line(line, index, config_path)
        normalized_lines.append(normalized_line)
        if changed:
            changed_line_numbers.append(index)

    normalized_text = "\n".join(normalized_lines) + ("\n" if original_text.endswith("\n") else "")

    if changed_line_numbers:
        line_summary = ", ".join(str(number) for number in changed_line_numbers)
        if dry_run:
            log(f"would normalize TOML password strings in {config_path} on line(s): {line_summary}")
        else:
            config_path.write_text(normalized_text, encoding="utf-8")
            log(f"normalized TOML password strings in {config_path} on line(s): {line_summary}")

    return normalized_text


def load_toml_config(config_path: Path, dry_run: bool) -> dict:
    if not config_path.is_file():
        fail(f"config not found: {config_path}")
    try:
        with config_path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        normalized_text = normalize_password_assignments(config_path, dry_run)
        try:
            return tomllib.loads(normalized_text)
        except tomllib.TOMLDecodeError as exc:
            fail(f"{config_path}:{exc.lineno}:{exc.colno}: {exc.msg}")


def parse_runtime_spec(data: dict, config_path: Path) -> RuntimeSpec:
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        fail(f"invalid [runtime] section in {config_path}")
    runtime_base = config_path.parent
    raw_filebrowser_image = runtime.get("filebrowser_image")
    filebrowser_image = (
        str(raw_filebrowser_image).strip()
        if raw_filebrowser_image not in (None, "")
        else None
    )
    return RuntimeSpec(
        web_env_file=resolve_repo_path(
            runtime.get("web_env_file", str(DEFAULT_WEB_ENV.relative_to(REPO_ROOT))),
            runtime_base,
        ),
        web_setup_script=resolve_repo_path(
            runtime.get("web_setup_script", str(DEFAULT_WEB_SETUP.relative_to(REPO_ROOT))),
            runtime_base,
        ),
        mode=str(runtime.get("mode", "private-vpn")),
        container_runtime=str(runtime.get("container_runtime", "auto")),
        filebrowser_image=filebrowser_image,
        container_name=str(runtime.get("container_name", "snowbridge-filebrowser")),
        share_mount_path=str(runtime.get("share_mount_path", "/srv")),
        database_path=str(runtime.get("database_path", "/database/filebrowser.db")),
        run_as_account=str(runtime.get("run_as_account", "snowbridge")),
        run_as_group=(
            str(runtime["run_as_group"])
            if runtime.get("run_as_group") not in (None, "")
            else None
        ),
        sync_web_env_uid_gid=bool(runtime.get("sync_web_env_uid_gid", True)),
        restart_strategy=str(runtime.get("restart_strategy", "recreate")),
    )


def parse_app_spec(data: dict, config_path: Path) -> AppSpec:
    app = data.get("app", {})
    if not isinstance(app, dict):
        fail(f"invalid [app] section in {config_path}")
    return AppSpec(root=str(app.get("root", "/srv")))


def parse_auth_spec(data: dict, config_path: Path, runtime_spec: RuntimeSpec) -> AuthSpec | None:
    auth = data.get("auth")

    if auth is None:
        if runtime_spec.mode != "private-vpn-mtls":
            return None
        return AuthSpec(
            method="proxy",
            proxy_header="X-Snowbridge-Auth-User",
            proxy_username="snowbridge",
            hide_login_button=True,
        )

    if not isinstance(auth, dict):
        fail(f"invalid [auth] section in {config_path}")

    default_method = "proxy" if runtime_spec.mode == "private-vpn-mtls" else "json"
    method = str(auth.get("method", default_method)).strip()
    if not method:
        fail(f"[auth] method is empty in {config_path}")

    proxy_header: str | None = None
    proxy_username: str | None = None
    if method == "proxy":
        proxy_header = str(auth.get("proxy_header", "X-Snowbridge-Auth-User")).strip()
        proxy_username = str(auth.get("proxy_username", "snowbridge")).strip()
        if not proxy_header:
            fail(f"[auth] proxy_header is required when method=proxy in {config_path}")
        if not proxy_username:
            fail(f"[auth] proxy_username is required when method=proxy in {config_path}")

    hide_login_button = bool(auth.get("hide_login_button", method == "proxy"))
    return AuthSpec(
        method=method,
        proxy_header=proxy_header,
        proxy_username=proxy_username,
        hide_login_button=hide_login_button,
    )


def parse_keepass_profile(data: dict, repo_auto_pass: dict[str, str]) -> str:
    value = str(data.get("keepass_profile", "")).strip()
    return value or repo_auto_pass.get("profile", "") or DEFAULT_KEEPASS_PROFILE


def parse_users(
    data: dict,
    config_path: Path,
    *,
    keepass_profile: str,
    repo_auto_pass: dict[str, str],
) -> list[UserSpec]:
    users = data.get("users", [])
    if not isinstance(users, list) or not users:
        fail(f"no [[users]] entries found in {config_path}")
    parsed: list[UserSpec] = []
    for entry in users:
        if not isinstance(entry, dict):
            fail(f"invalid [[users]] entry in {config_path}")
        username = str(entry.get("username", "")).strip()
        password = str(entry.get("password", ""))
        password_entry = str(entry.get("password_keepass_entry", "")).strip()
        if not password_entry:
            fallback_key = f"{_normalize_auto_pass_key(username)}_password_keepass_entry"
            password_entry = repo_auto_pass.get(fallback_key, "")
        scope = str(entry.get("scope", "."))
        admin = bool(entry.get("admin", False))
        if not username:
            fail(f"[[users]] entry missing username in {config_path}")
        if (not password or password.startswith("CHOOSE_") or password.startswith("REPLACE_")) and password_entry:
            password = _resolve_keepass_value(
                password_entry,
                "password",
                keepass_profile,
            )
        if not password or password.startswith("CHOOSE_") or password.startswith("REPLACE_"):
            fail(f"user {username} still has a placeholder password in {config_path}")
        parsed.append(UserSpec(username=username, password=password, scope=scope, admin=admin))
    return parsed


def validate_auth_users(auth_spec: AuthSpec | None, users: list[UserSpec], config_path: Path) -> None:
    if auth_spec is None or auth_spec.method != "proxy":
        return

    usernames = {user.username for user in users}
    if auth_spec.proxy_username not in usernames:
        fail(
            f"[auth] proxy_username {auth_spec.proxy_username!r} is not present in [[users]] "
            f"within {config_path}"
        )


def detect_container_runtime(preferred: str) -> str:
    if preferred != "auto":
        if shutil.which(preferred) is None:
            fail(f"container runtime not found: {preferred}")
        return preferred
    for candidate in ("podman", "docker"):
        if shutil.which(candidate) is not None:
            return candidate
    fail("no supported container runtime found (supported: podman, docker)")


def load_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.is_file():
        fail(f"web env file not found: {env_path}")
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def resolve_filebrowser_image(runtime_spec: RuntimeSpec, env_values: dict[str, str]) -> str:
    if runtime_spec.filebrowser_image:
        return runtime_spec.filebrowser_image

    env_image = env_values.get("FILEBROWSER_IMAGE", "").strip()
    return env_image or DEFAULT_FILEBROWSER_IMAGE


def replace_env_setting(env_path: Path, key: str, value: str, dry_run: bool) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated_lines: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}="):
            updated_lines.append(f"{key}={value}")
            replaced = True
        else:
            updated_lines.append(line)
    if not replaced:
        updated_lines.append(f"{key}={value}")
    if dry_run:
        log(f"would set {key}={value} in {env_path}")
        return
    env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    log(f"set {key}={value} in {env_path}")


def lookup_account_ids(account: str, group_name: str | None) -> tuple[int, int]:
    try:
        passwd_entry = pwd.getpwnam(account)
    except KeyError as exc:
        raise SetupError(f"host account not found: {account}") from exc

    if group_name is None:
        return passwd_entry.pw_uid, passwd_entry.pw_gid

    try:
        group_entry = grp.getgrnam(group_name)
    except KeyError as exc:
        raise SetupError(f"host group not found: {group_name}") from exc
    return passwd_entry.pw_uid, group_entry.gr_gid


def ensure_runtime_host_paths(
    share_root: str,
    db_dir: str,
    config_dir: str,
    uid: int,
    gid: int,
    dry_run: bool,
) -> None:
    path_modes = {
        db_dir: "0750",
        config_dir: "0750",
    }

    for path, mode in path_modes.items():
        run_command(["install", "-d", "-m", mode, "-o", str(uid), "-g", str(gid), path], dry_run)
        run_command(["chown", "-R", f"{uid}:{gid}", path], dry_run)

    share_stat = Path(share_root)
    if not share_stat.exists():
        fail(f"share root does not exist: {share_root}")

    if dry_run:
        log(f"would verify share root exists and is traversable for UID/GID {uid}:{gid}: {share_root}")
        return

    stat_result = share_stat.stat()
    if stat_result.st_uid != uid and stat_result.st_gid != gid:
        run_command(["setfacl", "-m", f"u:{uid}:r-x", share_root], dry_run)
        log(
            f"share root {share_root} is owned by {stat_result.st_uid}:{stat_result.st_gid}; "
            f"added traverse/read ACL for UID {uid}"
        )


def require_host_tools() -> None:
    for command_name in ("install", "chown", "setfacl"):
        if shutil.which(command_name) is None:
            fail(f"required host command not found: {command_name}")


def run_command(command: list[str], dry_run: bool, check: bool = True) -> subprocess.CompletedProcess[str]:
    rendered = " ".join(shlex.quote(part) for part in command)
    if dry_run:
        log(f"$ {rendered}")
        return subprocess.CompletedProcess(command, 0, "", "")
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if check and result.returncode != 0:
        fail(f"command failed with exit code {result.returncode}: {rendered}")
    return result


def container_running(runtime: str, container_name: str, dry_run: bool) -> bool:
    if dry_run:
        return False
    result = subprocess.run(
        [runtime, "inspect", "-f", "{{.State.Running}}", container_name],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip().lower() == "true"


def stop_container_if_running(runtime: str, container_name: str, dry_run: bool) -> bool:
    was_running = container_running(runtime, container_name, dry_run)
    if was_running:
        run_command([runtime, "stop", container_name], dry_run)
    return was_running


def build_filebrowser_run_command(
    runtime: str,
    uid: int,
    gid: int,
    share_root: str,
    db_dir: str,
    config_dir: str,
    image: str,
    mount_path: str,
    extra_args: list[str],
) -> list[str]:
    return [
        runtime,
        "run",
        "--rm",
        "--security-opt",
        "label=disable",
        "--user",
        f"{uid}:{gid}",
        "--mount",
        (
            "type=bind,"
            f"src={share_root},"
            f"dst={mount_path},"
            "relabel=private,"
            "bind-propagation=rslave"
        ),
        "-v",
        f"{db_dir}:/database:Z",
        "-v",
        f"{config_dir}:/config:Z",
        image,
        *extra_args,
    ]


def filebrowser_user_exists(
    runtime: str,
    uid: int,
    gid: int,
    share_root: str,
    db_dir: str,
    config_dir: str,
    image: str,
    mount_path: str,
    database_path: str,
    username: str,
    dry_run: bool,
) -> bool:
    if dry_run:
        return False
    command = build_filebrowser_run_command(
        runtime,
        uid,
        gid,
        share_root,
        db_dir,
        config_dir,
        image,
        mount_path,
        ["users", "find", username, "-d", database_path],
    )
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    return result.returncode == 0


def apply_filebrowser_state(
    runtime: str,
    uid: int,
    gid: int,
    share_root: str,
    db_dir: str,
    config_dir: str,
    runtime_spec: RuntimeSpec,
    app_spec: AppSpec,
    auth_spec: AuthSpec | None,
    users: list[UserSpec],
    dry_run: bool,
) -> None:
    config_set_command = build_filebrowser_run_command(
        runtime,
        uid,
        gid,
        share_root,
        db_dir,
        config_dir,
        runtime_spec.filebrowser_image,
        runtime_spec.share_mount_path,
        ["config", "set", "--root", app_spec.root, "-d", runtime_spec.database_path],
    )
    run_command(config_set_command, dry_run)

    if auth_spec is not None:
        auth_args = [
            "config",
            "set",
            f"--auth.method={auth_spec.method}",
            f"--hideLoginButton={'true' if auth_spec.hide_login_button else 'false'}",
            "-d",
            runtime_spec.database_path,
        ]
        if auth_spec.method == "proxy":
            auth_args.insert(2, f"--auth.header={auth_spec.proxy_header}")

        auth_command = build_filebrowser_run_command(
            runtime,
            uid,
            gid,
            share_root,
            db_dir,
            config_dir,
            runtime_spec.filebrowser_image,
            runtime_spec.share_mount_path,
            auth_args,
        )
        run_command(auth_command, dry_run)

    for user in users:
        exists = filebrowser_user_exists(
            runtime,
            uid,
            gid,
            share_root,
            db_dir,
            config_dir,
            runtime_spec.filebrowser_image,
            runtime_spec.share_mount_path,
            runtime_spec.database_path,
            user.username,
            dry_run,
        )
        if exists:
            extra_args = [
                "users",
                "update",
                user.username,
                "--password",
                user.password,
                "--scope",
                user.scope,
                f"--perm.admin={'true' if user.admin else 'false'}",
                "-d",
                runtime_spec.database_path,
            ]
        else:
            extra_args = [
                "users",
                "add",
                user.username,
                user.password,
                "--scope",
                user.scope,
                f"--perm.admin={'true' if user.admin else 'false'}",
                "-d",
                runtime_spec.database_path,
            ]
        user_command = build_filebrowser_run_command(
            runtime,
            uid,
            gid,
            share_root,
            db_dir,
            config_dir,
            runtime_spec.filebrowser_image,
            runtime_spec.share_mount_path,
            extra_args,
        )
        run_command(user_command, dry_run)


def restart_web_stack(
    runtime_spec: RuntimeSpec,
    runtime: str,
    container_was_running: bool,
    dry_run: bool,
) -> None:
    if runtime_spec.restart_strategy == "none":
        return
    if runtime_spec.restart_strategy == "start":
        if container_was_running:
            run_command([runtime, "start", runtime_spec.container_name], dry_run)
        return
    if runtime_spec.restart_strategy != "recreate":
        fail(
            "invalid runtime.restart_strategy in access config "
            f"(supported: recreate, start, none): {runtime_spec.restart_strategy}"
        )
    setup_command = [str(runtime_spec.web_setup_script), "--mode", runtime_spec.mode, "--recreate"]
    run_command(setup_command, dry_run)


def main() -> int:
    args = parse_args()
    config_path = resolve_repo_path(args.config, REPO_ROOT)

    if args.init_local_configs:
        copy_if_missing(DEFAULT_EXAMPLE, config_path, args.dry_run)
        log("edit the local config, then rerun with sudo to apply it")
        return 0

    require_root()
    require_host_tools()

    config_data = load_toml_config(config_path, args.dry_run)
    repo_auto_pass = load_repo_auto_pass_config()
    runtime_spec = parse_runtime_spec(config_data, config_path)
    app_spec = parse_app_spec(config_data, config_path)
    auth_spec = parse_auth_spec(config_data, config_path, runtime_spec)
    keepass_profile = parse_keepass_profile(config_data, repo_auto_pass)
    users = parse_users(
        config_data,
        config_path,
        keepass_profile=keepass_profile,
        repo_auto_pass=repo_auto_pass,
    )
    validate_auth_users(auth_spec, users, config_path)

    runtime = detect_container_runtime(runtime_spec.container_runtime)
    env_values = load_env_file(runtime_spec.web_env_file)
    runtime_spec.filebrowser_image = resolve_filebrowser_image(runtime_spec, env_values)

    required_env_keys = ("SNOWBRIDGE_SHARE_ROOT", "FILEBROWSER_DB_DIR", "FILEBROWSER_CONFIG_DIR")
    for key in required_env_keys:
        if key not in env_values or not env_values[key]:
            fail(f"missing {key} in {runtime_spec.web_env_file}")

    uid, gid = lookup_account_ids(runtime_spec.run_as_account, runtime_spec.run_as_group)
    if runtime_spec.sync_web_env_uid_gid:
        replace_env_setting(runtime_spec.web_env_file, "SNOWBRIDGE_UID", str(uid), args.dry_run)
        replace_env_setting(runtime_spec.web_env_file, "SNOWBRIDGE_GID", str(gid), args.dry_run)

    share_root = env_values["SNOWBRIDGE_SHARE_ROOT"]
    db_dir = env_values["FILEBROWSER_DB_DIR"]
    config_dir = env_values["FILEBROWSER_CONFIG_DIR"]

    ensure_runtime_host_paths(share_root, db_dir, config_dir, uid, gid, args.dry_run)

    container_was_running = stop_container_if_running(runtime, runtime_spec.container_name, args.dry_run)

    apply_filebrowser_state(
        runtime,
        uid,
        gid,
        share_root,
        db_dir,
        config_dir,
        runtime_spec,
        app_spec,
        auth_spec,
        users,
        args.dry_run,
    )

    restart_web_stack(runtime_spec, runtime, container_was_running, args.dry_run)
    log("applied File Browser root and user state")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
