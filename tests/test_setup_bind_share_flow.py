"""Preparing the Samba share: config rules, bind mounts, ACLs and the managed fstab block.

The script runs as root and bind-mounts host folders into a share, applies recursive ACLs to
them, and rewrites ``/etc/fstab``. So the tests pin what keeps that safe: a source that is, or
resolves into, a system directory is refused (including through ``..`` and symlinks), the fstab is
replaced atomically so a failed write leaves the original, nothing real is mounted, and every
command that would run is checked as a list of arguments. Mounts, ACL tools, root and the real
filesystem outside ``tmp_path`` are all replaced.
"""

from __future__ import annotations

import os
import pathlib
import pwd
import resource
import signal
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import setup_bind_share as mod

ME = pwd.getpwuid(os.getuid()).pw_name
P = pathlib.Path


@pytest.fixture
def commands(monkeypatch):
    """Record every command run_command is asked to run instead of running it."""
    seen = []
    monkeypatch.setattr(mod, "run_command", lambda command, dry_run: seen.append((list(command), dry_run)))
    return seen


def folder(tmp_path, **kw):
    fields = dict(
        name="docs", source=tmp_path / "src", target_relative=mod.pathlib.PurePosixPath("docs"),
        target_path=tmp_path / "share" / "docs", persist=True, acl_mode="rwx", default_acl_mode="rwx",
        grant_parent_traverse=True, create_missing_source=False, recursive_acl=True,
        requires_mounts_for=None,
    )  # fmt: skip
    fields.update(kw)
    return mod.FolderMount(**fields)


def settings(tmp_path, **kw):
    fields = dict(share_root=tmp_path / "share", smb_user="smb", smb_group="smb", share_root_mode=0o2770,
                  mount_dir_mode=0o2770, acl_user="smb", acl_group=None)  # fmt: skip
    fields.update(kw)
    return mod.GlobalSettings(**fields)


# --- refusing dangerous sources -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/etc", "/etc/ssh", "/boot", "/usr/share", "/bin", "/sbin", "/lib", "/lib64", "/root",
     "/tmp", "/tmp/x", "/proc", "/sys", "/dev", "/run"],
)  # fmt: skip
def test_system_directories_are_never_a_share_source(path):
    with pytest.raises(mod.ConfigError, match="system-protected path"):
        mod.validate_source(P(path), "[folder x]")


@pytest.mark.parametrize(
    "path",
    ["/srv/../tmp/x", "/mnt/data/../../etc", "/home/user/../../proc/self", "/srv/./../root", "/a/b/../../.."],
)
def test_parent_directory_components_cannot_smuggle_in_a_protected_path(path):
    with pytest.raises(mod.ConfigError, match="system-protected path"):
        mod.validate_source(P(path), "[folder x]")


@pytest.fixture
def tmp_not_protected(monkeypatch):
    """pytest's temporary directory is under /tmp, which is protected; allow it for these tests only,
    so that what is being tested is the symlink or `..` handling and not the /tmp rule."""
    monkeypatch.setattr(mod, "PROTECTED_SOURCE_PREFIXES", tuple(p for p in mod.PROTECTED_SOURCE_PREFIXES if p != P("/tmp")))


def test_a_symlink_that_points_into_a_protected_directory_is_refused(tmp_path, tmp_not_protected):
    link = tmp_path / "innocent"
    link.symlink_to("/etc")
    with pytest.raises(mod.ConfigError, match=r"system-protected path.*resolves to /etc"):
        mod.validate_source(link, "[folder x]")
    with pytest.raises(mod.ConfigError, match="system-protected path"):
        mod.validate_source(link / "ssh", "[folder x]")
    mod.validate_source(tmp_path / "plain", "[folder x]")  # the same directory, without the link, is fine


def test_a_path_is_refused_if_it_is_protected_in_either_spelling(tmp_path, tmp_not_protected):
    """Read lexically `link/..` is the parent of `link`; resolved, it is the parent of its target."""
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(deep)
    ups = "/.." * len(tmp_path.parts)  # lexically this climbs exactly to "/"; resolved it stops short
    with pytest.raises(mod.ConfigError, match="system-protected path"):
        mod.validate_source(P(f"{link}{ups}/etc"), "[folder x]")


@pytest.mark.parametrize(
    "path", ["/mnt/data", "/srv/share", "/home/user/luks", "/tmpfiles", "/etcetera/x", "/usrlocal", "/mnt/4tb-m2/tmp"]
)
def test_ordinary_data_directories_are_accepted(path):
    mod.validate_source(P(path), "[folder x]")


