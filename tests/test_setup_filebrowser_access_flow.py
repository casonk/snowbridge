"""Applying the File Browser access config: parsing, host preparation, and the container commands.

The script runs as root, edits the web env file and drives a container that holds the user
database, and every user's password travels on the command line of that container. So the tests
pin that no command is ever echoed with a password in it (a dry run prints every command; a
failure puts the command in the error), that placeholders are never applied, that a malformed
password line is normalised or refused rather than guessed, and that the host paths get their
modes and ACLs. Containers, root, ACL tools and the filesystem outside ``tmp_path`` are replaced.
"""

from __future__ import annotations

import grp
import os
import pwd
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import setup_filebrowser_access as mod

ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name
PASSWORD = "p@ss-w0rd-CANARY"
REAL_REQUIRE_ROOT = mod.require_root


@pytest.fixture
def calls(monkeypatch):
    """Record every command run_command is asked to run; real execution is replaced."""
    seen = []

    def fake(command, dry_run, check=True, secrets=()):
        seen.append((list(command), dry_run, tuple(secrets)))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mod, "run_command", fake)
    return seen


def runtime(**kw):
    fields = dict(
        web_env_file=Path("/env"), web_setup_script=Path("/setup.sh"), mode="private-vpn",
        container_runtime="podman", filebrowser_image="img:1", container_name="fb",
        share_mount_path="/srv", database_path="/database/filebrowser.db",
        run_as_account=ME, run_as_group=None, sync_web_env_uid_gid=True, restart_strategy="recreate",
    )  # fmt: skip
    fields.update(kw)
    return mod.RuntimeSpec(**fields)


# --- small helpers ------------------------------------------------------------------------


def test_log_prints_and_fail_raises(capsys):
    mod.log("hi")
    assert capsys.readouterr().out == "hi\n"
    with pytest.raises(mod.SetupError, match="boom"):
        mod.fail("boom")


def test_keepass_entries_are_tried_bare_then_under_the_snowbridge_folder():
    assert mod._candidate_keepass_entries("  web/x ") == ("web/x",)
    assert mod._candidate_keepass_entries("x") == ("x", "snowbridge/x")
    assert mod._candidate_keepass_entries("   ") == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Alice Smith", "alice_smith"), ("  bob-2 ", "bob_2"), ("__x__", "x"), ("A.B.C", "a_b_c"), ("!!!", "")],
)
def test_auto_pass_keys_are_normalised(raw, expected):
    assert mod._normalize_auto_pass_key(raw) == expected


def test_a_repo_auto_pass_config_supplies_the_profile_and_per_user_entries(tmp_path, monkeypatch):
    ini = tmp_path / "auto-pass.ini"
    ini.write_text("[auto_pass]\nprofile = alt-profile\n[filebrowser]\nalice_password_keepass_entry = web/alice\nempty =   \n")
    monkeypatch.setattr(mod, "AUTO_PASS_CONFIG", ini)
    assert mod.load_repo_auto_pass_config() == {"profile": "alt-profile", "alice_password_keepass_entry": "web/alice"}
    monkeypatch.setattr(mod, "AUTO_PASS_CONFIG", tmp_path / "absent.ini")
    assert mod.load_repo_auto_pass_config() == {}
    bad = tmp_path / "bad.ini"
    bad.write_text("not an ini file\n")
    monkeypatch.setattr(mod, "AUTO_PASS_CONFIG", bad)
    with pytest.raises(mod.SetupError, match="cannot read auto-pass config"):
        mod.load_repo_auto_pass_config()


def test_the_root_check(monkeypatch):
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    with pytest.raises(mod.SetupError, match="run as root"):
        mod.require_root()
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    mod.require_root()


def test_relative_paths_resolve_against_their_base_and_absolute_ones_are_kept(tmp_path):
    assert mod.resolve_repo_path("/etc/x", tmp_path) == Path("/etc/x")
    assert mod.resolve_repo_path("sub/../y", tmp_path) == (tmp_path / "y").resolve()


