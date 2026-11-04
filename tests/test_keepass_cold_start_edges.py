"""Validation, merge staging, atomic publication and failure handling of the cold-start tool.

``keepassxc-cli`` is replaced by a fake that appends the candidate's bytes to the
destination, so merges are observable without any real vault or password.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import keepass_cold_start as mod


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.master = self.vault("master.kdbx", b"M", 0o600)
        self.commands: list[list[str]] = []
        self.fail_for: set[str] = set()

    def vault(self, name: str, data: bytes, mode: int = 0o600) -> Path:
        path = self.root / name
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def fake_run(self, command, **_kw):
        self.commands.append(list(command))
        destination, source = Path(command[-2]), Path(command[-1])
        if source.name in self.fail_for:
            return subprocess.CompletedProcess(command, 1)
        with destination.open("ab") as handle:
            handle.write(b"+" + source.read_bytes())
        return subprocess.CompletedProcess(command, 0)

    def run_main(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(mod.subprocess, "run", side_effect=self.fake_run),
            mock.patch.object(mod.shutil, "which", return_value="/usr/bin/keepassxc-cli"),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = mod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def leftovers(self):
        return sorted(p.name for p in self.root.iterdir() if p.name.startswith("."))


class ValidationTests(Base):
    def test_validate_vault_rules(self) -> None:
        self.assertEqual(mod.validate_vault(self.master, "master"), self.master)
        with self.assertRaisesRegex(mod.ColdStartError, "absolute path"):
            mod.validate_vault(Path("relative.kdbx"), "master")
        with self.assertRaisesRegex(mod.ColdStartError, "regular, non-symlink"):
            mod.validate_vault(self.root / "missing.kdbx", "candidate")
        with self.assertRaisesRegex(mod.ColdStartError, "regular, non-symlink"):
            mod.validate_vault(self.root, "candidate")
        link = self.root / "link.kdbx"
        link.symlink_to(self.master)
        with self.assertRaisesRegex(mod.ColdStartError, "regular, non-symlink"):
            mod.validate_vault(link, "candidate")

    def test_oversized_vault_is_refused(self) -> None:
        with mock.patch.object(mod, "MAX_VAULT_BYTES", 1):
            with self.assertRaisesRegex(mod.ColdStartError, "exceeds 1 bytes"):
                mod.validate_vault(self.vault("big.kdbx", b"xx"), "candidate")
            mod.validate_vault(self.vault("ok.kdbx", b"x"), "candidate")

    def test_tilde_is_expanded(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(mod.validate_vault(Path("~/master.kdbx"), "master"), self.master)

    def test_cli_rejects_bad_combinations_before_running_anything(self) -> None:
        a = self.vault("a.kdbx", b"A")
        cases = {
            "must be distinct": ("--master", str(self.master), "--candidate", str(a), "--candidate", str(a)),
            "must not also be a candidate": ("--master", str(self.master), "--candidate", str(self.master)),
            "candidate must be": ("--master", str(self.master), "--candidate", "nope.kdbx"),
            "master must be": ("--master", "nope.kdbx", "--candidate", str(a)),
        }
        for fragment, argv in cases.items():
            with self.subTest(fragment):
                code, _, err = self.run_main(*argv)
                self.assertEqual(code, 2)
                self.assertIn(fragment, err)
        self.assertEqual(self.commands, [])

    def test_the_same_vault_reached_through_a_symlinked_directory_is_not_a_second_candidate(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        code, _, err = self.run_main(
            "--master", str(self.master), "--candidate", str(alias / "master.kdbx")
        )
        self.assertEqual(code, 2)
        self.assertIn("must not also be a candidate", err)
        self.assertEqual(self.commands, [])

    def test_master_given_through_a_symlinked_directory_is_still_the_master(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        code, _, err = self.run_main(
            "--master", str(alias / "master.kdbx"), "--candidate", str(self.master)
        )
        self.assertEqual(code, 2)
        self.assertIn("must not also be a candidate", err)

    def test_two_aliases_of_one_candidate_are_not_distinct(self) -> None:
        a = self.vault("a.kdbx", b"A")
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        code, _, err = self.run_main(
            "--master", str(self.master), "--candidate", str(a), "--candidate", str(alias / "a.kdbx")
        )
        self.assertEqual(code, 2)
        self.assertIn("must be distinct", err)

    def test_missing_keepassxc_cli_is_a_clean_error(self) -> None:
        a = self.vault("a.kdbx", b"A")
        with (
            mock.patch.object(mod.shutil, "which", return_value=None),
            redirect_stderr(io.StringIO()) as err,
        ):
            code = mod.main(["--master", str(self.master), "--candidate", str(a)])
        self.assertEqual(code, 2)
        self.assertIn("keepassxc-cli is not installed", err.getvalue())

    def test_parser_requires_master_and_candidate(self) -> None:
        for argv in ([], ["--master", "/x"], ["--candidate", "/x"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                mod.main(argv)


class MergeCommandTests(Base):
    def test_merge_command_shape(self) -> None:
        a = self.vault("a.kdbx", b"A")
        for same in (False, True):
            self.commands.clear()
            with mock.patch.object(mod.subprocess, "run", side_effect=self.fake_run):
                mod.merge_into(self.master, a, same)
            expected = ["keepassxc-cli", "merge"] + (["--same-credentials"] if same else [])
            self.assertEqual(self.commands, [expected + [str(self.master), str(a)]])

    def test_merge_never_passes_a_password_or_captures_the_terminal(self) -> None:
        a = self.vault("a.kdbx", b"A")
        with mock.patch.object(mod.subprocess, "run", side_effect=self.fake_run) as run:
            mod.merge_into(self.master, a)
        self.assertEqual(run.call_args.kwargs, {})
        self.assertFalse(any(part.startswith(("-p", "--password", "--key")) for part in self.commands[0]))

    def test_failed_merge_names_the_candidate_not_the_path(self) -> None:
        a = self.vault("a.kdbx", b"A")
        self.fail_for.add("a.kdbx")
        with mock.patch.object(mod.subprocess, "run", side_effect=self.fake_run):
            with self.assertRaisesRegex(mod.ColdStartError, "merge failed for a.kdbx"):
                mod.merge_into(self.master, a)


class PlanModeTests(Base):
    def test_plan_merges_into_a_copy_and_leaves_the_vault_alone(self) -> None:
        a = self.vault("a.kdbx", b"A")
        b = self.vault("b.kdbx", b"B")
        code, out, _ = self.run_main("--master", str(self.master), "--candidate", str(a), "--candidate", str(b))
        self.assertEqual(code, 0)
        self.assertIn("plan complete; 2 candidate(s) merged", out)
        self.assertIn("authoritative vault unchanged", out)
        self.assertEqual(self.master.read_bytes(), b"M")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual([p.name for p in self.root.iterdir() if "pre-cold-start" in p.name], [])

    def test_later_candidates_see_earlier_merges(self) -> None:
        a = self.vault("a.kdbx", b"A")
        b = self.vault("b.kdbx", b"B")
        seen: list[bytes] = []
        original = self.fake_run

        def spy(command, **kw):
            seen.append(Path(command[-2]).read_bytes())
            return original(command, **kw)

        with (
            mock.patch.object(mod.subprocess, "run", side_effect=spy),
            mock.patch.object(mod.shutil, "which", return_value="x"),
            redirect_stdout(io.StringIO()),
        ):
            mod.main(["--master", str(self.master), "--candidate", str(a), "--candidate", str(b)])
        self.assertEqual(seen, [b"M", b"M+A"])

    def test_a_skipped_candidate_makes_the_plan_fail(self) -> None:
        a = self.vault("a.kdbx", b"A")
        b = self.vault("b.kdbx", b"B")
        self.fail_for.add("b.kdbx")
        code, out, err = self.run_main("--master", str(self.master), "--candidate", str(a), "--candidate", str(b))
        self.assertEqual(code, 2)
        self.assertIn("skipped", err)
        self.assertIn("1 candidate(s) skipped; authoritative vault unchanged", err)
        self.assertNotIn("authoritative vault unchanged\n", out.replace("plan complete", ""))
        self.assertEqual(self.master.read_bytes(), b"M")
        self.assertEqual(self.leftovers(), [])

    def test_nothing_merged_is_an_error_in_both_modes(self) -> None:
        a = self.vault("a.kdbx", b"A")
        self.fail_for.add("a.kdbx")
        for extra in ((), ("--apply",)):
            with self.subTest(extra=extra):
                code, _, err = self.run_main("--master", str(self.master), "--candidate", str(a), *extra)
                self.assertEqual(code, 2)
                self.assertIn("no candidate databases merged; authoritative vault unchanged", err)
                self.assertEqual(self.master.read_bytes(), b"M")
                self.assertEqual(self.leftovers(), [])


class ApplyModeTests(Base):
    def test_apply_publishes_atomically_and_keeps_a_backup(self) -> None:
        a = self.vault("a.kdbx", b"A")
        code, out, _ = self.run_main("--master", str(self.master), "--candidate", str(a), "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.master.read_bytes(), b"M+A")
        backups = [p for p in self.root.iterdir() if p.name.startswith("master.kdbx.pre-cold-start-")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"M")
        self.assertRegex(backups[0].name, r"\.pre-cold-start-\d{8}T\d{6}Z$")
        self.assertIn(f"published {self.master}", out)
        self.assertIn(f"backup    {backups[0]}", out)
        self.assertEqual(self.leftovers(), [])

    def test_modes_are_preserved_and_never_loosened(self) -> None:
        a = self.vault("a.kdbx", b"A", 0o644)
        os.chmod(self.master, 0o600)
        old = os.umask(0)
        try:
            self.run_main("--master", str(self.master), "--candidate", str(a), "--apply")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.master.stat().st_mode), 0o600)
        backup = next(p for p in self.root.iterdir() if "pre-cold-start" in p.name)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_master_is_never_missing_even_if_publication_is_interrupted(self) -> None:
        a = self.vault("a.kdbx", b"A")
        real_replace = os.replace
        calls = {"n": 0}

        def crash_on_publish(src, dst):
            calls["n"] += 1
            if Path(dst) == self.master:
                raise OSError("power cut")
            return real_replace(src, dst)

        with mock.patch.object(mod.os, "replace", side_effect=crash_on_publish):
            code, _, err = self.run_main("--master", str(self.master), "--candidate", str(a), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("power cut", err)
        self.assertTrue(self.master.exists())
        self.assertEqual(self.master.read_bytes(), b"M")
        self.assertEqual(self.leftovers(), [])

    def test_backup_name_collision_never_overwrites_an_earlier_backup(self) -> None:
        a = self.vault("a.kdbx", b"A")
        fixed = mock.Mock()
        fixed.strftime.return_value = "20260101T000000Z"
        with mock.patch.object(mod, "datetime") as dt:
            dt.now.return_value = fixed
            self.run_main("--master", str(self.master), "--candidate", str(a), "--apply")
            first = (self.root / "master.kdbx.pre-cold-start-20260101T000000Z").read_bytes()
            code, _, err = self.run_main("--master", str(self.master), "--candidate", str(a), "--apply")
        self.assertEqual(first, b"M")
        self.assertEqual(code, 2)
        self.assertIn("backup already exists", err)
        self.assertEqual((self.root / "master.kdbx.pre-cold-start-20260101T000000Z").read_bytes(), b"M")
        self.assertEqual(self.master.read_bytes(), b"M+A")

    def test_backup_falls_back_to_a_copy_where_hard_links_are_unavailable(self) -> None:
        a = self.vault("a.kdbx", b"A")
        with mock.patch.object(mod.os, "link", side_effect=OSError("no links here")):
            code, _, _ = self.run_main("--master", str(self.master), "--candidate", str(a), "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.master.read_bytes(), b"M+A")
        backup = next(p for p in self.root.iterdir() if "pre-cold-start" in p.name)
        self.assertEqual((backup.read_bytes(), stat.S_IMODE(backup.stat().st_mode)), (b"M", 0o600))

    def test_partial_failure_publishes_the_successes_then_reports_failure(self) -> None:
        a = self.vault("a.kdbx", b"A")
        b = self.vault("b.kdbx", b"B")
        self.fail_for.add("a.kdbx")
        code, out, err = self.run_main(
            "--master", str(self.master), "--candidate", str(a), "--candidate", str(b), "--apply"
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.master.read_bytes(), b"M+B")
        self.assertIn("published 1 successful candidate(s); 1 candidate(s) skipped", err)
        self.assertIn("published", out)

    def test_master_directory_is_created_if_needed_for_the_working_copy(self) -> None:
        # publish() is also callable directly; the target directory must exist after it.
        a = self.vault("a.kdbx", b"A")
        with (
            mock.patch.object(mod.subprocess, "run", side_effect=self.fake_run),
            redirect_stdout(io.StringIO()),
        ):
            mod.publish(self.master, [a], apply=False)
        self.assertEqual(self.master.read_bytes(), b"M")

    def test_same_credentials_flag_reaches_every_merge(self) -> None:
        a = self.vault("a.kdbx", b"A")
        b = self.vault("b.kdbx", b"B")
        self.run_main("--master", str(self.master), "--candidate", str(a), "--candidate", str(b), "--same-credentials")
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(all("--same-credentials" in c for c in self.commands))


if __name__ == "__main__":
    unittest.main()
