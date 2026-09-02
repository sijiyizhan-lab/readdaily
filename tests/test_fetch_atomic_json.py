import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(FETCH_SCRIPTS))


def load_lib():
    spec = importlib.util.spec_from_file_location(
        "readdaily_fetch_atomic_lib", FETCH_SCRIPTS / "lib.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lib = load_lib()


class AtomicJSONTests(unittest.TestCase):
    def test_durable_makedirs_flushes_each_new_parent_from_empty_archive(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            target = base / "archive" / "paper" / "2026-09-03"
            synced = []
            real_fsync_directory = lib.fsync_directory

            def observe(path):
                synced.append(Path(path))
                return real_fsync_directory(path)

            with mock.patch.object(
                lib, "fsync_directory", side_effect=observe
            ):
                created = lib.durable_makedirs(target)

            self.assertEqual(
                [Path(path).resolve() for path in created],
                [
                    (base / "archive").resolve(),
                    (base / "archive" / "paper").resolve(),
                    target.resolve(),
                ],
            )
            self.assertEqual(
                synced,
                [
                    base.resolve(),
                    (base / "archive").resolve(),
                    (base / "archive" / "paper").resolve(),
                ],
            )
            self.assertTrue(target.is_dir())

    def test_atomic_write_fsyncs_file_before_replace_then_parent(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            events = []
            real_fsync = lib.os.fsync
            real_replace = lib.os.replace

            def observe_fsync(descriptor):
                events.append("fsync")
                return real_fsync(descriptor)

            def observe_replace(source, destination, **kwargs):
                events.append("replace")
                return real_replace(source, destination, **kwargs)

            with mock.patch.object(
                lib.os, "fsync", side_effect=observe_fsync
            ), mock.patch.object(
                lib.os, "replace", side_effect=observe_replace
            ):
                lib.durable_atomic_write_bytes(target, b"durable")

            self.assertEqual(events, ["fsync", "replace", "fsync"])
            self.assertEqual(target.read_bytes(), b"durable")

    def test_parent_fsync_failure_is_reported_after_atomic_replace(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            with mock.patch.object(
                lib, "fsync_directory", side_effect=OSError("flush failed")
            ):
                with self.assertRaisesRegex(OSError, "flush failed"):
                    lib.durable_atomic_write_bytes(target, b"new-state")

            # The namespace mutation may already be visible, but the public
            # operation must not report success when its durability is unknown.
            self.assertEqual(target.read_bytes(), b"new-state")
            self.assertEqual(list(Path(td).glob(".state.json.*.tmp")), [])

    def test_delete_helpers_flush_parent_and_propagate_flush_failure(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            file_path = base / "state.json"
            tree_path = base / "issue"
            file_path.write_bytes(b"state")
            tree_path.mkdir()
            (tree_path / "issue.json").write_bytes(b"{}")
            observed = []

            def fail_after_delete(parent):
                observed.append(Path(parent))
                raise OSError("delete flush failed")

            with mock.patch.object(
                lib, "fsync_directory", side_effect=fail_after_delete
            ):
                with self.assertRaisesRegex(OSError, "delete flush failed"):
                    lib.durable_unlink(file_path)
            self.assertFalse(file_path.exists())

            with mock.patch.object(
                lib, "fsync_directory", side_effect=fail_after_delete
            ):
                with self.assertRaisesRegex(OSError, "delete flush failed"):
                    lib.durable_rmtree(tree_path)
            self.assertFalse(tree_path.exists())
            self.assertEqual(observed, [base.resolve(), base.resolve()])

    def test_each_save_uses_a_unique_sibling_temporary_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            temporary_paths = []
            real_replace = lib.os.replace

            def observe_replace(source, destination, **kwargs):
                temporary_paths.append(str(source))
                return real_replace(source, destination, **kwargs)

            with mock.patch.object(lib.os, "replace", side_effect=observe_replace):
                lib.save_json(str(target), {"writer": 1})
                lib.save_json(str(target), {"writer": 2})

            self.assertEqual(len(temporary_paths), 2)
            self.assertEqual(len(set(temporary_paths)), 2)
            self.assertTrue(all(Path(path).name.startswith(".state.json.")
                                for path in temporary_paths))
            self.assertFalse(any(target.parent.joinpath(path).exists()
                                 for path in temporary_paths))


if __name__ == "__main__":
    unittest.main()