def test_the_defaults_of_the_command_line(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog"])
    args = mod.parse_args()
    assert args.config == str(mod.DEFAULT_CONFIG) and args.dry_run is False and args.init_local_configs is False


# --- creating the local config ------------------------------------------------------------


def test_an_existing_local_config_is_kept(tmp_path, capsys):
    source, target = tmp_path / "example.toml", tmp_path / "local.toml"
    source.write_text("new")
    target.write_text("mine")
    mod.copy_if_missing(source, target, dry_run=False)
    assert target.read_text() == "mine" and "keep existing" in capsys.readouterr().out


def test_a_missing_local_config_is_created_owner_only_or_only_announced_in_a_dry_run(tmp_path):
    source, target = tmp_path / "example.toml", tmp_path / "deep" / "local.toml"
    source.write_text("placeholder")
    source.chmod(0o644)
    mod.copy_if_missing(source, target, dry_run=True)
    assert not target.exists()
    mod.copy_if_missing(source, target, dry_run=False)
    assert target.read_text() == "placeholder" and (target.stat().st_mode & 0o777) == 0o600


# --- tolerant password lines --------------------------------------------------------------


def test_only_valid_toml_strings_parse_as_strings():
    assert mod.try_parse_toml_string('"abc"') == "abc"
    assert mod.try_parse_toml_string("'lit'") == "lit"
    assert mod.try_parse_toml_string("abc") is None and mod.try_parse_toml_string("12") is None


@pytest.mark.parametrize(
    ("line", "changed", "expected"),
    [
        ('password = "fine"', False, 'password = "fine"'),
        ('username = "not a password"', False, 'username = "not a password"'),
        ("no equals sign here", False, "no equals sign here"),
        ("password = pa$$ word", True, 'password = "pa$$ word"'),
        ("password = pa\\ss", True, 'password = "pa\\\\ss"'),
        ("password = 'unmatched", None, None),
    ],
)
def test_a_password_line_that_is_not_valid_toml_is_quoted_safely(line, changed, expected, tmp_path):
    if changed is None:
        with pytest.raises(mod.SetupError, match="unmatched quote"):
            mod.normalize_password_assignment_line(line, 3, tmp_path / "c.toml")
        return
    assert mod.normalize_password_assignment_line(line, 3, tmp_path / "c.toml") == (expected, changed)


def test_an_ambiguous_or_empty_password_line_is_refused_not_guessed(tmp_path):
    with pytest.raises(mod.SetupError, match="inline comment marker"):
        mod.normalize_password_assignment_line("password = abc # tail", 4, tmp_path / "c.toml")
    with pytest.raises(mod.SetupError, match="password value is empty"):
        mod.normalize_password_assignment_line("password =   ", 5, tmp_path / "c.toml")


def test_normalising_a_file_is_reported_and_only_written_outside_a_dry_run(tmp_path, capsys):
    config = tmp_path / "c.toml"
    config.write_text('[[users]]\nusername = "a"\npassword = pa$$ word\n')
    text = mod.normalize_password_assignments(config, dry_run=True)
    assert 'password = "pa$$ word"' in text and "pa$$ word\n" in config.read_text()
    assert "would normalize TOML password strings" in capsys.readouterr().out
    mod.normalize_password_assignments(config, dry_run=False)
    assert 'password = "pa$$ word"' in config.read_text()
    assert "on line(s): 3" in capsys.readouterr().out
    clean = tmp_path / "clean.toml"
    clean.write_text('password = "ok"')  # no trailing newline
    assert mod.normalize_password_assignments(clean, dry_run=False) == 'password = "ok"'


def test_loading_a_config_handles_missing_valid_repairable_and_broken_files(tmp_path):
    with pytest.raises(mod.SetupError, match="config not found"):
        mod.load_toml_config(tmp_path / "none.toml", dry_run=False)
    good = tmp_path / "good.toml"
    good.write_text('[app]\nroot = "/srv"\n')
    assert mod.load_toml_config(good, dry_run=False) == {"app": {"root": "/srv"}}
    repair = tmp_path / "repair.toml"
    repair.write_text('[[users]]\nusername = "a"\npassword = pa$$ word\n')
    assert mod.load_toml_config(repair, dry_run=True)["users"][0]["password"] == "pa$$ word"
    broken = tmp_path / "broken.toml"
    broken.write_text("[app\nroot = 1\n")
    with pytest.raises(mod.SetupError, match=r"broken.toml:\d+:\d+:"):
        mod.load_toml_config(broken, dry_run=False)


# --- the config sections ------------------------------------------------------------------


def test_runtime_defaults_and_overrides(tmp_path):
    spec = mod.parse_runtime_spec({}, tmp_path / "c.toml")
    assert (spec.mode, spec.container_runtime, spec.container_name) == ("private-vpn", "auto", "snowbridge-filebrowser")
    assert (spec.share_mount_path, spec.database_path) == ("/srv", "/database/filebrowser.db")
    assert spec.filebrowser_image is None and spec.run_as_group is None
    assert spec.sync_web_env_uid_gid is True and spec.restart_strategy == "recreate"
    spec = mod.parse_runtime_spec(
        {"runtime": {"filebrowser_image": " img:2 ", "run_as_group": "grp", "mode": "private-vpn-mtls",
                     "web_env_file": "env.local", "sync_web_env_uid_gid": False, "restart_strategy": "none"}},  # fmt: skip
        tmp_path / "c.toml",
    )
    assert spec.filebrowser_image == "img:2" and spec.run_as_group == "grp" and spec.mode == "private-vpn-mtls"
    assert spec.web_env_file == (tmp_path / "env.local").resolve() and spec.sync_web_env_uid_gid is False
    blank = mod.parse_runtime_spec({"runtime": {"filebrowser_image": "", "run_as_group": ""}}, tmp_path / "c.toml")
    assert blank.filebrowser_image is None and blank.run_as_group is None
    with pytest.raises(mod.SetupError, match=r"invalid \[runtime\]"):
        mod.parse_runtime_spec({"runtime": "x"}, tmp_path / "c.toml")


def test_the_app_section(tmp_path):
    assert mod.parse_app_spec({}, tmp_path / "c.toml").root == "/srv"
    assert mod.parse_app_spec({"app": {"root": "/srv/share"}}, tmp_path / "c.toml").root == "/srv/share"
    with pytest.raises(mod.SetupError, match=r"invalid \[app\]"):
        mod.parse_app_spec({"app": []}, tmp_path / "c.toml")


def test_the_auth_section(tmp_path):
    c = tmp_path / "c.toml"
    plain, mtls = runtime(), runtime(mode="private-vpn-mtls")
    assert mod.parse_auth_spec({}, c, plain) is None
    default = mod.parse_auth_spec({}, c, mtls)
    assert (default.method, default.proxy_header, default.proxy_username, default.hide_login_button) == (
        "proxy", "X-Snowbridge-Auth-User", "snowbridge", True)  # fmt: skip
    json_auth = mod.parse_auth_spec({"auth": {}}, c, plain)
    assert (json_auth.method, json_auth.proxy_header, json_auth.hide_login_button) == ("json", None, False)
    proxy = mod.parse_auth_spec({"auth": {"method": "proxy", "proxy_header": " X-U ", "proxy_username": " bob "}}, c, plain)
    assert (proxy.proxy_header, proxy.proxy_username, proxy.hide_login_button) == ("X-U", "bob", True)
    shown = mod.parse_auth_spec({"auth": {"method": "proxy", "hide_login_button": False}}, c, plain)
    assert shown.hide_login_button is False
    assert mod.parse_auth_spec({"auth": {}}, c, mtls).method == "proxy"
    for bad, message in (
        ({"auth": "x"}, r"invalid \[auth\]"),
        ({"auth": {"method": "  "}}, "method is empty"),
        ({"auth": {"method": "proxy", "proxy_header": " "}}, "proxy_header is required"),
        ({"auth": {"method": "proxy", "proxy_username": " "}}, "proxy_username is required"),
    ):
        with pytest.raises(mod.SetupError, match=message):
            mod.parse_auth_spec(bad, c, plain)


def test_the_keepass_profile_precedence():
    assert mod.parse_keepass_profile({"keepass_profile": " work "}, {"profile": "repo"}) == "work"
    assert mod.parse_keepass_profile({}, {"profile": "repo"}) == "repo"
    assert mod.parse_keepass_profile({}, {}) == mod.DEFAULT_KEEPASS_PROFILE


def users(data, repo=None, profile="main-profile"):
    return mod.parse_users(data, Path("/c.toml"), keepass_profile=profile, repo_auto_pass=repo or {})


def test_users_are_parsed_with_defaults():
    (user,) = users({"users": [{"username": " alice ", "password": PASSWORD}]})
    assert (user.username, user.password, user.scope, user.admin) == ("alice", PASSWORD, ".", False)
    (admin,) = users({"users": [{"username": "root", "password": "x", "scope": "/a", "admin": True}]})
    assert (admin.scope, admin.admin) == ("/a", True)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "no .*users.* entries"),
        ({"users": []}, "no .*users.* entries"),
        ({"users": ["x"]}, r"invalid \[\[users\]\] entry"),
        ({"users": [{"password": "x"}]}, "missing username"),
        ({"users": [{"username": "a"}]}, "placeholder password"),
        ({"users": [{"username": "a", "password": "CHOOSE_A_PASSWORD"}]}, "placeholder password"),
        ({"users": [{"username": "a", "password": "REPLACE_ME"}]}, "placeholder password"),
    ],
)
def test_a_user_without_a_real_password_is_refused(data, message):
    with pytest.raises(mod.SetupError, match=message):
        users(data)


