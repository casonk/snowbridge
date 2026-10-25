import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "keepass_cold_start", Path(__file__).parents[1] / "scripts/keepass_cold_start.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ColdStartTests(unittest.TestCase):
    def test_rejects_relative_and_symlink_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "vault.kdbx"
            vault.write_bytes(b"synthetic")
            link = root / "link.kdbx"
            link.symlink_to(vault)
            with self.assertRaises(module.ColdStartError):
                module.validate_vault(Path("relative.kdbx"), "candidate")
            with self.assertRaises(module.ColdStartError):
                module.validate_vault(link, "candidate")

    def test_plan_does_not_replace_master(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.kdbx"
            candidate = root / "candidate.kdbx"
            master.write_bytes(b"master")
            candidate.write_bytes(b"candidate")
            with patch.object(module, "merge_into") as merge:
                module.publish(master, [candidate], apply=False)
            self.assertEqual(master.read_bytes(), b"master")
            merge.assert_called_once()
            self.assertFalse(list(root.glob("*.pre-cold-start-*")))

    def test_apply_keeps_backup_and_replaces_master(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "master.kdbx"
            candidate = root / "candidate.kdbx"
            master.write_bytes(b"master")
            candidate.write_bytes(b"candidate")
            def fake_merge(destination, source, dry_run):
                destination.write_bytes(b"merged")

            with patch.object(module, "merge_into", side_effect=fake_merge) as merge:
                module.publish(master, [candidate], apply=True)
            backups = list(root.glob("master.kdbx.pre-cold-start-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"master")
            self.assertEqual(master.read_bytes(), b"merged")
            merge.assert_called_once()


if __name__ == "__main__":
    unittest.main()
