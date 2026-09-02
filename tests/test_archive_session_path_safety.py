import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib  # noqa: E402
import local_pdf  # noqa: E402
from adapters import wechat_read  # noqa: E402


def load_fetch():
    spec = importlib.util.spec_from_file_location(
        "archive_session_fetch_test", SCRIPTS / "fetch.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetch = load_fetch()


class ArchiveSessionPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.safe_parent = self.base / "safe"
        self.safe_parent.mkdir()
        self.archive = self.safe_parent / "archive"
        self.archive.mkdir()
        self.vault = self.base / "vault"
        self.vault.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _swap_archive_root(self):
        moved = self.base / "opened-archive"
        os.rename(self.archive, moved)
        self.archive.symlink_to(self.vault, target_is_directory=True)
        return moved

    def test_atomic_json_root_swap_fails_and_never_writes_vault(self):
        real_replace = lib.os.replace
        moved = None
        swapped = False

        def replace_after_swap(source, target, **kwargs):
            nonlocal moved, swapped
            if not swapped:
                swapped = True
                moved = self._swap_archive_root()
            return real_replace(source, target, **kwargs)

        with self.assertRaises(lib.ArchiveConflictError):
            with lib.archive_session(self.archive, create=True):
                with mock.patch.object(
                    lib.os, "replace", side_effect=replace_after_swap
                ):
                    lib.save_json(self.archive / "state.json", {"ok": True})

        self.assertTrue(swapped)
        self.assertEqual(list(self.vault.rglob("*")), [])
        self.assertEqual(
            json.loads((moved / "state.json").read_text(encoding="utf-8")),
            {"ok": True},
        )
        self.assertIsNone(lib.current_archive_session())

    def test_issue_commit_root_swap_fails_without_vault_artifacts(self):
        real_replace = lib.os.replace
        swapped = False

        def replace_after_swap(source, target, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                self._swap_archive_root()
            return real_replace(source, target, **kwargs)

        with self.assertRaises(lib.ArchiveConflictError):
            with lib.archive_session(self.archive, create=True):
                with mock.patch.object(
                    lib.os, "replace", side_effect=replace_after_swap
                ):
                    lib.commit_issue_tree(
                        self.archive / "paper" / "2026-09-03",
                        [("pages/01.jpg", b"page")],
                        {"source": "paper", "date": "2026-09-03"},
                    )

        self.assertTrue(swapped)
        self.assertEqual(list(self.vault.rglob("*")), [])
        self.assertIsNone(lib.current_archive_session())

    def test_validation_to_open_ancestor_swap_is_rejected_by_fetch_main(self):
        # Precreate the path an old realpath-based implementation would accept
        # after ``safe`` is replaced by a Vault symlink.
        (self.vault / "archive").mkdir()
        baseline = sorted(path.relative_to(self.vault) for path in self.vault.rglob("*"))
        registry = {
            "archive_root": str(self.archive),
            "sources": [{
                "id": "paper", "name": "示例报纸",
                "channel": "founder", "enabled": True,
            }],
        }
        real_validate = fetch.validate_registry_write_roots_outside_vault
        swapped = False

        def validate_then_swap(*args, **kwargs):
            nonlocal swapped
            result = real_validate(*args, **kwargs)
            moved = self.base / "safe-before-swap"
            os.rename(self.safe_parent, moved)
            self.safe_parent.symlink_to(self.vault, target_is_directory=True)
            swapped = True
            return result

        argv = ["fetch.py", "--date", "2026-09-03", "--stage", "fetched"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fetch, "load_registry", return_value=registry), \
                mock.patch.object(
                    fetch, "validate_registry_write_roots_outside_vault",
                    side_effect=validate_then_swap,
                ), \
                mock.patch.object(fetch, "load_adapter") as load_adapter, \
                mock.patch.dict(os.environ, {"READDAILY_VAULT": str(self.vault)}):
            with self.assertRaises(SystemExit):
                fetch.main()

        self.assertTrue(swapped)
        load_adapter.assert_not_called()
        self.assertEqual(
            sorted(path.relative_to(self.vault) for path in self.vault.rglob("*")),
            baseline,
        )

    def test_nonexistent_root_parent_swap_uses_opened_parent_and_fails_closed(self):
        self.archive.rmdir()
        configured = self.archive / "new" / "archive"
        real_mkdir = lib.os.mkdir
        swapped = False

        def mkdir_then_swap(name, mode=0o777, **kwargs):
            nonlocal swapped
            result = real_mkdir(name, mode, **kwargs)
            if name == "new" and not swapped:
                moved = self.base / "safe-during-create"
                os.rename(self.safe_parent, moved)
                self.safe_parent.symlink_to(self.vault, target_is_directory=True)
                swapped = True
            return result

        with mock.patch.object(lib.os, "mkdir", side_effect=mkdir_then_swap):
            with self.assertRaises(lib.ArchiveConflictError):
                with lib.archive_session(configured, create=True):
                    pass

        self.assertTrue(swapped)
        self.assertEqual(list(self.vault.rglob("*")), [])

    def test_repeated_nested_read_write_does_not_leak_file_descriptors(self):
        before = len(os.listdir("/dev/fd"))
        with lib.archive_session(self.archive, create=True):
            for index in range(200):
                target = self.archive / "state" / ("%03d.json" % (index % 3))
                lib.save_json(target, {"index": index})
                self.assertEqual(lib.load_json(target)["index"], index)
        after = len(os.listdir("/dev/fd"))

        self.assertLessEqual(after, before + 1)
        self.assertIsNone(lib.current_archive_session())

    def test_context_is_thread_local_nested_and_always_reset(self):
        seen = []
        with lib.archive_session(self.archive, create=True) as outer:
            with lib.archive_session(self.archive, create=True) as nested:
                self.assertIs(outer, nested)
                thread = threading.Thread(
                    target=lambda: seen.append(lib.current_archive_session())
                )
                thread.start()
                thread.join()
            self.assertIs(lib.current_archive_session(), outer)
        self.assertEqual(seen, [None])
        self.assertIsNone(lib.current_archive_session())

    def test_local_pdf_snapshot_swap_never_writes_vault(self):
        pdf = self.base / "《中国建设报》2026-09-03_第9170期.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
        (self.vault / "archive").mkdir()
        baseline = sorted(path.relative_to(self.vault) for path in self.vault.rglob("*"))
        real_mkstemp = local_pdf.tempfile.mkstemp
        swapped = False

        def mkstemp_after_swap(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                moved = self.base / "safe-local-pdf"
                os.rename(self.safe_parent, moved)
                self.safe_parent.symlink_to(self.vault, target_is_directory=True)
                swapped = True
            return real_mkstemp(*args, **kwargs)

        with mock.patch.object(
            local_pdf.tempfile, "mkstemp", side_effect=mkstemp_after_swap
        ), mock.patch.object(local_pdf, "run_pdfocr") as renderer:
            with self.assertRaises(lib.ArchiveConflictError):
                local_pdf.import_pdf(
                    pdf, self.archive, date="2026-09-03",
                    vault_root=self.vault,
                )

        self.assertTrue(swapped)
        renderer.assert_not_called()
        self.assertEqual(
            sorted(path.relative_to(self.vault) for path in self.vault.rglob("*")),
            baseline,
        )

    def test_helper_compile_swap_uses_system_staging_and_never_vault(self):
        source = self.base / "pdfocr.swift"
        source.write_text("print(1)", encoding="utf-8")
        missing_prebuilt = self.base / "missing-pdfocr"
        output_paths = []

        def fake_swiftc(command, **_kwargs):
            output = Path(command[command.index("-o") + 1])
            output_paths.append(output)
            self._swap_archive_root()
            output.write_bytes(b"compiled-helper")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaises(lib.ArchiveConflictError):
            with lib.archive_session(self.archive, create=True):
                with mock.patch.object(local_pdf, "PDFOCR_SOURCE", source), \
                        mock.patch.object(local_pdf, "PREBUILT_PDFOCR", missing_prebuilt), \
                        mock.patch.object(local_pdf.shutil, "which", return_value="/usr/bin/swiftc"), \
                        mock.patch.object(local_pdf.subprocess, "run", side_effect=fake_swiftc):
                    local_pdf._helper_binary(self.archive)

        self.assertEqual(len(output_paths), 1)
        self.assertFalse(str(output_paths[0]).startswith(str(self.archive)))
        self.assertEqual(list(self.vault.rglob("*")), [])

    def test_wechat_out_validation_to_open_swap_fails_without_vault_write(self):
        out_parent = self.base / "wechat-safe"
        out_parent.mkdir()
        out = out_parent / "artifacts"
        out.mkdir()
        (self.vault / "artifacts").mkdir()
        baseline = sorted(
            path.relative_to(self.vault) for path in self.vault.rglob("*")
        )
        real_check = wechat_read.lib.assert_configured_roots_separate
        swapped = False

        def validate_then_swap(*args, **kwargs):
            nonlocal swapped
            result = real_check(*args, **kwargs)
            moved = self.base / "wechat-out-before-swap"
            os.rename(out_parent, moved)
            out_parent.symlink_to(self.vault, target_is_directory=True)
            swapped = True
            return result

        source = {
            "id": "zgjsb",
            "name": "中国建设报",
            "channel": "wechat_read",
            "out": str(out),
            "_vault_root": str(self.vault),
        }
        with mock.patch.object(
            wechat_read.lib,
            "assert_configured_roots_separate",
            side_effect=validate_then_swap,
        ), mock.patch.object(wechat_read, "_load_engine") as load_engine:
            with self.assertRaises(lib.ArchivePathSafetyError):
                wechat_read.acquire(
                    source,
                    datetime.date(2026, 9, 3),
                    self.archive,
                    offline_ok=True,
                )

        self.assertTrue(swapped)
        load_engine.assert_not_called()
        self.assertEqual(
            sorted(path.relative_to(self.vault) for path in self.vault.rglob("*")),
            baseline,
        )


if __name__ == "__main__":
    unittest.main()