def test_a_placeholder_password_is_replaced_from_the_password_manager(monkeypatch):
    looked = []
    monkeypatch.setattr(mod, "_resolve_keepass_value", lambda entry, field, profile: looked.append((entry, field, profile)) or PASSWORD)
    (a,) = users({"users": [{"username": "alice", "password": "CHOOSE_X", "password_keepass_entry": "web/alice"}]}, profile="work")
    (b,) = users({"users": [{"username": "Bob Jones", "password": ""}]}, repo={"bob_jones_password_keepass_entry": "web/bob"})
    (c,) = users({"users": [{"username": "carol", "password": "literal-ok", "password_keepass_entry": "web/carol"}]})
    assert (a.password, b.password, c.password) == (PASSWORD, PASSWORD, "literal-ok")
    assert looked == [("web/alice", "password", "work"), ("web/bob", "password", "main-profile")]


def test_a_manager_lookup_that_still_gives_a_placeholder_is_refused(monkeypatch):
    monkeypatch.setattr(mod, "_resolve_keepass_value", lambda *a: "REPLACE_ME")
    with pytest.raises(mod.SetupError, match="placeholder password"):
        users({"users": [{"username": "a", "password": "", "password_keepass_entry": "x"}]})


def test_proxy_auth_needs_its_user_to_exist():
    spec = mod.AuthSpec("proxy", "H", "bob", True)
    members = [mod.UserSpec("alice", "p", ".", False)]
    with pytest.raises(mod.SetupError, match="'bob' is not present"):
        mod.validate_auth_users(spec, members, Path("/c.toml"))
    mod.validate_auth_users(spec, members + [mod.UserSpec("bob", "p", ".", False)], Path("/c.toml"))
    mod.validate_auth_users(None, members, Path("/c.toml"))
    mod.validate_auth_users(mod.AuthSpec("json", None, None, False), members, Path("/c.toml"))


