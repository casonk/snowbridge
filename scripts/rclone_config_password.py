#!/usr/bin/env python3
"""Print the rclone config encryption password from KeePassXC via auto-pass.

This is an rclone ``--password-command`` provider. rclone executes it and reads
the password from stdout, so stdout carries the secret and nothing else: all
diagnostics go to stderr, and no failure path echoes a resolved value.

It replaces the fixed macOS Keychain helper for owners who keep portfolio
secrets in KeePassXC. Which entry to read is declared in the ignored
``config/auto-pass.ini``; the tracked example shows the shape.

The password protects the encrypted rclone config, which is what keeps OAuth
refresh tokens and the iCloud app-specific password unreadable at rest. It is
never the provider credential itself.
"""

from __future__ import annotations

import argparse
import configparser
import os
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_auto_pass_root(start: Path = REPO_ROOT) -> Path:
    """Locate the auto-pass sibling checkout.

    Normally it sits beside this repo under the portfolio's util-repos
    directory. Inside a git worktree the repo root is
    ``<repo>/.claude/worktrees/<name>``, so the plain sibling lookup lands in
    ``.claude/worktrees``; walk up until a checkout that actually contains the
    package is found. The sibling path is returned unchanged when nothing
    matches, so the caller's error names the expected location.
    """

    sibling = start.parent / "auto-pass"
    if (sibling / "src" / "auto_pass").is_dir():
        return sibling
    for ancestor in start.parents:
        candidate = ancestor / "auto-pass"
        if (candidate / "src" / "auto_pass").is_dir():
            return candidate
    return sibling


AUTO_PASS_ROOT = resolve_auto_pass_root()
AUTO_PASS_CONFIG = REPO_ROOT / "config" / "auto-pass.ini"
DEFAULT_FIELD = "Password"
# auto-pass declares requires-python = ">=3.11".
MINIMUM_PYTHON = (3, 11)
# auto-pass caches the unlocked database password so later non-interactive runs
# can reuse it. Its default location is the shared ~/.cache, which on this host
# is root-owned and unwritable, and which other tools also use. Keeping the
# cache under the snowbridge config directory makes it owner-only, scoped to
# this repo, and independent of that shared directory's ownership.
CACHE_DIR = Path.home() / ".config" / "snowbridge" / "cache"
CONFIG_SECTION = "cloud"
ENTRY_OPTION = "rclone_config_keepass_entry"
FIELD_OPTION = "rclone_config_keepass_field"


class PasswordHelperError(RuntimeError):
    """A fail-closed lookup error that never carries a secret value."""


def _fail(message: str) -> NoReturn:
    raise PasswordHelperError(message)


def read_helper_config(path: Path = AUTO_PASS_CONFIG) -> tuple[str, str, str]:
    """Return (profile, entry, field) from the ignored auto-pass config."""

    if not path.is_file():
        _fail(
            f"{path} does not exist; copy config/auto-pass.example.ini to it and "
            f"set [{CONFIG_SECTION}] {ENTRY_OPTION}"
        )
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as error:
        _fail(f"cannot read {path}: {error}")
    profile = parser.get("auto_pass", "profile", fallback="").strip()
    entry = parser.get(CONFIG_SECTION, ENTRY_OPTION, fallback="").strip()
    field = parser.get(CONFIG_SECTION, FIELD_OPTION, fallback="").strip() or DEFAULT_FIELD
    if not entry:
        _fail(f"{path} is missing [{CONFIG_SECTION}] {ENTRY_OPTION}")
    return profile, entry, field


def prepare_cache_directory(path: Path = CACHE_DIR) -> Path:
    """Create the owner-only cache directory holding the unlocked DB password."""

    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir's mode is subject to umask, and the directory may predate this
        # helper, so the permission is asserted rather than assumed.
        os.chmod(path, 0o700)
        details = path.stat()
    except OSError as error:
        _fail(f"cannot prepare password cache directory {path}: {error}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        _fail(f"password cache directory {path} must be owned by the current user")
    return path


def resolve_password(profile: str, entry: str, field: str) -> str:
    """Resolve one KeePassXC field through the auto-pass sibling repo."""

    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        running = ".".join(str(part) for part in sys.version_info[:3])
        _fail(
            f"auto-pass requires Python {required}+, but this helper is running "
            f"under {running} ({sys.executable}). Invoke it with a newer "
            "interpreter; note that rclone runs it with a minimal PATH where "
            "/usr/bin precedes Homebrew."
        )
    source = AUTO_PASS_ROOT / "src"
    if not source.is_dir():
        _fail(f"auto-pass sibling repo is not available at {AUTO_PASS_ROOT}")
    if os.fspath(source) not in sys.path:
        sys.path.insert(0, os.fspath(source))
    try:
        from auto_pass.envfile import load_config_environment
        from auto_pass.keepassxc import (
            KeepassCommandError,
            KeepassXCStoreConfig,
            resolve_keepassxc_entry,
        )
    except ImportError as error:
        _fail(f"cannot import auto-pass from {source}: {error}")

    environment_file = AUTO_PASS_ROOT / "config" / "auto-pass.env.local"
    if environment_file.is_file():
        load_config_environment(environment_file, profile=profile or None)

    store = KeepassXCStoreConfig(
        database_password_cache_dir=os.fspath(prepare_cache_directory(CACHE_DIR))
    )
    try:
        resolved = resolve_keepassxc_entry(entry, attrs_map={"value": field}, config=store)
    except KeepassCommandError as error:
        # KeePassXC reports the entry path and database on failure, never the
        # attribute value, so this message stays free of the secret.
        _fail(f"auto-pass lookup failed for entry {entry!r} field {field!r}: {error}")

    password = resolved.get("value", "")
    if not password:
        _fail(f"entry {entry!r} has no value in field {field!r}")
    if "\n" in password or "\r" in password:
        _fail(f"entry {entry!r} field {field!r} contains a line break")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print the rclone config encryption password from KeePassXC. "
            "Intended as an rclone --password-command provider."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the lookup succeeds and print only its length, not the password.",
    )
    arguments = parser.parse_args(argv)
    try:
        profile, entry, field = read_helper_config(AUTO_PASS_CONFIG)
        password = resolve_password(profile, entry, field)
    except PasswordHelperError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if arguments.check:
        print(f"resolved {entry!r} field {field!r}: {len(password)} characters")
        return 0
    # rclone reads the first stdout line. Nothing else may be written here.
    sys.stdout.write(f"{password}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
