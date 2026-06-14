#!/usr/bin/env python3
"""Prepare bind-mounted folders for the snowbridge Samba share."""

from __future__ import annotations

import argparse
import configparser
import os
import pathlib
import pwd
import grp
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass


MANAGED_BLOCK_START = "# --- snowbridge bind mounts: managed block start ---"
MANAGED_BLOCK_END = "# --- snowbridge bind mounts: managed block end ---"
DEFAULT_CONFIG = pathlib.Path("../config/share-layout/folders.local.ini")
FOLDER_SECTION_PREFIX = "folder "
PROTECTED_SOURCE_PREFIXES: tuple[pathlib.Path, ...] = (
    pathlib.Path("/tmp"),
    pathlib.Path("/proc"),
    pathlib.Path("/sys"),
    pathlib.Path("/dev"),
    pathlib.Path("/run"),
)


class ConfigError(RuntimeError):
    """Raised when the bind-share config is invalid."""


@dataclass(frozen=True)
class FolderMount:
    name: str
    source: pathlib.Path
    target_relative: pathlib.PurePosixPath
    target_path: pathlib.Path
    persist: bool
    acl_mode: str
    default_acl_mode: str
    grant_parent_traverse: bool
    create_missing_source: bool
    recursive_acl: bool
    # When set, emits x-systemd.requires-mounts-for= in the fstab entry so the
    # generated .mount unit waits for this path before activating.  Use for
    # sources that live on LUKS-backed ext4 volumes that unlock after boot.
    requires_mounts_for: str | None = None


@dataclass(frozen=True)
class GlobalSettings:
    share_root: pathlib.Path
    smb_user: str
    smb_group: str
    share_root_mode: int
    mount_dir_mode: int
    acl_user: str
    acl_group: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create the Samba share root, bind-mount configured folders into it, "
            "and apply ACLs for the SMB account."
        )
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"INI config file to read (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned actions without changing the system.",
    )
    parser.add_argument(
        "--skip-mount",
        action="store_true",
        help="Do not call mount --bind for the configured folders.",
    )
    parser.add_argument(
        "--skip-acls",
        action="store_true",
        help="Do not apply ACL updates with setfacl.",
    )
    parser.add_argument(
        "--write-fstab",
        action="store_true",
        help="Write a managed bind-mount block into the fstab path.",
    )
    parser.add_argument(
        "--fstab-path",
        default="/etc/fstab",
        help="Path to the fstab file to update when --write-fstab is set.",
    )
    return parser.parse_args()