def test_the_container_runtime_is_detected(monkeypatch):
    present = {"docker"}
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}" if name in present else None)
    assert mod.detect_container_runtime("auto") == "docker"
    present.add("podman")
    assert mod.detect_container_runtime("auto") == "podman"
    assert mod.detect_container_runtime("docker") == "docker"
    with pytest.raises(mod.SetupError, match="container runtime not found: nerdctl"):
        mod.detect_container_runtime("nerdctl")
    present.clear()
    with pytest.raises(mod.SetupError, match="no supported container runtime"):
        mod.detect_container_runtime("auto")


# --- the web env file ---------------------------------------------------------------------


def test_the_env_file_is_read_ignoring_comments_and_keeping_equals_signs(tmp_path):
    env = tmp_path / "env"
    env.write_text("# comment\n# OLD_KEY=commented-out\n\nA=1\n  B = two words \nC=x=y\nnovalue\n")
    assert mod.load_env_file(env) == {"A": "1", "B": "two words", "C": "x=y"}  # a commented-out setting is not read
    with pytest.raises(mod.SetupError, match="web env file not found"):
        mod.load_env_file(tmp_path / "none")


def test_the_image_comes_from_the_runtime_then_the_env_then_the_default():
    assert mod.resolve_filebrowser_image(runtime(filebrowser_image="a"), {"FILEBROWSER_IMAGE": "b"}) == "a"
    assert mod.resolve_filebrowser_image(runtime(filebrowser_image=None), {"FILEBROWSER_IMAGE": " b "}) == "b"
    assert mod.resolve_filebrowser_image(runtime(filebrowser_image=None), {}) == mod.DEFAULT_FILEBROWSER_IMAGE


def test_one_env_setting_is_replaced_or_appended_and_a_dry_run_writes_nothing(tmp_path, capsys):
    env = tmp_path / "env"
    env.write_text("SNOWBRIDGE_UID=1\nSNOWBRIDGE_UID_OTHER=9\nKEEP=me\n")
    mod.replace_env_setting(env, "SNOWBRIDGE_UID", "1000", dry_run=True)
    assert env.read_text().startswith("SNOWBRIDGE_UID=1\n") and "would set SNOWBRIDGE_UID=1000" in capsys.readouterr().out
    mod.replace_env_setting(env, "SNOWBRIDGE_UID", "1000", dry_run=False)
    assert env.read_text() == "SNOWBRIDGE_UID=1000\nSNOWBRIDGE_UID_OTHER=9\nKEEP=me\n"  # a longer key is untouched
    mod.replace_env_setting(env, "NEW", "v", dry_run=False)
    assert env.read_text().endswith("KEEP=me\nNEW=v\n")


# --- the host -----------------------------------------------------------------------------


def test_without_a_group_override_the_accounts_own_group_is_used(monkeypatch):
    # uid and gid differ here, unlike on many machines where they happen to match
    monkeypatch.setattr(mod.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1234, pw_gid=5678))
    assert mod.lookup_account_ids("svc", None) == (1234, 5678)
    monkeypatch.setattr(mod.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=9999))
    assert mod.lookup_account_ids("svc", "other") == (1234, 9999)


def test_host_accounts_are_looked_up_with_an_optional_group_override():
    assert mod.lookup_account_ids(ME, None) == (os.getuid(), pwd.getpwnam(ME).pw_gid)
    assert mod.lookup_account_ids(ME, MY_GROUP) == (os.getuid(), os.getgid())
    with pytest.raises(mod.SetupError, match="host account not found: no-such-user-xyz"):
        mod.lookup_account_ids("no-such-user-xyz", None)
    with pytest.raises(mod.SetupError, match="host group not found: no-such-group-xyz"):
        mod.lookup_account_ids(ME, "no-such-group-xyz")


