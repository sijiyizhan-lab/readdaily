import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(FETCH_SCRIPTS))


def load_fetch():
    spec = importlib.util.spec_from_file_location(
        "readdaily_fetch_path_safety", FETCH_SCRIPTS / "fetch.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetch = load_fetch()


class FetchPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        (self.vault / ".obsidian").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetch_rejects_archive_equal_to_vault_or_its_descendant(self):
        with self.assertRaises(ValueError):
            fetch.validate_archive_outside_vault(self.vault, self.vault)
        with self.assertRaises(ValueError):
            fetch.validate_archive_outside_vault(
                self.vault / "private-archive", self.vault
            )
        with self.assertRaises(ValueError):
            fetch.validate_archive_outside_vault(self.base, self.vault)

    def test_fetch_rejects_archive_symlink_alias_of_vault(self):
        alias = self.base / "vault-alias"
        alias.symlink_to(self.vault, target_is_directory=True)

        with self.assertRaises(ValueError):
            fetch.validate_archive_outside_vault(alias, self.vault)

    def test_fetch_rejects_archive_child_symlinked_into_vault(self):
        archive = self.base / "archive"
        archive.mkdir()
        (archive / "_state").symlink_to(self.vault, target_is_directory=True)

        with self.assertRaises(ValueError):
            fetch.validate_archive_outside_vault(archive, self.vault)

    def test_archive_paths_rejects_traversal_absolute_and_non_ascii_source_ids(self):
        archive = self.base / "archive"
        for source_id in ("../vault", "/tmp/vault", "报纸", "paper/child", "."):
            with self.subTest(source_id=source_id):
                with self.assertRaises(ValueError):
                    fetch.lib.archive_paths(archive, source_id, "2026-09-02")

        safe = fetch.lib.archive_paths(archive, "paper_1-test", "2026-09-02")
        self.assertEqual(
            Path(safe["dir"]), archive / "paper_1-test" / "2026-09-02"
        )

    def test_invalid_registry_identity_stops_fetch_parse_and_probe_before_writes(self):
        archive = self.base / "external-archive"
        archive.mkdir()
        sentinel = archive / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        baseline_archive = {
            path.relative_to(archive): path.read_bytes()
            for path in archive.rglob("*") if path.is_file()
        }
        baseline_vault = sorted(
            path.relative_to(self.vault) for path in self.vault.rglob("*")
        )
        modes = (
            ["fetch.py", "--date", "2026-09-02", "--stage", "fetched"],
            ["fetch.py", "--date", "2026-09-02", "--stage", "parsed"],
            ["fetch.py", "--date", "2026-09-02", "--probe", "../vault"],
        )
        unsafe_ids = ("../vault", str(self.vault))

        for unsafe_id in unsafe_ids:
            for argv in modes:
                registry = {
                    "archive_root": str(archive),
                    "sources": [{
                        "id": unsafe_id,
                        "name": "恶意来源",
                        "channel": "founder",
                        "enabled": True,
                    }],
                }
                with self.subTest(source_id=unsafe_id, argv=argv), \
                        mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(
                            fetch, "load_registry", return_value=registry
                        ), \
                        mock.patch.object(fetch, "load_adapter") as loader, \
                        mock.patch.object(fetch, "fetch_date_lock") as lock, \
                        mock.patch.object(fetch.lib, "log_line") as logger, \
                        mock.patch.dict(os.environ, {
                            "READDAILY_ARCHIVE": str(archive),
                            "READDAILY_VAULT": str(self.vault),
                        }):
                    with self.assertRaises(SystemExit) as caught:
                        fetch.main()

                self.assertIn("id", str(caught.exception))
                loader.assert_not_called()
                lock.assert_not_called()
                logger.assert_not_called()
                self.assertEqual(
                    {
                        path.relative_to(archive): path.read_bytes()
                        for path in archive.rglob("*") if path.is_file()
                    },
                    baseline_archive,
                )
                self.assertEqual(
                    sorted(
                        path.relative_to(self.vault)
                        for path in self.vault.rglob("*")
                    ),
                    baseline_vault,
                )

    def test_fetch_main_checks_boundary_before_loading_an_adapter(self):
        registry = {
            "archive_root": str(self.vault),
            "sources": [{
                "id": "zgjsb",
                "name": "中国建设报",
                "channel": "wechat_read",
                "enabled": True,
            }],
        }
        argv = ["fetch.py", "--date", "2026-09-02", "--registry", "unused.json"]

        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fetch, "load_registry", return_value=registry), \
                mock.patch.object(fetch, "load_adapter") as loader, \
                mock.patch.dict(os.environ, {"READDAILY_VAULT": str(self.vault)}):
            with self.assertRaises(SystemExit) as caught:
                fetch.main()

        self.assertIn("Vault", str(caught.exception))
        loader.assert_not_called()

    def test_source_write_roots_reject_vault_overlap_and_symlink_routes(self):
        archive = self.base / "archive"
        archive.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        alias = self.base / "out-alias"
        alias.symlink_to(self.vault, target_is_directory=True)
        nested = self.base / "nested-out"
        nested.mkdir()
        (nested / "中国建设报").symlink_to(
            self.vault, target_is_directory=True
        )

        unsafe_roots = (
            self.vault,
            self.vault / "wechat-output",
            self.base,
            alias,
            nested,
        )
        for output_root in unsafe_roots:
            registry = {
                "archive_root": str(archive),
                "sources": [{
                    "id": "zgjsb",
                    "name": "中国建设报",
                    "channel": "wechat_read",
                    "enabled": True,
                    "out": str(output_root),
                }],
            }
            with self.subTest(output_root=str(output_root)):
                with self.assertRaises(ValueError):
                    fetch.validate_registry_write_roots_outside_vault(
                        registry, self.vault
                    )

        safe_registry = {
            "sources": [{
                "id": "zgjsb",
                "name": "中国建设报",
                "channel": "wechat_read",
                "enabled": True,
                "out": str(outside),
            }],
        }
        validated = fetch.validate_registry_write_roots_outside_vault(
            safe_registry, self.vault
        )
        self.assertEqual(validated[0]["path"], str(outside.resolve()))

    def test_wechat_runtime_default_root_is_checked_even_with_custom_out(self):
        outside = self.base / "outside"
        outside.mkdir()
        registry = {
            "sources": [{
                "id": "zgjsb",
                "name": "中国建设报",
                "channel": "wechat_read",
                "enabled": True,
                "out": str(outside),
            }],
        }

        with mock.patch.object(fetch, "DEFAULT_WECHAT_OUT", str(self.vault)):
            with self.assertRaises(ValueError) as caught:
                fetch.validate_registry_write_roots_outside_vault(
                    registry, self.vault
                )

        self.assertIn("wechat_engine_default", str(caught.exception))

    def test_fetch_main_rejects_any_registry_write_root_before_adapter_and_writes(self):
        archive = self.base / "archive"
        archive.mkdir()
        registry = {
            "archive_root": str(archive),
            "sources": [
                {
                    "id": "rmrb",
                    "name": "人民日报",
                    "channel": "founder",
                    "enabled": True,
                },
                {
                    "id": "zgjsb",
                    "name": "中国建设报",
                    "channel": "wechat_read",
                    "enabled": False,
                    "out": str(self.vault),
                },
            ],
        }
        argv = [
            "fetch.py", "--date", "2026-09-02", "--source", "rmrb",
            "--registry", "unused.json",
        ]

        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fetch, "load_registry", return_value=registry), \
                mock.patch.object(fetch, "load_adapter") as loader, \
                mock.patch.dict(os.environ, {
                    "READDAILY_ARCHIVE": str(archive),
                    "READDAILY_VAULT": str(self.vault),
                }):
            with self.assertRaises(SystemExit) as caught:
                fetch.main()

        self.assertIn("zgjsb.out", str(caught.exception))
        self.assertIn("Vault", str(caught.exception))
        loader.assert_not_called()
        self.assertEqual(list(archive.iterdir()), [])
        self.assertEqual(
            sorted(path.name for path in self.vault.iterdir()), [".obsidian"]
        )

    def test_fetch_lock_is_scoped_by_archive_and_date_and_releases_on_error(self):
        archive = self.base / "archive"
        archive.mkdir()

        with fetch.fetch_date_lock(archive, "2026-09-02"):
            with self.assertRaises(fetch.FetchLockedError):
                with fetch.fetch_date_lock(archive, "2026-09-02"):
                    pass
            with fetch.fetch_date_lock(archive, "2026-09-03"):
                pass

        with fetch.fetch_date_lock(archive, "2026-09-02"):
            pass

        with self.assertRaises(RuntimeError):
            with fetch.fetch_date_lock(archive, "2026-09-04"):
                raise RuntimeError("simulate crash unwind")
        with fetch.fetch_date_lock(archive, "2026-09-04"):
            pass

    def test_fetch_batch_continues_after_one_source_raises(self):
        archive = self.base / "archive"
        vault = self.base / "separate-vault"
        archive.mkdir()
        vault.mkdir()
        bad_fetch = mock.Mock(side_effect=TimeoutError("upstream timeout"))

        def save_good_issue(source, day, root):
            issue = {
                "source": source["id"],
                "date": day.isoformat(),
                "issue_no": "1",
                "editions": [],
                "units": [],
            }
            paths = fetch.lib.archive_paths(root, source["id"], day)
            fetch.lib.save_json(paths["issue_json"], issue)
            return issue, None

        good_fetch = mock.Mock(side_effect=save_good_issue)
        adapters = {
            "founder": SimpleNamespace(fetch=bad_fetch),
            "paper_api": SimpleNamespace(fetch=good_fetch),
        }
        registry = {
            "archive_root": str(archive),
            "sources": [
                {"id": "bad", "name": "坏源", "channel": "founder", "enabled": True},
                {"id": "good", "name": "好源", "channel": "paper_api", "enabled": True},
            ],
        }
        argv = ["fetch.py", "--date", "2026-09-02", "--stage", "fetched"]

        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fetch, "load_registry", return_value=registry), \
                mock.patch.object(fetch, "load_adapter", side_effect=lambda name: adapters[name]), \
                mock.patch.dict(os.environ, {
                    "READDAILY_ARCHIVE": str(archive),
                    "READDAILY_VAULT": str(vault),
                }):
            fetch.main()

        bad_fetch.assert_called_once()
        good_fetch.assert_called_once()
        failed_state = json.loads(
            (archive / "_state" / "bad" / "2026-09-02.json").read_text(encoding="utf-8")
        )
        self.assertIn("failed", failed_state["stages"])
        good_state = json.loads(
            (archive / "_state" / "good" / "2026-09-02.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            good_state["state_schema_version"], fetch.STATE_SCHEMA_VERSION
        )


if __name__ == "__main__":
    unittest.main()