def get_parser(config_path: pathlib.Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    loaded = parser.read(config_path)
    if not loaded:
        raise ConfigError(f"config file not found: {config_path}")
    return parser


def get_required(
    parser: configparser.ConfigParser, section: str, option: str
) -> str:
    try:
        value = parser[section][option].strip()
    except KeyError as exc:
        raise ConfigError(f"missing required option [{section}] {option}") from exc
    if not value:
        raise ConfigError(f"empty required option [{section}] {option}")
    return value


def get_optional(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    default: str | None = None,
) -> str | None:
    if not parser.has_option(section, option):
        return default
    value = parser[section][option].strip()
    return value or default


def parse_bool(value: str, label: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean for {label}: {value}")


def parse_mode(value: str, label: str) -> int:
    try:
        return int(value, 8)
    except ValueError as exc:
        raise ConfigError(f"invalid octal mode for {label}: {value}") from exc


def validate_acl_mode(value: str, label: str) -> str:
    allowed = set("rwx-")
    if len(value) != 3 or any(char not in allowed for char in value):
        raise ConfigError(
            f"invalid ACL mode for {label}: {value} (expected rwx-style permissions)"
        )
    return value


def validate_target(value: str, label: str) -> pathlib.PurePosixPath:
    target = pathlib.PurePosixPath(value)
    if target.is_absolute():
        raise ConfigError(f"{label} must be relative to share_root: {value}")
    if any(part in {"", ".", ".."} for part in target.parts):
        raise ConfigError(f"{label} contains an invalid relative path: {value}")
    return target


def validate_source(source: pathlib.Path, label: str) -> None:
    for protected in PROTECTED_SOURCE_PREFIXES:
        if source == protected or source.is_relative_to(protected):
            raise ConfigError(
                f"{label} source is a system-protected path: {source}"
            )


def load_config(config_path: pathlib.Path) -> tuple[GlobalSettings, list[FolderMount]]:
    parser = get_parser(config_path)

    if "global" not in parser:
        raise ConfigError("missing required [global] section")

    share_root = pathlib.Path(get_required(parser, "global", "share_root"))
    if not share_root.is_absolute():
        raise ConfigError("share_root must be an absolute path")

    smb_user = get_required(parser, "global", "smb_user")
    smb_group = get_optional(parser, "global", "smb_group", smb_user) or smb_user
    share_root_mode = parse_mode(
        get_optional(parser, "global", "share_root_mode", "2770"), "share_root_mode"
    )
    mount_dir_mode = parse_mode(
        get_optional(parser, "global", "mount_dir_mode", "2770"), "mount_dir_mode"
    )
    acl_user = get_optional(parser, "global", "acl_user", smb_user) or smb_user
    acl_group = get_optional(parser, "global", "acl_group", None)
    global_acl_mode = validate_acl_mode(
        get_optional(parser, "global", "acl_mode", "rwx"), "global acl_mode"
    )
    global_default_acl_mode = validate_acl_mode(
        get_optional(parser, "global", "default_acl_mode", global_acl_mode),
        "global default_acl_mode",
    )
    global_grant_parent_traverse = parse_bool(
        get_optional(parser, "global", "grant_parent_traverse", "yes"),
        "grant_parent_traverse",
    )
    global_create_missing_sources = parse_bool(
        get_optional(parser, "global", "create_missing_sources", "no"),
        "create_missing_sources",
    )
    global_recursive_acl = parse_bool(
        get_optional(parser, "global", "recursive_acl", "yes"),
        "recursive_acl",
    )

    folders: list[FolderMount] = []
    for section in parser.sections():
        if not section.startswith(FOLDER_SECTION_PREFIX):
            continue

        name = section[len(FOLDER_SECTION_PREFIX) :].strip()
        if not name:
            raise ConfigError(f"invalid folder section name: [{section}]")

        source_value = get_required(parser, section, "source")
        source = pathlib.Path(source_value)
        if not source.is_absolute():
            raise ConfigError(f"[{section}] source must be an absolute path")
        validate_source(source, f"[{section}]")

        target_value = get_optional(parser, section, "target", name) or name
        target_relative = validate_target(target_value, f"[{section}] target")
        target_path = share_root.joinpath(*target_relative.parts)

        folders.append(
            FolderMount(
                name=name,
                source=source,
                target_relative=target_relative,
                target_path=target_path,
                persist=parse_bool(
                    get_optional(parser, section, "persist", "yes"), f"[{section}] persist"
                ),
                acl_mode=validate_acl_mode(
                    get_optional(parser, section, "acl_mode", global_acl_mode),
                    f"[{section}] acl_mode",
                ),
                default_acl_mode=validate_acl_mode(
                    get_optional(
                        parser,
                        section,
                        "default_acl_mode",
                        global_default_acl_mode,
                    ),
                    f"[{section}] default_acl_mode",
                ),
                grant_parent_traverse=parse_bool(
                    get_optional(
                        parser,
                        section,
                        "grant_parent_traverse",
                        "yes" if global_grant_parent_traverse else "no",
                    ),
                    f"[{section}] grant_parent_traverse",
                ),
                create_missing_source=parse_bool(
                    get_optional(
                        parser,
                        section,
                        "create_missing_source",
                        "yes" if global_create_missing_sources else "no",
                    ),
                    f"[{section}] create_missing_source",
                ),
                recursive_acl=parse_bool(
                    get_optional(
                        parser,
                        section,
                        "recursive_acl",
                        "yes" if global_recursive_acl else "no",
                    ),
                    f"[{section}] recursive_acl",
                ),
                requires_mounts_for=get_optional(
                    parser, section, "requires_mounts_for", None
                ),
            )
        )

    if not folders:
        raise ConfigError("no [folder ...] sections found in config")

    globals_ = GlobalSettings(
        share_root=share_root,
        smb_user=smb_user,
        smb_group=smb_group,
        share_root_mode=share_root_mode,
        mount_dir_mode=mount_dir_mode,
        acl_user=acl_user,
        acl_group=acl_group,
    )
    return globals_, folders


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise ConfigError(f"required tool not found in PATH: {name}")


def log(message: str) -> None:
    print(message)


def run_command(command: list[str], dry_run: bool) -> None:
    printable = " ".join(command)
    log(f"$ {printable}")
    if dry_run:
        return
    subprocess.run(command, check=True)


def lookup_ids(user: str, group: str) -> tuple[int, int]:
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError as exc:
        raise ConfigError(f"unknown user: {user}") from exc
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise ConfigError(f"unknown group: {group}") from exc
    return uid, gid


def ensure_directory(
    path: pathlib.Path,
    uid: int,
    gid: int,
    mode: int,
    dry_run: bool,
    skip_existing_permissions: bool = False,
) -> None:
    log(f"ensure directory {path} mode={mode:o}")
    if dry_run:
        return
    path.mkdir(parents=True, exist_ok=True)
    if skip_existing_permissions:
        return
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def is_mountpoint(path: pathlib.Path) -> bool:
    result = subprocess.run(
        ["mountpoint", "-q", str(path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def mounted_sources(path: pathlib.Path) -> list[str]:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE", "--target", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    seen: set[str] = set()
    sources: list[str] = []
    for line in result.stdout.splitlines():
        value = line.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        sources.append(value)
    return sources


def mounted_source_details(path: pathlib.Path) -> tuple[str, pathlib.Path] | None:
    result = subprocess.run(
        ["findmnt", "-P", "-n", "-o", "SOURCE,TARGET", "--target", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        fields: dict[str, str] = {}
        for token in shlex.split(value):
            key, _, field_value = token.partition("=")
            if key and field_value:
                fields[key] = field_value
        source = fields.get("SOURCE")
        target = fields.get("TARGET")
        if source and target:
            return source, pathlib.Path(target)
    return None


def mounted_source_subpath(source: str) -> pathlib.Path | None:
    if "[" not in source or not source.endswith("]"):
        return None
    _, _, remainder = source.partition("[")
    subpath = remainder[:-1].strip()
    return pathlib.Path(subpath) if subpath else None


def mounted_source_device_and_subpath(
    source: str,
) -> tuple[pathlib.Path, pathlib.Path] | None:
    if "[" not in source or not source.endswith("]"):
        return None
    device_text, _, remainder = source.partition("[")
    subpath_text = remainder[:-1].strip()
    device = device_text.strip()
    if not device or not subpath_text:
        return None
    return pathlib.Path(device), pathlib.Path(subpath_text)


def same_mounted_source(a: str, b: str) -> bool:
    if a == b:
        return True

    a_parts = mounted_source_device_and_subpath(a)
    b_parts = mounted_source_device_and_subpath(b)
    if a_parts is None or b_parts is None:
        return False

    a_device, a_subpath = a_parts
    b_device, b_subpath = b_parts
    if pathlib.Path(os.path.realpath(a_device)) != pathlib.Path(os.path.realpath(b_device)):
        return False

    if a_subpath == b_subpath:
        return True

    resolved_a_subpath = pathlib.Path(os.path.realpath(a_subpath))
    resolved_b_subpath = pathlib.Path(os.path.realpath(b_subpath))
    if resolved_a_subpath == resolved_b_subpath:
        return True

    try:
        a_stat = resolved_a_subpath.stat()
        b_stat = resolved_b_subpath.stat()
    except OSError:
        return False
    return (a_stat.st_dev, a_stat.st_ino) == (b_stat.st_dev, b_stat.st_ino)


def mounted_source_identity(path: pathlib.Path) -> str | None:
    details = mounted_source_details(path)
    if details is None:
        return None

    source, target = details
    try:
        relative = path.relative_to(target)
    except ValueError:
        return source

    source_parts = mounted_source_device_and_subpath(source)
    if source_parts is not None:
        device, base_subpath = source_parts
    else:
        source_path = pathlib.Path(source)
        if not str(source_path).startswith("/dev/"):
            return source
        device = source_path
        base_subpath = pathlib.Path("/")

    subpath = base_subpath
    if relative.parts:
        subpath = base_subpath.joinpath(*relative.parts)

    return f"{device}[{subpath}]"


def mounted_source_matches(source: str, expected: pathlib.Path) -> bool:
    expected_source = mounted_source_identity(expected)
    if expected_source is not None and same_mounted_source(source, expected_source):
        return True

    resolved_expected = pathlib.Path(os.path.realpath(expected))
    resolved_plain = pathlib.Path(os.path.realpath(source))
    return resolved_expected == resolved_plain


def ensure_source_exists(folder: FolderMount, dry_run: bool) -> None:
    if folder.source.exists():
        return
    if not folder.create_missing_source:
        if dry_run:
            log(
                f"dry-run warning: source path does not exist yet for [{folder.name}]: "
                f"{folder.source}"
            )
            return
        raise ConfigError(
            f"source path does not exist for [{folder.name}]: {folder.source} "
            "(enable create_missing_source to create it)"
        )
    log(f"create source directory {folder.source}")
    if not dry_run:
        folder.source.mkdir(parents=True, exist_ok=True)


def apply_acl_entry(path: pathlib.Path, entity: str, mode: str, dry_run: bool) -> None:
    run_command(["setfacl", "-m", f"{entity}:{mode}", str(path)], dry_run)


def apply_default_acl_entry(
    path: pathlib.Path, entity: str, mode: str, dry_run: bool, recursive: bool
) -> None:
    command = ["setfacl"]
    if recursive:
        command.append("-R")
    command.extend(["-d", "-m", f"{entity}:{mode}", str(path)])
    run_command(command, dry_run)


def apply_acl_mode(
    path: pathlib.Path, entity: str, mode: str, dry_run: bool, recursive: bool
) -> None:
    command = ["setfacl"]
    if recursive:
        command.append("-R")
    command.extend(["-m", f"{entity}:{mode}", str(path)])
    run_command(command, dry_run)


def parent_paths(path: pathlib.Path) -> list[pathlib.Path]:
    parents = list(path.parents)
    if parents and parents[-1] == pathlib.Path("/"):
        parents = parents[:-1]
    parents.reverse()
    return parents


def apply_folder_acls(
    globals_: GlobalSettings, folder: FolderMount, dry_run: bool, skip_acls: bool
) -> None:
    if skip_acls:
        log(f"skip ACL updates for {folder.source}")
        return

    require_tool("setfacl")

    if folder.grant_parent_traverse:
        for parent in parent_paths(folder.source):
            apply_acl_entry(parent, f"u:{globals_.acl_user}", "--x", dry_run)
            if globals_.acl_group:
                apply_acl_entry(parent, f"g:{globals_.acl_group}", "--x", dry_run)

    apply_acl_mode(
        folder.source,
        f"u:{globals_.acl_user}",
        folder.acl_mode,
        dry_run,
        folder.recursive_acl,
    )
    apply_default_acl_entry(
        folder.source,
        f"u:{globals_.acl_user}",
        folder.default_acl_mode,
        dry_run,
        folder.recursive_acl,
    )

    if globals_.acl_group:
        apply_acl_mode(
            folder.source,
            f"g:{globals_.acl_group}",
            folder.acl_mode,
            dry_run,
            folder.recursive_acl,
        )
        apply_default_acl_entry(
            folder.source,
            f"g:{globals_.acl_group}",
            folder.default_acl_mode,
            dry_run,
            folder.recursive_acl,
        )


def ensure_mountpoint(
    globals_: GlobalSettings, folder: FolderMount, uid: int, gid: int, dry_run: bool
) -> None:
    current = globals_.share_root
    for part in folder.target_relative.parts:
        current = current / part
        ensure_directory(
            current,
            uid,
            gid,
            globals_.mount_dir_mode,
            dry_run,
            skip_existing_permissions=is_mountpoint(current),
        )


def ensure_bind_mount(folder: FolderMount, dry_run: bool, skip_mount: bool) -> None:
    if is_mountpoint(folder.target_path):
        current_sources = mounted_sources(folder.target_path)
        if not current_sources:
            raise ConfigError(f"could not determine mounted source for {folder.target_path}")
        if not any(
            mounted_source_matches(current_source, folder.source)
            for current_source in current_sources
        ):
            if skip_mount:
                log(
                    f"skip remount for {folder.target_path}: current source "
                    f"{', '.join(current_sources)}, expected {folder.source}"
                )
                return
            log(
                f"remount bind target {folder.target_path}: current source "
                f"{', '.join(current_sources)}, expected {folder.source}"
            )
            run_command(["umount", str(folder.target_path)], dry_run)
            run_command(
                ["mount", "--bind", str(folder.source), str(folder.target_path)],
                dry_run,
            )
            return
        log(f"bind mount already active: {folder.source} -> {folder.target_path}")
        return

    if skip_mount:
        log(f"skip mount for {folder.source} -> {folder.target_path}")
        return

    run_command(
        ["mount", "--bind", str(folder.source), str(folder.target_path)],
        dry_run,
    )


def escape_fstab_field(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(" ", "\\040")
        .replace("\t", "\\011")
        .replace("\n", "\\012")
    )


def build_fstab_block(
    config_path: pathlib.Path,
    folders: list[FolderMount],
) -> str:
    lines = [
        MANAGED_BLOCK_START,
        f"# Managed by scripts/setup_bind_share.py --config {config_path}",
    ]
    for folder in folders:
        if not folder.persist:
            continue
        opts = "bind"
        if folder.requires_mounts_for:
            opts += f",x-systemd.requires-mounts-for={folder.requires_mounts_for}"
        lines.append(
            " ".join(
                [
                    escape_fstab_field(str(folder.source)),
                    escape_fstab_field(str(folder.target_path)),
                    "none",
                    opts,
                    "0",
                    "0",
                ]
            )
        )
    lines.append(MANAGED_BLOCK_END)
    return "\n".join(lines) + "\n"


def write_fstab_block(
    fstab_path: pathlib.Path,
    config_path: pathlib.Path,
    folders: list[FolderMount],
    dry_run: bool,
) -> None:
    block = build_fstab_block(config_path, folders)
    log(f"update managed fstab block in {fstab_path}")
    if dry_run:
        print(block, end="")
        return

    existing = fstab_path.read_text(encoding="utf-8") if fstab_path.exists() else ""
    if MANAGED_BLOCK_START in existing and MANAGED_BLOCK_END in existing:
        start = existing.index(MANAGED_BLOCK_START)
        end = existing.index(MANAGED_BLOCK_END) + len(MANAGED_BLOCK_END)
        updated = existing[:start].rstrip() + "\n\n" + block + existing[end:].lstrip("\n")
    else:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        updated = existing + separator + ("\n" if existing else "") + block
    fstab_path.write_text(updated, encoding="utf-8")


def main() -> int:
    args = parse_args()
    config_path = pathlib.Path(args.config)

    try:
        globals_, folders = load_config(config_path)

        require_tool("mountpoint")
        require_tool("findmnt")
        require_tool("mount")

        if not args.dry_run and os.geteuid() != 0:
            raise ConfigError("run this script as root unless --dry-run is set")

        uid, gid = lookup_ids(globals_.smb_user, globals_.smb_group)
        ensure_directory(
            globals_.share_root,
            uid,
            gid,
            globals_.share_root_mode,
            args.dry_run,
        )

        for folder in folders:
            log(f"configure folder {folder.name}: {folder.source} -> {folder.target_path}")
            ensure_source_exists(folder, args.dry_run)
            apply_folder_acls(globals_, folder, args.dry_run, args.skip_acls)
            ensure_mountpoint(globals_, folder, uid, gid, args.dry_run)
            ensure_bind_mount(folder, args.dry_run, args.skip_mount)

        if args.write_fstab:
            write_fstab_block(
                pathlib.Path(args.fstab_path),
                config_path,
                folders,
                args.dry_run,
            )

    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"command failed: {' '.join(exc.cmd)}", file=sys.stderr)
        return exc.returncode or 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