def test_the_database_and_config_directories_are_private_to_the_service_account(tmp_path, calls):
    share = tmp_path / "share"
    share.mkdir()
    mod.ensure_runtime_host_paths(str(share), "/db", "/cfg", os.getuid(), os.getgid(), dry_run=False)
    commands = [c for c, _, _ in calls]
    uid, gid = str(os.getuid()), str(os.getgid())
    assert ["install", "-d", "-m", "0750", "-o", uid, "-g", gid, "/db"] in commands
    assert ["install", "-d", "-m", "0750", "-o", uid, "-g", gid, "/cfg"] in commands
    assert ["chown", "-R", f"{uid}:{gid}", "/db"] in commands and ["chown", "-R", f"{uid}:{gid}", "/cfg"] in commands


def test_a_missing_share_root_is_an_error_and_a_dry_run_only_reports(tmp_path, calls, capsys):
    with pytest.raises(mod.SetupError, match="share root does not exist"):
        mod.ensure_runtime_host_paths(str(tmp_path / "gone"), "/db", "/cfg", 1, 1, dry_run=False)
    share = tmp_path / "share"
    share.mkdir()
    calls.clear()
    mod.ensure_runtime_host_paths(str(share), "/db", "/cfg", 1, 1, dry_run=True)
    assert "would verify share root exists" in capsys.readouterr().out
    assert all(dry for _, dry, _ in calls)  # every command was a dry-run command


def test_a_traverse_acl_is_added_only_when_the_account_neither_owns_nor_shares_the_group(tmp_path, calls):
    share = tmp_path / "share"
    share.mkdir()
    me_uid, me_gid = os.getuid(), os.getgid()
    mod.ensure_runtime_host_paths(str(share), "/db", "/cfg", me_uid + 1, me_gid + 1, dry_run=False)
    assert ["setfacl", "-m", f"u:{me_uid + 1}:r-x", str(share)] in [c for c, _, _ in calls]
    calls.clear()
    mod.ensure_runtime_host_paths(str(share), "/db", "/cfg", me_uid, me_gid + 1, dry_run=False)
    mod.ensure_runtime_host_paths(str(share), "/db", "/cfg", me_uid + 1, me_gid, dry_run=False)
    assert not any(c[0] == "setfacl" for c, _, _ in calls)


def test_the_needed_host_tools_are_checked(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None if name == "setfacl" else "/bin/x")
    with pytest.raises(mod.SetupError, match="required host command not found: setfacl"):
        mod.require_host_tools()
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/x")
    mod.require_host_tools()


# --- running commands, and not repeating a password ---------------------------------------


@pytest.fixture
def real_run(monkeypatch):
    runs = []

    def fake(command, **kwargs):
        runs.append((command, kwargs))
        return fake.result

    fake.result = subprocess.CompletedProcess([], 0, "out text", "err text")
    monkeypatch.setattr(mod.subprocess, "run", fake)
    return SimpleNamespace(runs=runs, fake=fake)


def test_a_dry_run_prints_the_quoted_command_and_runs_nothing(real_run, capsys):
    result = mod.run_command(["echo", "a b", "c'd"], dry_run=True)
    assert capsys.readouterr().out == "$ echo 'a b' 'c'\"'\"'d'\n"
    assert result.returncode == 0 and real_run.runs == []


def test_a_real_run_passes_output_through_and_returns_the_result(real_run, capsys):
    result = mod.run_command(["tool"], dry_run=False)
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("out text", "err text") and result.stdout == "out text"
    assert real_run.runs[0][1] == {"text": True, "capture_output": True, "check": False}


def test_a_failing_command_is_an_error_unless_checking_is_off(real_run):
    real_run.fake.result = subprocess.CompletedProcess([], 3, "", "")
    with pytest.raises(mod.SetupError, match="exit code 3"):
        mod.run_command(["tool", "x"], dry_run=False)
    assert mod.run_command(["tool"], dry_run=False, check=False).returncode == 3


def test_a_dry_run_never_prints_a_password(capsys):
    mod.run_command(["tool", "users", "add", "alice", PASSWORD, "--scope", "."], dry_run=True, secrets=[PASSWORD])
    out = capsys.readouterr().out
    assert PASSWORD not in out and "<redacted>" in out and "alice" in out


def test_a_failure_never_repeats_the_password_in_its_error(real_run):
    real_run.fake.result = subprocess.CompletedProcess([], 1, "", "")
    with pytest.raises(mod.SetupError) as info:
        mod.run_command(["tool", "users", "update", "alice", "--password", PASSWORD], dry_run=False, secrets=[PASSWORD])
    assert PASSWORD not in str(info.value) and "<redacted>" in str(info.value)


def test_only_exact_secret_arguments_are_hidden_and_blank_secrets_hide_nothing():
    assert mod.render_command(["a", "s3", "b"], secrets=["s3"]) == "a '<redacted>' b"
    assert mod.render_command(["a", "xs3x"], secrets=["s3"]) == "a xs3x"
    assert mod.render_command(["a", ""], secrets=[""]) == "a ''"
    assert mod.render_command(["a b"], secrets=()) == "'a b'"


