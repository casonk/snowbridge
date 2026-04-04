#!/usr/bin/env python3
"""Detect WireGuard public-endpoint drift and refresh local client artifacts."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "access" / "wireguard" / "endpoint-monitor.local.toml"
DEFAULT_STATE_PATH = REPO_ROOT / "state" / "wireguard-endpoint-monitor.json"
DEFAULT_PUBLIC_IP_LOOKUP_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)
DEFAULT_GMAIL_CONFIG = Path("services/gmail-imap/config.local.yaml")
DEFAULT_SIGNAL_CONFIG = Path("services/signal-cli/config.local.yaml")
DEFAULT_GMAIL_SCRIPT = Path("services/gmail-imap/send_email.py")
DEFAULT_SIGNAL_SCRIPT = Path("services/signal-cli/send_message.py")
SAMPLE_ENDPOINT_HOSTS = {
    "<wireguard-endpoint-hostname-or-ip>",
    "vpn.example.com",
}
AUTO_PASS_NOTIFY_SUPPRESS_ENV = "AUTO_PASS_NOTIFY_PASSWORD_RETRIEVAL_SUPPRESS"


class MonitorError(RuntimeError):
    """Raised when the endpoint monitor cannot complete safely."""


@dataclass(frozen=True)
class EndpointValue:
    host: str
    port: int | None = None

    def render(self) -> str:
        host = self.host
        try:
            parsed = ipaddress.ip_address(host)
        except ValueError:
            parsed = None

        if parsed and parsed.version == 6:
            host = f"[{host}]"

        if self.port is None:
            return host
        return f"{host}:{self.port}"


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    config_path: Path
    qr_path: Path


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool
    to_address: str
    config_path: Path
    sender_script: Path


@dataclass(frozen=True)
class SignalConfig:
    enabled: bool
    recipient: str
    note_to_self: bool
    config_path: Path
    sender_script: Path


@dataclass(frozen=True)
class MonitorConfig:
    config_path: Path
    host_label: str
    shock_relay_root: Path
    state_file: Path
    public_ip_lookup_urls: tuple[str, ...]
    profiles: tuple[ProfileConfig, ...]
    email: EmailConfig
    signal: SignalConfig


@dataclass
class ProfileOutcome:
    name: str
    config_path: Path
    qr_path: Path
    old_endpoint: str
    new_endpoint: str
    changed: bool


@dataclass
class MonitorOutcome:
    public_ip: str
    endpoint_changed: bool
    profile_outcomes: list[ProfileOutcome]
    regenerated_qrs: list[Path] = field(default_factory=list)
    email_sent: bool = False
    signal_sent: bool = False
    email_retry_needed: bool = False
    signal_retry_needed: bool = False

    @property
    def current_endpoint(self) -> str:
        for outcome in self.profile_outcomes:
            if outcome.new_endpoint:
                return outcome.new_endpoint
        return self.public_ip


def _resolve_repo_path(path_value: str | None, base: Path) -> Path:
    if not path_value:
        return base
    candidate = Path(path_value)
    if candidate.is_absolute():
        return candidate
    return (REPO_ROOT / candidate).resolve()


def _resolve_child_path(root: Path, relative_or_absolute: str | Path) -> Path:
    candidate = Path(relative_or_absolute)
    if candidate.is_absolute():
        return candidate
    return (root / candidate).resolve()


def load_config(path: Path) -> MonitorConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(f"config not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise MonitorError(f"invalid TOML in {path}: {exc}") from exc

    monitor_section = data.get("monitor", {})
    if not isinstance(monitor_section, dict):
        raise MonitorError("[monitor] must be a TOML table")

    shock_relay_root = _resolve_repo_path(
        monitor_section.get("shock_relay_root"),
        (REPO_ROOT.parent / "shock-relay").resolve(),
    )
    state_file = _resolve_repo_path(
        monitor_section.get("state_file"),
        DEFAULT_STATE_PATH,
    )

    public_ip_lookup_urls = monitor_section.get("public_ip_lookup_urls", list(DEFAULT_PUBLIC_IP_LOOKUP_URLS))
    if not isinstance(public_ip_lookup_urls, list) or not public_ip_lookup_urls:
        raise MonitorError("[monitor].public_ip_lookup_urls must be a non-empty list")

    profiles_section = data.get("profiles")
    if not isinstance(profiles_section, list) or not profiles_section:
        raise MonitorError("config must define at least one [[profiles]] entry")

    profiles: list[ProfileConfig] = []
    for entry in profiles_section:
        if not isinstance(entry, dict):
            raise MonitorError("[[profiles]] entries must be TOML tables")
        try:
            name = str(entry["name"]).strip()
            config_path = _resolve_repo_path(str(entry["config_path"]), DEFAULT_CONFIG_PATH)
            qr_path = _resolve_repo_path(str(entry["qr_path"]), REPO_ROOT / "artifacts" / "wireguard")
        except KeyError as exc:
            raise MonitorError(f"missing required profile key: {exc}") from exc
        if not name:
            raise MonitorError("[[profiles]].name must not be empty")
        profiles.append(ProfileConfig(name=name, config_path=config_path, qr_path=qr_path))

    email_section = data.get("email", {})
    if not isinstance(email_section, dict):
        raise MonitorError("[email] must be a TOML table")
    email = EmailConfig(
        enabled=bool(email_section.get("enabled", False)),
        to_address=str(email_section.get("to", "")).strip(),
        config_path=_resolve_child_path(
            shock_relay_root,
            email_section.get("config_path", DEFAULT_GMAIL_CONFIG),
        ),
        sender_script=_resolve_child_path(
            shock_relay_root,
            email_section.get("sender_script", DEFAULT_GMAIL_SCRIPT),
        ),
    )

    signal_section = data.get("signal", {})
    if not isinstance(signal_section, dict):
        raise MonitorError("[signal] must be a TOML table")
    signal = SignalConfig(
        enabled=bool(signal_section.get("enabled", False)),
        recipient=str(signal_section.get("recipient", "")).strip(),
        note_to_self=bool(signal_section.get("note_to_self", False)),
        config_path=_resolve_child_path(
            shock_relay_root,
            signal_section.get("config_path", DEFAULT_SIGNAL_CONFIG),
        ),
        sender_script=_resolve_child_path(
            shock_relay_root,
            signal_section.get("sender_script", DEFAULT_SIGNAL_SCRIPT),
        ),
    )

    if email.enabled and not email.to_address:
        raise MonitorError("[email].to is required when email notifications are enabled")
    if signal.enabled and not signal.note_to_self and not signal.recipient:
        raise MonitorError("[signal].recipient is required when signal notifications are enabled")

    return MonitorConfig(
        config_path=path,
        host_label=str(monitor_section.get("host_label", "snowbridge")).strip() or "snowbridge",
        shock_relay_root=shock_relay_root,
        state_file=state_file,
        public_ip_lookup_urls=tuple(str(url).strip() for url in public_ip_lookup_urls),
        profiles=tuple(profiles),
        email=email,
        signal=signal,
    )


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise MonitorError(f"invalid JSON in state file {path}: {exc}") from exc


def save_state(path: Path, state: Mapping[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def detect_public_ip(urls: tuple[str, ...]) -> str:
    last_error: Exception | None = None
    for url in urls:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "snowbridge-wireguard-endpoint-monitor/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                candidate = response.read().decode("utf-8").strip()
            ipaddress.ip_address(candidate)
            return candidate
        except Exception as exc:  # pragma: no cover - exercised via CLI/runtime
            last_error = exc
            continue

    if last_error is not None:
        raise MonitorError(f"unable to detect public IP: {last_error}")
    raise MonitorError("unable to detect public IP")


def parse_endpoint_value(value: str) -> EndpointValue:
    stripped = value.strip()
    if not stripped:
        raise MonitorError("WireGuard Endpoint value is empty")

    if stripped.startswith("["):
        closing = stripped.find("]")
        if closing == -1:
            raise MonitorError(f"invalid bracketed Endpoint value: {value}")
        host = stripped[1:closing]
        remainder = stripped[closing + 1 :]
        if not remainder:
            return EndpointValue(host=host, port=None)
        if not remainder.startswith(":"):
            raise MonitorError(f"invalid bracketed Endpoint value: {value}")
        return EndpointValue(host=host, port=int(remainder[1:]))

    if stripped.count(":") == 1:
        host, port_text = stripped.rsplit(":", 1)
        if port_text.isdigit():
            return EndpointValue(host=host, port=int(port_text))

    return EndpointValue(host=stripped, port=None)


def _parse_endpoint_line(line: str) -> tuple[str, EndpointValue, str] | None:
    stripped_newline = line[:-1] if line.endswith("\n") else line
    if "=" not in stripped_newline:
        return None
    left, right = stripped_newline.split("=", 1)
    if left.strip() != "Endpoint":
        return None

    value = right.strip()
    if not value:
        raise MonitorError("WireGuard Endpoint line is empty")

    suffix = "\n" if line.endswith("\n") else ""
    equals_index = stripped_newline.index("=")
    prefix = stripped_newline[: equals_index + 1]
    trailing_spaces = right[: len(right) - len(right.lstrip())]
    return f"{prefix}{trailing_spaces}", parse_endpoint_value(value), suffix


def _is_direct_ip_endpoint(host: str) -> bool:
    if host in SAMPLE_ENDPOINT_HOSTS:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def update_profile_endpoint(profile: ProfileConfig, public_ip: str, *, dry_run: bool) -> ProfileOutcome:
    try:
        lines = profile.config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError as exc:
        raise MonitorError(f"profile config not found: {profile.config_path}") from exc

    for index, line in enumerate(lines):
        parsed = _parse_endpoint_line(line)
        if parsed is None:
            continue
        prefix, endpoint, suffix = parsed
        old_endpoint = endpoint.render()

        if _is_direct_ip_endpoint(endpoint.host):
            new_endpoint = EndpointValue(host=public_ip, port=endpoint.port or 51820).render()
            changed = old_endpoint != new_endpoint
            if changed and not dry_run:
                lines[index] = f"{prefix}{new_endpoint}{suffix}"
                profile.config_path.write_text("".join(lines), encoding="utf-8")
            return ProfileOutcome(
                name=profile.name,
                config_path=profile.config_path,
                qr_path=profile.qr_path,
                old_endpoint=old_endpoint,
                new_endpoint=new_endpoint,
                changed=changed,
            )

        return ProfileOutcome(
            name=profile.name,
            config_path=profile.config_path,
            qr_path=profile.qr_path,
            old_endpoint=old_endpoint,
            new_endpoint=old_endpoint,
            changed=False,
        )

    raise MonitorError(f"profile config has no Endpoint line: {profile.config_path}")


def render_profile_qr(profile: ProfileConfig, *, dry_run: bool) -> None:
    if dry_run:
        return

    profile.qr_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["qrencode", "-o", str(profile.qr_path)],
        input=profile.config_path.read_bytes(),
        check=True,
    )


def _build_notification_subject(host_label: str, endpoint: str) -> str:
    return f"[{host_label}] WireGuard endpoint updated: {endpoint}"


def build_notification_body(host_label: str, outcome: MonitorOutcome) -> str:
    lines = [
        f"{host_label} detected a WireGuard endpoint change.",
        "",
        f"Latest endpoint: {outcome.current_endpoint}",
        "",
        "Profiles:",
    ]
    for profile_outcome in outcome.profile_outcomes:
        if profile_outcome.changed:
            lines.append(
                f"- {profile_outcome.name}: {profile_outcome.old_endpoint} -> {profile_outcome.new_endpoint}"
            )
        else:
            lines.append(f"- {profile_outcome.name}: {profile_outcome.new_endpoint}")

    if outcome.regenerated_qrs:
        lines.extend(
            [
                "",
                "Regenerated QR codes:",
            ]
        )
        lines.extend(f"- {path}" for path in outcome.regenerated_qrs)

    lines.extend(
        [
            "",
            f"Checked at: {datetime.now(timezone.utc).isoformat()}",
        ]
    )
    return "\n".join(lines)


def send_email_notification(config: EmailConfig, subject: str, body: str, *, dry_run: bool) -> None:
    if dry_run or not config.enabled:
        return
    env = dict(os.environ)
    env[AUTO_PASS_NOTIFY_SUPPRESS_ENV] = "1"
    subprocess.run(
        [
            sys.executable,
            str(config.sender_script),
            config.to_address,
            subject,
            body,
            "--config",
            str(config.config_path),
        ],
        check=True,
        env=env,
    )


def load_signal_cli_identity(config_path: Path) -> tuple[str, str]:
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MonitorError(f"unable to read Signal config {config_path}: {exc}") from exc

    account = ""
    bus_name = ""
    in_signal_cli = False
    base_indent: int | None = None

    def val_from_line(line: str) -> str:
        match = re.match(r"^\s*[^:]+:\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s#]+))\s*(?:#.*)?$", line)
        if not match:
            return ""
        return next(value for value in match.groups() if value is not None)

    for line in config_text.splitlines():
        if not in_signal_cli:
            if re.match(r"^\s*signal_cli:\s*$", line):
                in_signal_cli = True
                base_indent = len(line) - len(line.lstrip())
            continue

        if line.strip():
            indent = len(line) - len(line.lstrip())
            if base_indent is not None and indent <= base_indent and not re.match(r"^\s*signal_cli:\s*$", line):
                break

        if re.match(r"^\s*account:\s*", line):
            account = val_from_line(line)
        elif re.match(r"^\s*bus_name:\s*", line):
            bus_name = val_from_line(line)

    if not account:
        raise MonitorError(f"missing signal_cli.account in {config_path}")

    return account, bus_name


def send_signal_notification(config: SignalConfig, message: str, *, dry_run: bool) -> None:
    if dry_run or not config.enabled:
        return
    env = dict(os.environ)
    env[AUTO_PASS_NOTIFY_SUPPRESS_ENV] = "1"
    if config.note_to_self:
        account, bus_name = load_signal_cli_identity(config.config_path)
        cmd = ["signal-cli", "-a", account]
        if bus_name:
            cmd.extend(["--bus-name", bus_name])
        cmd.extend(["send", "--note-to-self", "-m", message])
        subprocess.run(cmd, check=True, env=env)
        return

    subprocess.run(
        [
            sys.executable,
            str(config.sender_script),
            config.recipient,
            message,
            "--config",
            str(config.config_path),
        ],
        check=True,
        env=env,
    )


def run_monitor(
    config: MonitorConfig,
    state: dict[str, Any],
    *,
    public_ip_detector: Callable[[tuple[str, ...]], str] = detect_public_ip,
    qr_renderer: Callable[[ProfileConfig], None] | None = None,
    email_sender: Callable[[EmailConfig, str, str], None] | None = None,
    signal_sender: Callable[[SignalConfig, str], None] | None = None,
    dry_run: bool = False,
) -> MonitorOutcome:
    public_ip = public_ip_detector(config.public_ip_lookup_urls)
    profile_outcomes = [
        update_profile_endpoint(profile, public_ip, dry_run=dry_run)
        for profile in config.profiles
    ]

    endpoint_changed = any(outcome.changed for outcome in profile_outcomes)
    outcome = MonitorOutcome(
        public_ip=public_ip,
        endpoint_changed=endpoint_changed,
        profile_outcomes=profile_outcomes,
    )

    needs_qr_regen = endpoint_changed
    if not needs_qr_regen:
        needs_qr_regen = any(not profile.qr_path.exists() for profile in config.profiles)

    if needs_qr_regen:
        renderer = qr_renderer or (lambda profile: render_profile_qr(profile, dry_run=dry_run))
        for profile in config.profiles:
            renderer(profile)
            outcome.regenerated_qrs.append(profile.qr_path)

    endpoint_transition = endpoint_changed

    now = datetime.now(timezone.utc).isoformat()
    state["last_checked_at"] = now
    state["last_seen_public_ip"] = public_ip
    state["last_applied_public_ip"] = public_ip
    state["last_applied_endpoint"] = outcome.current_endpoint
    if endpoint_transition:
        state["last_changed_at"] = now

    notifications = state.setdefault("notifications", {})
    subject = _build_notification_subject(config.host_label, outcome.current_endpoint)
    body = build_notification_body(config.host_label, outcome)

    email_retry_needed = False
    if config.email.enabled:
        email_state = notifications.setdefault("email", {})
        email_retry_needed = endpoint_transition or email_state.get("last_sent_endpoint") != outcome.current_endpoint
        if email_retry_needed:
            sender = email_sender or (lambda cfg, sub, msg: send_email_notification(cfg, sub, msg, dry_run=dry_run))
            sender(config.email, subject, body)
            email_state["last_sent_endpoint"] = outcome.current_endpoint
            email_state["last_sent_at"] = now
            outcome.email_sent = True
    outcome.email_retry_needed = email_retry_needed

    signal_retry_needed = False
    if config.signal.enabled:
        signal_state = notifications.setdefault("signal", {})
        signal_retry_needed = endpoint_transition or signal_state.get("last_sent_endpoint") != outcome.current_endpoint
        if signal_retry_needed:
            sender = signal_sender or (lambda cfg, msg: send_signal_notification(cfg, msg, dry_run=dry_run))
            sender(config.signal, body)
            signal_state["last_sent_endpoint"] = outcome.current_endpoint
            signal_state["last_sent_at"] = now
            outcome.signal_sent = True
    outcome.signal_retry_needed = signal_retry_needed

    return outcome


def print_summary(outcome: MonitorOutcome, *, dry_run: bool) -> None:
    mode_label = "dry-run " if dry_run else ""
    print(f"{mode_label}detected public IP: {outcome.public_ip}")
    if outcome.endpoint_changed:
        print(f"{mode_label}updated WireGuard endpoint to {outcome.current_endpoint}")
    else:
        print(f"{mode_label}WireGuard endpoint unchanged: {outcome.current_endpoint}")

    for profile_outcome in outcome.profile_outcomes:
        if profile_outcome.changed:
            print(
                f"{mode_label}profile {profile_outcome.name}: "
                f"{profile_outcome.old_endpoint} -> {profile_outcome.new_endpoint}"
            )
        else:
            print(f"{mode_label}profile {profile_outcome.name}: {profile_outcome.new_endpoint}")

    if outcome.regenerated_qrs:
        print(f"{mode_label}regenerated {len(outcome.regenerated_qrs)} QR code(s)")
        for qr_path in outcome.regenerated_qrs:
            print(f"{mode_label}qr: {qr_path}")

    if outcome.email_sent:
        print(f"{mode_label}sent email notification")
    if outcome.signal_sent:
        print(f"{mode_label}sent Signal notification")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect a changed public WireGuard endpoint, refresh local client profiles, "
            "regenerate QR artifacts, and notify through shock-relay."
        )
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to endpoint-monitor.local.toml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions that would be taken without writing files or sending notifications.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(Path(args.config).resolve())
    state = load_state(config.state_file)

    try:
        outcome = run_monitor(config, state, dry_run=args.dry_run)
    except subprocess.CalledProcessError as exc:
        save_state(config.state_file, state, dry_run=args.dry_run)
        raise MonitorError(f"subprocess failed with exit code {exc.returncode}: {exc.cmd}") from exc
    except Exception:
        save_state(config.state_file, state, dry_run=args.dry_run)
        raise

    save_state(config.state_file, state, dry_run=args.dry_run)
    print_summary(outcome, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MonitorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
