import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LIB_PATH = ROOT / "skills" / "newspaper-fetch" / "scripts" / "lib.py"
SPEC = importlib.util.spec_from_file_location("atomic_issue_tree_lib", LIB_PATH)
lib = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lib)


def load_adapter(name):
    path = LIB_PATH.parent / "adapters" / (name + ".py")
    spec = importlib.util.spec_from_file_location(
        "atomic_issue_tree_" + name, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree_snapshot(root):
    root = Path(root)
    if not root.exists():
        return None
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class AtomicIssueTreeCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.target = self.base / "paper" / "2026-09-02"
        (self.target / "pages").mkdir(parents=True)
        (self.target / "text").mkdir()
        (self.target / "pages" / "old.jpg").write_bytes(b"old-page")
        (self.target / "text" / "old.txt").write_bytes(b"old-text")
        (self.target / "issue.json").write_text(
            json.dumps({"version": "old"}), encoding="utf-8"
        )
        self.before = tree_snapshot(self.target)
        self.files = [
            ("pages/01.jpg", b"new-page-1"),
            ("pages/02.jpg", b"new-page-2"),
        ]
        self.issue = {"version": "new", "editions": [1, 2]}

    def tearDown(self):
        self.temp.cleanup()

    def assert_no_transaction_artifacts(self):
        parent = self.target.parent
        self.assertEqual(list(parent.glob(".2026-09-02.staging.*")), [])
        self.assertEqual(list(parent.glob(".2026-09-02.previous.*")), [])

    def test_staged_file_write_failure_preserves_existing_tree_byte_for_byte(self):
        real_write = lib._write_bytes_fsync
        calls = 0

        def fail_second(path, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return real_write(path, payload)

        with mock.patch.object(
            lib, "_write_bytes_fsync", side_effect=fail_second
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                lib.commit_issue_tree(str(self.target), self.files, self.issue)

        self.assertEqual(tree_snapshot(self.target), self.before)
        self.assert_no_transaction_artifacts()

    def test_issue_json_write_failure_preserves_existing_tree_byte_for_byte(self):
        with mock.patch.object(
            lib, "save_json", side_effect=OSError("metadata write failed")
        ):
            with self.assertRaisesRegex(OSError, "metadata write failed"):
                lib.commit_issue_tree(str(self.target), self.files, self.issue)

        self.assertEqual(tree_snapshot(self.target), self.before)
        self.assert_no_transaction_artifacts()

    def test_old_tree_move_failure_preserves_existing_tree_and_leaks_nothing(self):
        real_replace = lib.os.replace
        failed = False

        def fail_old_tree_move_once(source, destination, **kwargs):
            nonlocal failed
            if (not failed
                    and Path(source).name == self.target.name
                    and ".previous." in Path(destination).name):
                failed = True
                raise OSError("cannot move old tree")
            return real_replace(source, destination, **kwargs)

        with mock.patch.object(
            lib.os, "replace", side_effect=fail_old_tree_move_once
        ):
            with self.assertRaisesRegex(OSError, "cannot move old tree"):
                lib.commit_issue_tree(str(self.target), self.files, self.issue)

        self.assertTrue(failed)
        self.assertEqual(tree_snapshot(self.target), self.before)
        self.assert_no_transaction_artifacts()

    def test_new_tree_replace_failure_rolls_back_and_cleans_backup(self):
        real_replace = lib.os.replace
        failed = False

        def fail_commit_once(source, destination, **kwargs):
            nonlocal failed
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                not failed
                and ".staging." in source_path.name
                and destination_path.name == self.target.name
            ):
                failed = True
                raise OSError("rename interrupted")
            return real_replace(source, destination, **kwargs)

        with mock.patch.object(lib.os, "replace", side_effect=fail_commit_once):
            with self.assertRaisesRegex(OSError, "rename interrupted"):
                lib.commit_issue_tree(str(self.target), self.files, self.issue)

        self.assertTrue(failed)
        self.assertEqual(tree_snapshot(self.target), self.before)
        self.assert_no_transaction_artifacts()

    def test_commit_and_recovery_double_failure_is_typed_batch_fatal(self):
        real_rename = lib.ArchiveSession.rename

        def fail_commit_and_restore(session, source, destination):
            source_name = Path(source).name
            destination_name = Path(destination).name
            if ".staging." in source_name and destination_name == self.target.name:
                raise OSError("commit rename failed")
            if ".previous." in source_name and destination_name == self.target.name:
                raise lib.ArchiveConflictError("backup restore identity changed")
            return real_rename(session, source, destination)

        with mock.patch.object(
                lib.ArchiveSession, "rename",
                new=fail_commit_and_restore,
        ):
            with self.assertRaises(lib.ArchiveTransactionError) as caught:
                lib.commit_issue_tree(str(self.target), self.files, self.issue)

        self.assertIsInstance(caught.exception.__cause__, lib.ArchiveConflictError)
        self.assertIn(
            lib.ArchiveTransactionError, lib.ARCHIVE_FATAL_EXCEPTIONS
        )

    def test_parent_fsync_failure_after_old_move_restores_old_tree(self):
        real_fsync_directory = lib.fsync_directory
        parent_syncs = 0

        def fail_first_live_parent_sync(path):
            nonlocal parent_syncs
            if Path(path).resolve() == self.target.parent.resolve():
                parent_syncs += 1
                # #1 durably creates the private sibling; #2 follows the
                # live old-tree -> backup rename.
                if parent_syncs == 2:
                    raise OSError("old-tree parent fsync failed")
            return real_fsync_directory(path)

        with mock.patch.object(
            lib, "fsync_directory", side_effect=fail_first_live_parent_sync
        ):
            with self.assertRaisesRegex(
                OSError, "old-tree parent fsync failed"
            ):
                lib.commit_issue_tree(str(self.target), self.files, self.issue)

        self.assertGreaterEqual(parent_syncs, 3)
        self.assertEqual(tree_snapshot(self.target), self.before)
        self.assert_no_transaction_artifacts()

    def test_parent_fsync_failure_after_new_move_rolls_back_old_tree(self):
        real_fsync_directory = lib.fsync_directory
        parent_syncs = 0

        def fail_second_live_parent_sync(path):
            nonlocal parent_syncs
            if Path(path).resolve() == self.target.parent.resolve():
                parent_syncs += 1
                # #1 creates staging, #2 moves old -> backup, #3 commits new.
                if parent_syncs == 3:
                    raise OSError("new-tree parent fsync failed")
            return real_fsync_directory(path)

        with mock.patch.object(
            lib, "fsync_directory", side_effect=fail_second_live_parent_sync
        ):
            with self.assertRaisesRegex(
                OSError, "new-tree parent fsync failed"
            ):
                lib.commit_issue_tree(str(self.target), self.files, self.issue)

        self.assertGreaterEqual(parent_syncs, 5)
        self.assertEqual(tree_snapshot(self.target), self.before)
        self.assert_no_transaction_artifacts()

    def test_first_issue_parent_fsync_failure_leaves_no_live_issue(self):
        fresh = self.target.parent / "2026-09-03"
        real_fsync_directory = lib.fsync_directory
        parent_syncs = 0

        def fail_first_live_parent_sync(path):
            nonlocal parent_syncs
            if Path(path).resolve() == fresh.parent.resolve():
                parent_syncs += 1
                # #1 creates staging; #2 follows first live issue commit.
                if parent_syncs == 2:
                    raise OSError("first issue parent fsync failed")
            return real_fsync_directory(path)

        with mock.patch.object(
            lib, "fsync_directory", side_effect=fail_first_live_parent_sync
        ):
            with self.assertRaisesRegex(
                OSError, "first issue parent fsync failed"
            ):
                lib.commit_issue_tree(str(fresh), self.files, self.issue)

        self.assertGreaterEqual(parent_syncs, 3)
        self.assertFalse(fresh.exists())
        self.assertEqual(list(fresh.parent.glob(".2026-09-03.*")), [])

    def test_persistent_parent_fsync_failure_reports_incomplete_cleanup(self):
        real_fsync_directory = lib.fsync_directory

        def fail_live_parent(path):
            if Path(path).resolve() == self.target.parent.resolve():
                raise OSError("persistent parent fsync failure")
            return real_fsync_directory(path)

        with mock.patch.object(
            lib, "fsync_directory", side_effect=fail_live_parent
        ):
            with self.assertRaisesRegex(
                lib.ArchiveTransactionError, "耐久清理未完成"
            ):
                lib.commit_issue_tree(str(self.target), self.files, self.issue)

        self.assertEqual(tree_snapshot(self.target), self.before)
        self.assert_no_transaction_artifacts()

    def test_success_flushes_staging_before_renames_and_backup_delete(self):
        events = []
        real_fsync_tree = lib.fsync_tree
        real_replace = lib.os.replace
        real_rmtree = lib.durable_rmtree

        def observe_tree(path):
            events.append(("tree", Path(path).name))
            return real_fsync_tree(path)

        def observe_replace(source, destination, **kwargs):
            events.append(
                ("replace", Path(source).name, Path(destination).name)
            )
            return real_replace(source, destination, **kwargs)

        def observe_rmtree(path, *args, **kwargs):
            events.append(("rmtree", Path(path).name))
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(lib, "fsync_tree", side_effect=observe_tree), \
                mock.patch.object(lib.os, "replace", side_effect=observe_replace), \
                mock.patch.object(lib, "durable_rmtree", side_effect=observe_rmtree):
            lib.commit_issue_tree(str(self.target), self.files, self.issue)

        tree_index = next(
            index for index, event in enumerate(events) if event[0] == "tree"
        )
        old_move_index = next(
            index for index, event in enumerate(events)
            if event[0] == "replace" and event[1] == self.target.name
        )
        new_move_index = next(
            index for index, event in enumerate(events)
            if event[0] == "replace" and ".staging." in event[1]
        )
        backup_delete_index = next(
            index for index, event in enumerate(events)
            if event[0] == "rmtree" and ".previous." in event[1]
        )
        self.assertLess(tree_index, old_move_index)
        self.assertLess(old_move_index, new_move_index)
        self.assertLess(new_move_index, backup_delete_index)
        self.assert_no_transaction_artifacts()

    def test_success_replaces_the_complete_tree_in_one_transaction(self):
        lib.commit_issue_tree(str(self.target), self.files, self.issue)

        snapshot = tree_snapshot(self.target)
        self.assertEqual(snapshot["pages/01.jpg"], b"new-page-1")
        self.assertEqual(snapshot["pages/02.jpg"], b"new-page-2")
        self.assertNotIn("pages/old.jpg", snapshot)
        self.assertNotIn("text/old.txt", snapshot)
        self.assertEqual(
            json.loads(snapshot["issue.json"].decode("utf-8")), self.issue
        )
        self.assertTrue((self.target / "text").is_dir())
        self.assert_no_transaction_artifacts()

    def test_all_network_fetchers_use_only_the_atomic_tree_committer(self):
        for adapter_name in (
            "cms_index", "founder", "mobile_epaper", "paper_api"
        ):
            with self.subTest(adapter=adapter_name):
                source = inspect.getsource(load_adapter(adapter_name).fetch)
                self.assertIn("lib.commit_issue_tree", source)
                self.assertNotIn("open(", source)
                self.assertNotIn("lib.save_json", source)


if __name__ == "__main__":
    unittest.main()