def test_the_container_state_is_read_from_inspect(real_run):
    assert mod.container_running("podman", "fb", dry_run=True) is False and real_run.runs == []
    real_run.fake.result = subprocess.CompletedProcess([], 0, " True\n", "")
    assert mod.container_running("podman", "fb", dry_run=False) is True
    assert real_run.runs[0][0] == ["podman", "inspect", "-f", "{{.State.Running}}", "fb"]
    real_run.fake.result = subprocess.CompletedProcess([], 0, "false\n", "")
    assert mod.container_running("podman", "fb", dry_run=False) is False
    real_run.fake.result = subprocess.CompletedProcess([], 1, "true\n", "")
    assert mod.container_running("podman", "fb", dry_run=False) is False


def test_a_running_container_is_stopped_and_remembered(monkeypatch, calls):
    monkeypatch.setattr(mod, "container_running", lambda *a: True)
    assert mod.stop_container_if_running("podman", "fb", dry_run=False) is True
    assert calls[0][0] == ["podman", "stop", "fb"]
    calls.clear()
    monkeypatch.setattr(mod, "container_running", lambda *a: False)
    assert mod.stop_container_if_running("podman", "fb", dry_run=False) is False and calls == []


def test_the_throwaway_container_runs_as_the_service_account_with_private_mounts():
    command = mod.build_filebrowser_run_command("podman", 968, 969, "/srv/share", "/db", "/cfg", "img:1", "/srv", ["users", "ls"])
    assert command[:3] == ["podman", "run", "--rm"]
    assert command[command.index("--user") + 1] == "968:969"
    assert "type=bind,src=/srv/share,dst=/srv,relabel=private,bind-propagation=rslave" in command
    assert command[command.index("--security-opt") + 1] == "label=disable"
    assert "/db:/database:Z" in command and "/cfg:/config:Z" in command
    assert command[-3:] == ["img:1", "users", "ls"]


def test_a_user_is_found_through_the_containers_exit_status(real_run):
    args = ("podman", 1, 1, "/s", "/d", "/c", "img", "/srv", "/database/fb.db", "alice")
    assert mod.filebrowser_user_exists(*args, dry_run=True) is False and real_run.runs == []
    assert mod.filebrowser_user_exists(*args, dry_run=False) is True
    assert real_run.runs[0][0][-5:] == ["users", "find", "alice", "-d", "/database/fb.db"]
    real_run.fake.result = subprocess.CompletedProcess([], 1, "", "")
    assert mod.filebrowser_user_exists(*args, dry_run=False) is False


# --- applying the state -------------------------------------------------------------------


def apply(calls, monkeypatch, *, exists=False, auth=None, members=None, dry_run=False):
    monkeypatch.setattr(mod, "filebrowser_user_exists", lambda *a: exists)
    mod.apply_filebrowser_state(
        "podman", 968, 969, "/s", "/d", "/c", runtime(), mod.AppSpec("/srv/share"), auth,
        members or [mod.UserSpec("alice", PASSWORD, "/home", True)], dry_run,
    )  # fmt: skip
    return calls


def tail(command):
    return command[command.index("img:1") + 1 :]


def test_the_root_is_set_first_and_a_new_user_is_added(calls, monkeypatch):
    apply(calls, monkeypatch)
    assert tail(calls[0][0]) == ["config", "set", "--root", "/srv/share", "-d", "/database/filebrowser.db"]
    assert tail(calls[1][0]) == ["users", "add", "alice", PASSWORD, "--scope", "/home", "--perm.admin=true", "-d", "/database/filebrowser.db"]
    assert len(calls) == 2


def test_an_existing_user_is_updated_not_added(calls, monkeypatch):
    apply(calls, monkeypatch, exists=True, members=[mod.UserSpec("bob", PASSWORD, ".", False)])
    assert tail(calls[1][0]) == ["users", "update", "bob", "--password", PASSWORD, "--scope", ".", "--perm.admin=false", "-d", "/database/filebrowser.db"]


def test_every_user_command_declares_its_password_as_a_secret(calls, monkeypatch):
    apply(calls, monkeypatch, members=[mod.UserSpec("a", "pw1", ".", False), mod.UserSpec("b", "pw2", ".", False)])
    secrets = [c[2] for c in calls]
    assert secrets == [(), ("pw1",), ("pw2",)]  # the config command carries no secret


def test_proxy_authentication_is_configured_with_its_header(calls, monkeypatch):
    apply(calls, monkeypatch, auth=mod.AuthSpec("proxy", "X-User", "alice", True))
    assert tail(calls[1][0]) == ["config", "set", "--auth.header=X-User", "--auth.method=proxy", "--hideLoginButton=true", "-d", "/database/filebrowser.db"]