# --- config values ------------------------------------------------------------------------


def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog"])
    args = mod.parse_args()
    assert args.config == str(mod.DEFAULT_CONFIG) and args.fstab_path == "/etc/fstab"
    assert (args.dry_run, args.skip_mount, args.skip_acls, args.write_fstab) == (False, False, False, False)


@pytest.mark.parametrize(("raw", "expected"), [("yes", True), (" ON ", True), ("1", True), ("True", True),
                                              ("no", False), ("0", False), ("Off", False), ("false", False)])  # fmt: skip
def test_booleans(raw, expected):
    assert mod.parse_bool(raw, "x") is expected


def test_an_unclear_boolean_is_refused():
    with pytest.raises(mod.ConfigError, match="invalid boolean for opt: maybe"):
        mod.parse_bool("maybe", "opt")


def test_modes_are_octal():
    assert mod.parse_mode("2770", "m") == 0o2770 and mod.parse_mode("755", "m") == 0o755
    for bad in ("999", "rwx", ""):
        with pytest.raises(mod.ConfigError, match="invalid octal mode"):
            mod.parse_mode(bad, "m")


@pytest.mark.parametrize("value", ["rwx", "r-x", "---", "-w-"])
def test_valid_acl_modes(value):
    assert mod.validate_acl_mode(value, "m") == value


@pytest.mark.parametrize("value", ["rw", "rwxx", "rwa", "", "RWX", "r x"])
def test_invalid_acl_modes(value):
    with pytest.raises(mod.ConfigError, match="invalid ACL mode"):
        mod.validate_acl_mode(value, "m")


@pytest.mark.parametrize(("value", "expected"), [("docs", "docs"), ("a/b", "a/b"), ("a/b/c", "a/b/c"), ("a/./b", "a/b"), ("a//b", "a/b")])
def test_relative_targets_are_accepted_and_tidied(value, expected):
    assert str(mod.validate_target(value, "t")) == expected


@pytest.mark.parametrize("value", ["/abs", "../up", "a/../b", "a//b/..", ".."])
def test_absolute_and_parent_targets_are_refused(value):
    with pytest.raises(mod.ConfigError):
        mod.validate_target(value, "t")


def write_ini(tmp_path, body):
    path = tmp_path / "folders.ini"
    path.write_text(body)
    return path


GOOD = """
[global]
share_root = /srv/share
smb_user = smb

[folder docs]
source = /mnt/data/docs
"""


def test_a_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(mod.ConfigError, match="config file not found"):
        mod.get_parser(tmp_path / "none.ini")


def test_required_and_optional_options(tmp_path):
    parser = mod.get_parser(write_ini(tmp_path, "[s]\na = 1\nb =   \n"))
    assert mod.get_required(parser, "s", "a") == "1"
    with pytest.raises(mod.ConfigError, match=r"missing required option \[s\] z"):
        mod.get_required(parser, "s", "z")
    with pytest.raises(mod.ConfigError, match=r"empty required option \[s\] b"):
        mod.get_required(parser, "s", "b")
    assert mod.get_optional(parser, "s", "a", "d") == "1"
    assert mod.get_optional(parser, "s", "b", "d") == "d" and mod.get_optional(parser, "s", "z", "d") == "d"
    assert mod.get_optional(parser, "s", "z") is None


def test_defaults_are_filled_in_and_shared_by_every_folder(tmp_path):
    globals_, folders = mod.load_config(write_ini(tmp_path, GOOD))
    assert (globals_.smb_user, globals_.smb_group, globals_.acl_user, globals_.acl_group) == ("smb", "smb", "smb", None)
    assert (globals_.share_root_mode, globals_.mount_dir_mode) == (0o2770, 0o2770)
    (docs,) = folders
    assert docs.target_relative == mod.pathlib.PurePosixPath("docs") and docs.target_path == P("/srv/share/docs")
    assert (docs.persist, docs.acl_mode, docs.default_acl_mode) == (True, "rwx", "rwx")
    assert (docs.grant_parent_traverse, docs.create_missing_source, docs.recursive_acl) == (True, False, True)
    assert docs.requires_mounts_for is None


