#!/usr/bin/env python3
"""Stage and publish a KeePassXC cold-start merge.

The command never reads database passwords. KeePassXC prompts for them. A
working copy is merged first; ``--apply`` is required before the authoritative
vault is replaced, and the prior vault is retained as a timestamped backup.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_VAULT_BYTES = 512 * 1024 * 1024


class ColdStartError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise ColdStartError(message)


def validate_vault(path: Path, label: str) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        fail(f"{label} must be an absolute path: {path}")
    if path.is_symlink() or not path.is_file():
        fail(f"{label} must be a regular, non-symlink file: {path}")
    if path.stat().st_size > MAX_VAULT_BYTES:
        fail(f"{label} exceeds {MAX_VAULT_BYTES} bytes: {path}")
    return path


def merge_into(destination: Path, source: Path, same_credentials: bool = False) -> None:
    command = ["keepassxc-cli", "merge"]
    if same_credentials:
        command.append("--same-credentials")
    command.extend([str(destination), str(source)])
    result = subprocess.run(command)
    if result.returncode:
        fail(f"KeePassXC merge failed for {source.name}")


def publish(
    master: Path, candidates: list[Path], apply: bool, same_credentials: bool = False
) -> None:
    target_dir = master.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{master.name}.cold-start-", suffix=".tmp", dir=target_dir, delete=False
    ) as handle:
        working = Path(handle.name)
    failed: list[Path] = []
    merged = 0
    try:
        shutil.copy2(master, working)
        for candidate in candidates:
            # Merge into the disposable copy in both modes. KeePassXC's own
            # --dry-run compares each candidate to the original working file,
            # so later candidates would not see earlier candidate changes.
            with tempfile.NamedTemporaryFile(
                prefix=f".{master.name}.candidate-", suffix=".tmp", dir=target_dir, delete=False
            ) as handle:
                candidate_working = Path(handle.name)
            try:
                shutil.copy2(working, candidate_working)
                merge_into(candidate_working, candidate, same_credentials)
                os.replace(candidate_working, working)
                merged += 1
            except ColdStartError as error:
                print(f"skipped {candidate}: {error}", file=sys.stderr)
                failed.append(candidate)
            finally:
                if candidate_working.exists():
                    candidate_working.unlink()
        if merged == 0:
            fail("no candidate databases merged; authoritative vault unchanged")
        if not apply:
            print(f"plan complete; {merged} candidate(s) merged into temporary copy")
            if failed:
                fail(f"{len(failed)} candidate(s) skipped; authoritative vault unchanged")
            print("authoritative vault unchanged")
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = target_dir / f"{master.name}.pre-cold-start-{timestamp}"
        os.replace(master, backup)
        os.replace(working, master)
        print(f"published {master}")
        print(f"backup    {backup}")
        if failed:
            fail(f"published {merged} successful candidate(s); {len(failed)} candidate(s) skipped")
    finally:
        if working.exists():
            working.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge candidate KeePassXC vaults into an authoritative cold-start copy."
    )
    parser.add_argument("--master", required=True, type=Path, help="Authoritative vault to update.")
    parser.add_argument(
        "--candidate",
        required=True,
        action="append",
        type=Path,
        help="Candidate vault; repeat for Mac, phone, or other sources.",
    )
    parser.add_argument(
        "--same-credentials",
        action="store_true",
        help="Use one unlock prompt per merge when both vaults share credentials.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Publish the merged result; without this flag only a merge plan runs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        master = validate_vault(args.master, "master")
        candidates = [validate_vault(path, "candidate") for path in args.candidate]
        if len(set(candidates)) != len(candidates):
            fail("candidate vaults must be distinct")
        if master in candidates:
            fail("master must not also be a candidate")
        if shutil.which("keepassxc-cli") is None:
            fail("keepassxc-cli is not installed")
        publish(master, candidates, args.apply, args.same_credentials)
        return 0
    except (ColdStartError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