def test_json_authentication_has_no_header(calls, monkeypatch):
    apply(calls, monkeypatch, auth=mod.AuthSpec("json", None, None, False))
    assert tail(calls[1][0]) == ["config", "set", "--auth.method=json", "--hideLoginButton=false", "-d", "/database/filebrowser.db"]


def test_a_dry_run_applies_nothing_and_shows_no_password(monkeypatch, capsys):
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("nothing may run"))
    mod.apply_filebrowser_state("podman", 1, 1, "/s", "/d", "/c", runtime(), mod.AppSpec("/srv"), None,
                                [mod.UserSpec("alice", PASSWORD, ".", False)], dry_run=True)  # fmt: skip
    out = capsys.readouterr().out
    assert "users add alice" in out and PASSWORD not in out and "<redacted>" in out


@pytest.mark.parametrize("strategy", ["none", "start", "recreate", "bogus"])
def test_the_web_stack_is_restarted_according_to_the_strategy(calls, strategy):
    spec = runtime(restart_strategy=strategy, mode="private-vpn-mtls")
    if strategy == "bogus":
        with pytest.raises(mod.SetupError, match="invalid runtime.restart_strategy"):
            mod.restart_web_stack(spec, "podman", True, dry_run=False)
        return
    mod.restart_web_stack(spec, "podman", True, dry_run=False)
    commands = [c for c, _, _ in calls]
    assert commands == {"none": [], "start": [["podman", "start", "fb"]],
                        "recreate": [["/setup.sh", "--mode", "private-vpn-mtls", "--recreate"]]}[strategy]  # fmt: skip


def test_start_only_restarts_a_container_that_was_running(calls):
    mod.restart_web_stack(runtime(restart_strategy="start"), "podman", False, dry_run=False)
    assert calls == []


# --- the whole command --------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    share, db, cfg = tmp_path / "share", tmp_path / "db", tmp_path / "cfg"
    share.mkdir()
    env = tmp_path / "web.env"
    env.write_text(f"SNOWBRIDGE_SHARE_ROOT={share}\nFILEBROWSER_DB_DIR={db}\nFILEBROWSER_CONFIG_DIR={cfg}\n")
    config = tmp_path / "access.toml"
    config.write_text(
        f'[runtime]\nweb_env_file = "{env}"\nrun_as_account = "{ME}"\ncontainer_runtime = "podman"\n'
        'restart_strategy = "none"\nfilebrowser_image = "img:1"\n'
        f'[[users]]\nusername = "alice"\npassword = "{PASSWORD}"\n'
    )
    monkeypatch.setattr(mod, "require_root", lambda: None)
    monkeypatch.setattr(mod, "require_host_tools", lambda: None)
    monkeypatch.setattr(mod, "AUTO_PASS_CONFIG", tmp_path / "none.ini")
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(mod, "container_running", lambda *a: False)
    monkeypatch.setattr(mod, "filebrowser_user_exists", lambda *a: False)
    return SimpleNamespace(tmp=tmp_path, env=env, config=config, share=share)


def run_main(world, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["prog", "--config", str(world.config), *argv])
    return mod.main()


def test_a_full_run_prepares_the_host_applies_the_state_and_syncs_the_env(world, calls, monkeypatch, capsys):
    assert run_main(world, monkeypatch) == 0
    assert "applied File Browser root and user state" in capsys.readouterr().out
    env_text = world.env.read_text()
    assert f"SNOWBRIDGE_UID={os.getuid()}" in env_text and f"SNOWBRIDGE_GID={os.getgid()}" in env_text
    kinds = [c[0][0] for c in calls]
    assert kinds.count("install") == 2 and kinds.count("podman") == 2  # config set + users add


def test_a_dry_run_changes_nothing_and_never_prints_a_password(world, monkeypatch, capsys):
    before = world.env.read_text()
    assert run_main(world, monkeypatch, "--dry-run") == 0
    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err
    assert world.env.read_text() == before


def test_applying_requires_root_before_anything_else(world, calls, monkeypatch, capsys):
    monkeypatch.setattr(mod, "require_root", REAL_REQUIRE_ROOT)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    before = world.env.read_text()
    with pytest.raises(mod.SetupError, match="run as root"):
        run_main(world, monkeypatch)
    assert calls == [] and world.env.read_text() == before


def test_the_env_ids_are_left_alone_when_syncing_is_switched_off(world, calls, monkeypatch):
    world.config.write_text(world.config.read_text().replace(
        'restart_strategy = "none"', 'restart_strategy = "none"\nsync_web_env_uid_gid = false'))
    before = world.env.read_text()
    assert run_main(world, monkeypatch) == 0
    assert world.env.read_text() == before and "SNOWBRIDGE_UID" not in before