def test_global_settings_are_inherited_and_a_folder_can_override_them(tmp_path):
    ini = """
[global]
share_root = /srv/share
smb_user = smb
smb_group = staff
acl_user = reader
acl_group = readers
acl_mode = r-x
grant_parent_traverse = no
create_missing_sources = yes
recursive_acl = no
share_root_mode = 0750
mount_dir_mode = 0700

[folder inherited]
source = /mnt/a

[folder custom name]
source = /mnt/b
target = deep/er
persist = no
acl_mode = rwx
default_acl_mode = r--
grant_parent_traverse = yes
create_missing_source = no
recursive_acl = yes
requires_mounts_for = /mnt/b
"""
    globals_, (inherited, custom) = mod.load_config(write_ini(tmp_path, ini))
    assert (globals_.smb_group, globals_.acl_user, globals_.acl_group) == ("staff", "reader", "readers")
    assert (globals_.share_root_mode, globals_.mount_dir_mode) == (0o750, 0o700)
    assert (inherited.acl_mode, inherited.default_acl_mode) == ("r-x", "r-x")
    assert (inherited.grant_parent_traverse, inherited.create_missing_source, inherited.recursive_acl) == (False, True, False)
    assert custom.name == "custom name" and str(custom.target_relative) == "deep/er"
    assert custom.target_path == P("/srv/share/deep/er") and custom.persist is False
    assert (custom.acl_mode, custom.default_acl_mode) == ("rwx", "r--")
    assert (custom.grant_parent_traverse, custom.create_missing_source, custom.recursive_acl) == (True, False, True)
    assert custom.requires_mounts_for == "/mnt/b"


@pytest.mark.parametrize(
    ("ini", "message"),
    [
        ("[folder x]\nsource = /mnt/a\n", r"missing required \[global\] section"),
        ("[global]\nsmb_user = smb\n[folder x]\nsource = /mnt/a\n", "missing required option"),
        ("[global]\nshare_root = rel\nsmb_user = smb\n[folder x]\nsource = /mnt/a\n", "share_root must be an absolute path"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\n", "no .folder"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\n[folder ]\nsource = /mnt/a\n", "invalid folder section name"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\n[folder x]\n", "missing required option"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\n[folder x]\nsource = rel/path\n", "source must be an absolute path"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\n[folder x]\nsource = /etc\n", "system-protected"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\n[folder x]\nsource = /mnt/a\ntarget = ../x\n", "invalid relative path"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\nacl_mode = rw\n[folder x]\nsource = /mnt/a\n", "invalid ACL mode"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\nshare_root_mode = 9\n[folder x]\nsource = /mnt/a\n", "invalid octal mode"),
        ("[global]\nshare_root = /srv\nsmb_user = smb\n[folder x]\nsource = /mnt/a\npersist = maybe\n", "invalid boolean"),
    ],
)
def test_bad_configs_are_refused_with_a_reason(tmp_path, ini, message):
    with pytest.raises(mod.ConfigError, match=message):
        mod.load_config(write_ini(tmp_path, ini))


def test_non_folder_sections_are_ignored(tmp_path):
    _, folders = mod.load_config(write_ini(tmp_path, GOOD + "\n[notes]\nanything = 1\n"))
    assert len(folders) == 1


# --- running things -----------------------------------------------------------------------


def test_a_missing_tool_is_named(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(mod.ConfigError, match="required tool not found in PATH: setfacl"):
        mod.require_tool("setfacl")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/x")
    mod.require_tool("setfacl")


def test_commands_are_printed_and_only_run_outside_a_dry_run(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(mod.subprocess, "run", lambda command, check: ran.append((command, check)))
    mod.run_command(["setfacl", "-m", "u:a:rwx", "/x"], dry_run=True)
    assert ran == [] and capsys.readouterr().out == "$ setfacl -m u:a:rwx /x\n"
    mod.run_command(["mount", "--bind", "/a", "/b"], dry_run=False)
    assert ran == [(["mount", "--bind", "/a", "/b"], True)]


def test_accounts_must_exist():
    assert mod.lookup_ids(ME, ME)[0] == os.getuid()
    with pytest.raises(mod.ConfigError, match="unknown user: no-such-user-xyz"):
        mod.lookup_ids("no-such-user-xyz", ME)
    with pytest.raises(mod.ConfigError, match="unknown group: no-such-group-xyz"):
        mod.lookup_ids(ME, "no-such-group-xyz")


def test_directories_are_created_with_owner_and_mode_unless_dry_run_or_already_mounted(tmp_path, capsys):
    target = tmp_path / "a" / "b"
    mod.ensure_directory(target, os.getuid(), os.getgid(), 0o750, dry_run=True)
    assert not target.exists() and "ensure directory" in capsys.readouterr().out
    mod.ensure_directory(target, os.getuid(), os.getgid(), 0o750, dry_run=False)
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    other = tmp_path / "mounted"
    other.mkdir(mode=0o700)
    mod.ensure_directory(other, os.getuid(), os.getgid(), 0o770, dry_run=False, skip_existing_permissions=True)
    assert stat.S_IMODE(other.stat().st_mode) == 0o700  # a mounted folder's own permissions are left alone


# --- reading mount state ------------------------------------------------------------------


def fake_run(monkeypatch, returncode=0, stdout=""):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    monkeypatch.setattr(mod.subprocess, "run", run)
    return calls


def test_mountpoint_status_follows_the_exit_code(monkeypatch):
    calls = fake_run(monkeypatch, 0)
    assert mod.is_mountpoint(P("/x")) is True and calls[0] == ["mountpoint", "-q", "/x"]
    fake_run(monkeypatch, 1)
    assert mod.is_mountpoint(P("/x")) is False


def test_mounted_sources_are_listed_without_blanks_or_repeats(monkeypatch):
    fake_run(monkeypatch, 0, "/dev/a[/x]\n\n  /dev/b \n/dev/a[/x]\n")
    assert mod.mounted_sources(P("/t")) == ["/dev/a[/x]", "/dev/b"]
    fake_run(monkeypatch, 1, "/dev/a")
    assert mod.mounted_sources(P("/t")) == []


def test_mount_details_are_parsed_from_findmnt_pairs(monkeypatch):
    fake_run(monkeypatch, 0, 'SOURCE="/dev/mapper/x[/sub dir]" TARGET="/mnt/x"\n')
    assert mod.mounted_source_details(P("/mnt/x")) == ("/dev/mapper/x[/sub dir]", P("/mnt/x"))
    fake_run(monkeypatch, 0, '\nSOURCE="/dev/a"\nSOURCE="/dev/b" TARGET="/mnt/b"\n')
    assert mod.mounted_source_details(P("/mnt/b")) == ("/dev/b", P("/mnt/b"))
    fake_run(monkeypatch, 0, "garbage without pairs\n")
    assert mod.mounted_source_details(P("/mnt/b")) is None
    fake_run(monkeypatch, 1, 'SOURCE="/dev/a" TARGET="/x"')
    assert mod.mounted_source_details(P("/mnt/b")) is None


def test_bracketed_sources_split_into_device_and_subpath():
    assert mod.mounted_source_subpath("/dev/a[/sub]") == P("/sub")
    assert mod.mounted_source_subpath("/dev/a") is None and mod.mounted_source_subpath("/dev/a[]") is None
    assert mod.mounted_source_subpath("/dev/a[/sub") is None
    assert mod.mounted_source_device_and_subpath("/dev/a[/sub]") == (P("/dev/a"), P("/sub"))
    for bad in ("/dev/a", "[/sub]", "/dev/a[]", "/dev/a[/sub"):
        assert mod.mounted_source_device_and_subpath(bad) is None


def test_the_same_mount_is_recognised_through_equivalent_spellings(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert mod.same_mounted_source("/dev/a", "/dev/a") is True
    assert mod.same_mounted_source("/dev/a", "/dev/b") is False
    assert mod.same_mounted_source("/dev/a[/x]", "/dev/b[/x]") is False  # different devices
    assert mod.same_mounted_source("/dev/a[/x]", "/dev/a[/x]") is True
    assert mod.same_mounted_source(f"/dev/a[{real}]", f"/dev/a[{link}]") is True  # via a symlink
    assert mod.same_mounted_source("/dev/a[/nope/1]", "/dev/a[/nope/2]") is False
    assert mod.same_mounted_source("/dev/a[/x]", "/dev/a") is False


def test_the_identity_of_a_path_is_its_device_and_subpath(monkeypatch):
    monkeypatch.setattr(mod, "mounted_source_details", lambda path: None)
    assert mod.mounted_source_identity(P("/mnt/x")) is None
    monkeypatch.setattr(mod, "mounted_source_details", lambda path: ("/dev/sda1", P("/mnt")))
    assert mod.mounted_source_identity(P("/mnt/x/y")) == "/dev/sda1[/x/y]"
    monkeypatch.setattr(mod, "mounted_source_details", lambda path: ("/dev/sda1[/base]", P("/mnt")))
    assert mod.mounted_source_identity(P("/mnt/x")) == "/dev/sda1[/base/x]"
    assert mod.mounted_source_identity(P("/mnt")) == "/dev/sda1[/base]"
    monkeypatch.setattr(mod, "mounted_source_details", lambda path: ("tmpfs", P("/mnt")))
    assert mod.mounted_source_identity(P("/mnt/x")) == "tmpfs"  # not a block device: reported as is
    monkeypatch.setattr(mod, "mounted_source_details", lambda path: ("/dev/sda1", P("/other")))
    assert mod.mounted_source_identity(P("/mnt/x")) == "/dev/sda1"  # path not under the mount target


# --- preparing the source and the ACLs ----------------------------------------------------


def test_a_missing_source_is_created_only_when_allowed(tmp_path, capsys):
    f = folder(tmp_path)
    with pytest.raises(mod.ConfigError, match="source path does not exist for \\[docs\\]"):
        mod.ensure_source_exists(f, dry_run=False)
    mod.ensure_source_exists(f, dry_run=True)
    assert "dry-run warning: source path does not exist yet" in capsys.readouterr().out
    allowed = folder(tmp_path, create_missing_source=True)
    mod.ensure_source_exists(allowed, dry_run=True)
    assert not allowed.source.exists()
    mod.ensure_source_exists(allowed, dry_run=False)
    assert allowed.source.is_dir()
    mod.ensure_source_exists(allowed, dry_run=False)  # already there: nothing to do


def test_acl_helpers_build_the_expected_setfacl_commands(commands):
    mod.apply_acl_entry(P("/p"), "u:a", "--x", True)
    mod.apply_default_acl_entry(P("/p"), "u:a", "rwx", False, recursive=True)
    mod.apply_default_acl_entry(P("/p"), "u:a", "rwx", False, recursive=False)
    mod.apply_acl_mode(P("/p"), "g:b", "r-x", False, recursive=True)
    mod.apply_acl_mode(P("/p"), "g:b", "r-x", False, recursive=False)
    assert commands == [
        (["setfacl", "-m", "u:a:--x", "/p"], True),
        (["setfacl", "-R", "-d", "-m", "u:a:rwx", "/p"], False),
        (["setfacl", "-d", "-m", "u:a:rwx", "/p"], False),
        (["setfacl", "-R", "-m", "g:b:r-x", "/p"], False),
        (["setfacl", "-m", "g:b:r-x", "/p"], False),
    ]


def test_parent_paths_run_from_the_top_down_and_exclude_the_root_and_the_path_itself():
    assert mod.parent_paths(P("/mnt/data/docs")) == [P("/mnt"), P("/mnt/data")]
    assert mod.parent_paths(P("/mnt")) == []
    assert mod.parent_paths(P("/")) == []


def test_skipping_acls_touches_nothing(tmp_path, commands, monkeypatch, capsys):
    monkeypatch.setattr(mod.shutil, "which", lambda name: pytest.fail("must not even look for setfacl"))
    mod.apply_folder_acls(settings(tmp_path), folder(tmp_path), dry_run=False, skip_acls=True)
    assert commands == [] and "skip ACL updates" in capsys.readouterr().out


def test_acls_grant_traverse_on_the_parents_and_the_configured_access_on_the_source(tmp_path, commands, monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/setfacl")
    src = P("/mnt/data/docs")
    mod.apply_folder_acls(settings(tmp_path, acl_user="reader", acl_group="readers"),
                          folder(tmp_path, source=src, acl_mode="r-x", default_acl_mode="r--"),
                          dry_run=True, skip_acls=False)  # fmt: skip
    assert [c for c, dry in commands if dry is True] == [c for c, _ in commands]
    assert [c for c, _ in commands] == [
        ["setfacl", "-m", "u:reader:--x", "/mnt"], ["setfacl", "-m", "g:readers:--x", "/mnt"],
        ["setfacl", "-m", "u:reader:--x", "/mnt/data"], ["setfacl", "-m", "g:readers:--x", "/mnt/data"],
        ["setfacl", "-R", "-m", "u:reader:r-x", "/mnt/data/docs"],
        ["setfacl", "-R", "-d", "-m", "u:reader:r--", "/mnt/data/docs"],
        ["setfacl", "-R", "-m", "g:readers:r-x", "/mnt/data/docs"],
        ["setfacl", "-R", "-d", "-m", "g:readers:r--", "/mnt/data/docs"],
    ]


def test_without_a_group_or_parent_traverse_only_the_user_acl_on_the_source_is_set(tmp_path, commands, monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/setfacl")
    mod.apply_folder_acls(settings(tmp_path), folder(tmp_path, source=P("/mnt/d"), grant_parent_traverse=False,
                                                     recursive_acl=False), dry_run=False, skip_acls=False)  # fmt: skip
    assert [c for c, _ in commands] == [
        ["setfacl", "-m", "u:smb:rwx", "/mnt/d"],
        ["setfacl", "-d", "-m", "u:smb:rwx", "/mnt/d"],
    ]


def test_acls_need_setfacl(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(mod.ConfigError, match="setfacl"):
        mod.apply_folder_acls(settings(tmp_path), folder(tmp_path), dry_run=True, skip_acls=False)


# --- mount points -------------------------------------------------------------------------


def test_each_level_of_the_target_is_created_and_a_mounted_level_keeps_its_permissions(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "ensure_directory", lambda *a, **k: seen.append((a[0], a[3], k.get("skip_existing_permissions"))))
    monkeypatch.setattr(mod, "is_mountpoint", lambda path: path.name == "deep")
    f = folder(tmp_path, target_relative=mod.pathlib.PurePosixPath("deep/er"), target_path=tmp_path / "share/deep/er")
    mod.ensure_mountpoint(settings(tmp_path, mount_dir_mode=0o750), f, 1, 2, dry_run=False)
    assert seen == [(tmp_path / "share" / "deep", 0o750, True), (tmp_path / "share" / "deep" / "er", 0o750, False)]


def bind(monkeypatch, *, mounted, sources=(), matches=False):
    monkeypatch.setattr(mod, "is_mountpoint", lambda path: mounted)
    monkeypatch.setattr(mod, "mounted_sources", lambda path: list(sources))
    monkeypatch.setattr(mod, "mounted_source_matches", lambda s, e: matches)


def test_an_unmounted_target_is_bind_mounted_unless_mounting_is_skipped(tmp_path, commands, monkeypatch, capsys):
    bind(monkeypatch, mounted=False)
    f = folder(tmp_path)
    mod.ensure_bind_mount(f, dry_run=False, skip_mount=False)
    assert commands == [(["mount", "--bind", str(f.source), str(f.target_path)], False)]
    commands.clear()
    mod.ensure_bind_mount(f, dry_run=False, skip_mount=True)
    assert commands == [] and "skip mount for" in capsys.readouterr().out


def test_an_active_matching_mount_is_left_alone(tmp_path, commands, monkeypatch, capsys):
    bind(monkeypatch, mounted=True, sources=["/dev/a"], matches=True)
    mod.ensure_bind_mount(folder(tmp_path), dry_run=False, skip_mount=False)
    assert commands == [] and "bind mount already active" in capsys.readouterr().out


def test_a_mount_whose_source_cannot_be_determined_is_an_error(tmp_path, commands, monkeypatch):
    bind(monkeypatch, mounted=True, sources=[])
    with pytest.raises(mod.ConfigError, match="could not determine mounted source"):
        mod.ensure_bind_mount(folder(tmp_path), dry_run=False, skip_mount=False)
    assert commands == []


def test_a_mount_from_the_wrong_source_is_replaced_in_the_right_order_unless_skipping(tmp_path, commands, monkeypatch):
    bind(monkeypatch, mounted=True, sources=["/dev/other"], matches=False)
    f = folder(tmp_path)
    mod.ensure_bind_mount(f, dry_run=False, skip_mount=False)
    assert [c for c, _ in commands] == [["umount", str(f.target_path)], ["mount", "--bind", str(f.source), str(f.target_path)]]
    commands.clear()
    mod.ensure_bind_mount(f, dry_run=False, skip_mount=True)
    assert commands == []


# --- the managed fstab block --------------------------------------------------------------


def test_fstab_fields_are_escaped():
    assert mod.escape_fstab_field("/mnt/a b\tc\nd\\e") == "/mnt/a\\040b\\011c\\012d\\\\e"
    assert mod.escape_fstab_field("/plain") == "/plain"


def test_the_block_lists_only_persistent_folders_with_their_options(tmp_path):
    folders = [
        folder(tmp_path, source=P("/mnt/a b"), target_path=P("/srv/share/a")),
        folder(tmp_path, name="skip", persist=False, source=P("/mnt/skip"), target_path=P("/srv/share/skip")),
        folder(tmp_path, name="late", source=P("/mnt/late"), target_path=P("/srv/share/late"), requires_mounts_for="/mnt/luks"),
    ]
    block = mod.build_fstab_block(P("/cfg/x.ini"), folders)
    assert block.splitlines() == [
        mod.MANAGED_BLOCK_START,
        "# Managed by scripts/setup_bind_share.py --config /cfg/x.ini",
        "/mnt/a\\040b /srv/share/a none bind 0 0",
        "/mnt/late /srv/share/late none bind,x-systemd.requires-mounts-for=/mnt/luks 0 0",
        mod.MANAGED_BLOCK_END,
    ]
    assert block.endswith("\n") and "/mnt/skip" not in block


def fstab_folders(tmp_path):
    return [folder(tmp_path, source=P("/mnt/a"), target_path=P("/srv/share/a"))]


def test_a_dry_run_prints_the_block_and_writes_nothing(tmp_path, capsys):
    fstab = tmp_path / "fstab"
    fstab.write_text("original\n")
    mod.write_fstab_block(fstab, P("/cfg.ini"), fstab_folders(tmp_path), dry_run=True)
    out = capsys.readouterr().out
    assert mod.MANAGED_BLOCK_START in out and fstab.read_text() == "original\n"


def test_a_missing_fstab_is_created_with_just_the_block(tmp_path):
    fstab = tmp_path / "fstab"
    mod.write_fstab_block(fstab, P("/cfg.ini"), fstab_folders(tmp_path), dry_run=False)
    assert fstab.read_text() == mod.build_fstab_block(P("/cfg.ini"), fstab_folders(tmp_path))
    assert stat.S_IMODE(fstab.stat().st_mode) == 0o644  # an fstab is world-readable


@pytest.mark.parametrize("existing", ["UUID=1 / ext4 defaults 0 1\n", "UUID=1 / ext4 defaults 0 1"])
def test_the_block_is_appended_after_the_existing_entries(tmp_path, existing):
    fstab = tmp_path / "fstab"
    fstab.write_text(existing)
    mod.write_fstab_block(fstab, P("/cfg.ini"), fstab_folders(tmp_path), dry_run=False)
    text = fstab.read_text()
    assert text.startswith("UUID=1 / ext4 defaults 0 1\n\n" + mod.MANAGED_BLOCK_START)
    assert text.count(mod.MANAGED_BLOCK_START) == 1


@pytest.mark.parametrize(
    "existing",
    [
        "keep 1\n" + "# --- snowbridge bind mounts: managed block start ---\nkeep 2\n",
        "keep 1\n" + "# --- snowbridge bind mounts: managed block end ---\nkeep 2\n",
        "# --- snowbridge bind mounts: managed block end ---\nkeep\n# --- snowbridge bind mounts: managed block start ---\n",
        ("# --- snowbridge bind mounts: managed block start ---\n# --- snowbridge bind mounts: managed block end ---\n") * 2,
    ],
    ids=["only-start", "only-end", "end-before-start", "two-blocks"],
)
def test_an_fstab_with_unbalanced_markers_is_refused_and_left_untouched(tmp_path, existing):
    """A stray marker would make the next run replace everything between it and the new block."""
    fstab = tmp_path / "fstab"
    fstab.write_text(existing)
    with pytest.raises(mod.ConfigError, match="unbalanced managed block"):
        mod.write_fstab_block(fstab, P("/cfg.ini"), fstab_folders(tmp_path), dry_run=False)
    assert fstab.read_text() == existing and [p.name for p in tmp_path.iterdir()] == ["fstab"]


def test_an_existing_block_is_replaced_in_place_and_everything_else_is_kept(tmp_path):
    fstab = tmp_path / "fstab"
    fstab.write_text("before\n\n" + mod.build_fstab_block(P("/old.ini"), fstab_folders(tmp_path)) + "\nafter 1\nafter 2\n")
    mod.write_fstab_block(fstab, P("/new.ini"), fstab_folders(tmp_path), dry_run=False)
    text = fstab.read_text()
    assert "/new.ini" in text and "/old.ini" not in text
    assert text.startswith("before\n\n") and text.endswith("after 1\nafter 2\n")
    assert text.count(mod.MANAGED_BLOCK_START) == 1
    mod.write_fstab_block(fstab, P("/new.ini"), fstab_folders(tmp_path), dry_run=False)
    assert fstab.read_text() == text  # writing it again changes nothing


def test_the_fstabs_mode_is_kept_and_no_temporary_file_is_left(tmp_path):
    fstab = tmp_path / "fstab"
    fstab.write_text("x\n")
    fstab.chmod(0o640)
    mod.write_fstab_block(fstab, P("/cfg.ini"), fstab_folders(tmp_path), dry_run=False)
    assert stat.S_IMODE(fstab.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["fstab"]


def test_a_symlinked_fstab_is_updated_through_the_link_not_replaced_by_a_file(tmp_path):
    real = tmp_path / "real-fstab"
    real.write_text("x\n")
    link = tmp_path / "fstab"
    link.symlink_to(real)
    mod.write_fstab_block(link, P("/cfg.ini"), fstab_folders(tmp_path), dry_run=False)
    assert link.is_symlink() and mod.MANAGED_BLOCK_START in real.read_text()


def test_a_write_that_fails_part_way_leaves_the_original_fstab_intact(tmp_path, capsys):
    """Disk-full during the write must never leave a truncated /etc/fstab.

    ``capsys`` keeps output capture in memory: with pytest's default capture the file-size limit
    below would also hit the capture file, the error would come from a ``print``, and the fstab
    would never be touched, so the test would pass for the wrong reason.
    """
    fstab = tmp_path / "fstab"
    original = "UUID=1 / ext4 defaults 0 1\n"
    fstab.write_text(original)
    folders = [folder(tmp_path, name=f"f{i}", source=P(f"/mnt/data{i}"), target_path=P(f"/srv/share/f{i}")) for i in range(20)]
    previous = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (len(original) + 40, hard))  # room for only part of the block
    try:
        with pytest.raises(OSError) as info:
            mod.write_fstab_block(fstab, P("/cfg.ini"), folders, dry_run=False)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, previous)
    # the "update managed fstab block" line was printed first, so the failure came from the write
    assert "update managed fstab block" in capsys.readouterr().out and info.value.errno != 0
    assert fstab.read_text() == original
    assert [p.name for p in tmp_path.iterdir()] == ["fstab"]  # and the partial temporary file is gone


# --- the whole command --------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    src = tmp_path / "data"
    src.mkdir()
    ini = write_ini(
        tmp_path,
        f"[global]\nshare_root = {tmp_path / 'share'}\nsmb_user = {ME}\n[folder docs]\nsource = {src}\ncreate_missing_source = yes\n",
    )
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(mod, "is_mountpoint", lambda path: False)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    # pytest's temporary directory is under /tmp, which is (rightly) protected; allow it here only
    monkeypatch.setattr(mod, "PROTECTED_SOURCE_PREFIXES", tuple(p for p in mod.PROTECTED_SOURCE_PREFIXES if p != P("/tmp")))
    return SimpleNamespace(tmp=tmp_path, ini=ini, src=src)


def run_main(world, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["prog", "--config", str(world.ini), *argv])
    return mod.main()


def test_a_dry_run_plans_everything_and_changes_nothing(world, commands, monkeypatch, capsys):
    assert run_main(world, monkeypatch, "--dry-run", "--write-fstab", "--fstab-path", str(world.tmp / "fstab")) == 0
    out = capsys.readouterr().out
    assert "configure folder docs" in out and mod.MANAGED_BLOCK_START in out
    assert not (world.tmp / "share").exists() and not (world.tmp / "fstab").exists()
    assert all(dry for _, dry in commands)


def test_a_real_run_creates_the_share_applies_acls_mounts_and_writes_fstab(world, commands, monkeypatch):
    fstab = world.tmp / "fstab"
    assert run_main(world, monkeypatch, "--write-fstab", "--fstab-path", str(fstab)) == 0
    assert (world.tmp / "share" / "docs").is_dir()
    assert any(c[:2] == ["mount", "--bind"] for c, _ in commands) and any(c[0] == "setfacl" for c, _ in commands)
    assert str(world.src) in fstab.read_text()


def test_skip_flags_skip_mounting_and_acls(world, commands, monkeypatch):
    assert run_main(world, monkeypatch, "--skip-mount", "--skip-acls") == 0
    assert commands == []


def test_applying_needs_root_unless_it_is_a_dry_run(world, commands, monkeypatch, capsys):
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    assert run_main(world, monkeypatch) == 2
    assert "run this script as root" in capsys.readouterr().err
    assert not (world.tmp / "share").exists()
    assert run_main(world, monkeypatch, "--dry-run") == 0


def test_configuration_problems_exit_with_two_and_one_line(world, commands, monkeypatch, capsys):
    world.ini.write_text("[global]\n")
    assert run_main(world, monkeypatch) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "Traceback" not in err


def test_a_failing_command_is_reported_with_its_exit_code(world, monkeypatch, capsys):
    def boom(command, dry_run):
        raise subprocess.CalledProcessError(5, command)

    monkeypatch.setattr(mod, "run_command", boom)
    assert run_main(world, monkeypatch) == 5
    assert capsys.readouterr().err.startswith("command failed: ")
    monkeypatch.setattr(mod, "run_command", lambda c, d: (_ for _ in ()).throw(subprocess.CalledProcessError(0, c)))
    assert run_main(world, monkeypatch) == 1  # a zero status inside a failure is still a failure


def test_missing_host_tools_stop_the_run_first(world, commands, monkeypatch, capsys):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None if name == "findmnt" else "/bin/x")
    assert run_main(world, monkeypatch) == 2
    assert "findmnt" in capsys.readouterr().err and not (world.tmp / "share").exists()
    assert mod.os is os