def test_initialising_creates_the_local_config_and_stops(tmp_path, monkeypatch, capsys):
    target = tmp_path / "local.toml"
    monkeypatch.setattr(sys, "argv", ["prog", "--config", str(target), "--init-local-configs"])
    assert mod.main() == 0
    assert "edit the local config, then rerun with sudo" in capsys.readouterr().out
    assert target.exists() and (target.stat().st_mode & 0o777) == 0o600


def test_missing_env_settings_are_named(world, calls, monkeypatch):
    world.env.write_text("SNOWBRIDGE_SHARE_ROOT=/x\n")
    with pytest.raises(mod.SetupError, match="missing FILEBROWSER_DB_DIR"):
        run_main(world, monkeypatch)


def test_a_missing_account_stops_the_run_before_anything_changes(world, calls, monkeypatch):
    world.config.write_text(world.config.read_text().replace(f'run_as_account = "{ME}"', 'run_as_account = "no-such-user-xyz"'))
    before = world.env.read_text()
    with pytest.raises(mod.SetupError, match="host account not found"):
        run_main(world, monkeypatch)
    assert world.env.read_text() == before and calls == []


def test_the_script_reports_a_setup_error_as_one_line_when_run_directly(tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(mod.__file__)), "--config", str(tmp_path / "absent.toml")],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )  # fmt: skip
    # Not root (or without the host tools) it stops at the first check; as root, at the config.
    assert result.returncode == 1 and result.stderr.startswith("error: ")
    assert "Traceback" not in result.stderr


def test_a_password_line_with_matching_quotes_that_is_not_valid_toml_keeps_its_text(tmp_path):
    for line, literal in (("password = 'it's'", "it's"), ('password = "a"b"', 'a"b')):
        fixed, changed = mod.normalize_password_assignment_line(line, 1, tmp_path / "c.toml")
        assert changed is True
        assert mod.try_parse_toml_string(fixed.split("=", 1)[1].strip()) == literal


# --- the password manager lookup ----------------------------------------------------------


class KeepassError(Exception):
    pass


@pytest.fixture
def fake_auto_pass(monkeypatch, tmp_path):
    state = SimpleNamespace(asked=[], answers={}, loaded=[])

    def resolve(entry, attrs_map):
        state.asked.append((entry, attrs_map))
        answer = state.answers[entry]
        if isinstance(answer, Exception):
            raise answer
        return answer

    envfile = SimpleNamespace(load_config_environment=lambda path, profile=None: state.loaded.append((path, profile)))
    keepass = SimpleNamespace(KeepassCommandError=KeepassError, resolve_keepassxc_entry=resolve)
    pkg = SimpleNamespace(envfile=envfile, keepassxc=keepass)
    monkeypatch.setitem(sys.modules, "auto_pass", pkg)
    monkeypatch.setitem(sys.modules, "auto_pass.envfile", envfile)
    monkeypatch.setitem(sys.modules, "auto_pass.keepassxc", keepass)
    monkeypatch.setattr(mod, "AUTO_PASS_ROOT", tmp_path / "auto-pass")
    monkeypatch.setattr(mod.sys, "path", list(sys.path))
    return state


def test_an_entry_missing_under_every_name_is_reported_with_the_last_reason(fake_auto_pass):
    fake_auto_pass.answers = {"x": KeepassError("Entry x was not found"), "snowbridge/x": KeepassError("no entry here")}
    with pytest.raises(mod.SetupError, match=r"auto-pass lookup failed for entry 'x' field 'password': no entry here"):
        mod._resolve_keepass_value("x", "password")
    assert [e for e, _ in fake_auto_pass.asked] == ["x", "snowbridge/x"]


def test_any_other_lookup_error_stops_at_once_without_trying_another_name(fake_auto_pass):
    fake_auto_pass.answers = {"x": KeepassError("database is locked"), "snowbridge/x": {"value": "should-not-be-read"}}
    with pytest.raises(mod.SetupError, match="database is locked"):
        mod._resolve_keepass_value("x", "password")
    assert [e for e, _ in fake_auto_pass.asked] == ["x"]


def test_an_entry_without_the_field_resolves_to_an_empty_string(fake_auto_pass):
    fake_auto_pass.answers = {"x": {}}
    assert mod._resolve_keepass_value("x", "password") == ""


def test_a_lookup_that_cannot_even_import_auto_pass_is_a_setup_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "auto_pass", None)
    monkeypatch.setitem(sys.modules, "auto_pass.envfile", None)
    monkeypatch.setattr(mod, "AUTO_PASS_ROOT", tmp_path / "none")
    with pytest.raises(mod.SetupError, match="auto-pass lookup failed for entry 'x'"):
        mod._resolve_keepass_value("x", "password")


def test_a_blank_entry_name_resolves_to_nothing(fake_auto_pass):
    assert mod._resolve_keepass_value("   ", "password") == ""
    assert fake_auto_pass.asked == []
