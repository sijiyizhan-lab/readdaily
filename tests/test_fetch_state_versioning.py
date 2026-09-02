import datetime
import importlib.util
import json
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
        "readdaily_fetch_state_versioning", FETCH_SCRIPTS / "fetch.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetch = load_fetch()


class FetchStateVersioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Path(self.temp.name) / "archive"
        self.day = datetime.date(2026, 9, 3)
        self.source = {
            "id": "paper",
            "name": "示例报纸",
            "channel": "founder",
        }
        self.args = SimpleNamespace(
            stage="fetched,parsed", no_state_skip=False, offline=True
        )
        self.paths = fetch.lib.archive_paths(
            str(self.archive), self.source["id"], self.day
        )
        self.daily_log = str(self.archive / "_dailylog.jsonl")

    def tearDown(self):
        self.temp.cleanup()

    def write_issue(self, marker, parsed=False):
        issue = {
            "source": self.source["id"],
            "source_name": self.source["name"],
            "date": self.day.isoformat(),
            "issue_no": "1",
            "editions": [{"no": 1, "name": "要闻"}],
            "units": [{"id": "paper_20260903_01", "text": marker}],
            "marker": marker,
        }
        if parsed:
            issue["parsed"] = True
        page = Path(self.paths["dir"]) / "pages" / "01.jpg"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(("page:" + marker).encode("utf-8"))
        fetch.lib.save_json(self.paths["issue_json"], issue)
        return issue

    def adapter(self):
        def fetch_issue(_source, _day, _root):
            return self.write_issue("fresh-fetch"), None

        def parse_issue(_source, _day, _root):
            issue = fetch.lib.load_json(self.paths["issue_json"])
            issue["parsed"] = True
            fetch.lib.save_json(self.paths["issue_json"], issue)
            return issue, None

        return SimpleNamespace(
            fetch=mock.Mock(side_effect=fetch_issue),
            parse=mock.Mock(side_effect=parse_issue),
        )

    def current_state(self, *, adapter_contract=None, parser_contract=None):
        digest = fetch._issue_evidence_digest(
            self.paths["dir"], self.source["id"], self.day
        )
        return {
            "state_schema_version": fetch.STATE_SCHEMA_VERSION,
            "pipeline_contract_version": fetch.PIPELINE_CONTRACT_VERSION,
            "adapter_contract_version": (
                adapter_contract
                if adapter_contract is not None
                else fetch._stage_contract_version(self.source, "fetched")
            ),
            "parser_contract_version": (
                parser_contract
                if parser_contract is not None
                else fetch._stage_contract_version(self.source, "parsed")
            ),
            "issue_evidence_sha256": digest,
            "stages": {
                "fetched": "2026-09-03T08:00:00",
                "parsed": "2026-09-03T08:01:00",
            },
        }

    def run_source(self, adapter):
        with mock.patch.object(fetch, "load_adapter", return_value=adapter):
            return fetch._run_source(
                self.source,
                self.day,
                str(self.archive),
                self.args,
                self.daily_log,
            )

    def issue_tree_bytes(self):
        issue_dir = Path(self.paths["dir"])
        if not issue_dir.exists():
            return None
        return {
            path.relative_to(issue_dir).as_posix(): path.read_bytes()
            for path in issue_dir.rglob("*")
            if path.is_file()
        }

    def test_legacy_timestamp_state_forces_fetch_and_parse_then_is_versioned(self):
        self.write_issue("legacy", parsed=True)
        fetch.lib.save_json(self.paths["state"], {
            "stages": {
                "fetched": "2026-09-03T07:00:00",
                "parsed": "2026-09-03T07:01:00",
            }
        })
        adapter = self.adapter()

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "ok")
        adapter.fetch.assert_called_once()
        adapter.parse.assert_called_once()
        state = fetch.lib.load_json(self.paths["state"])
        self.assertEqual(state["state_schema_version"], fetch.STATE_SCHEMA_VERSION)
        self.assertEqual(
            state["pipeline_contract_version"], fetch.PIPELINE_CONTRACT_VERSION
        )
        self.assertEqual(
            state["adapter_contract_version"],
            fetch._stage_contract_version(self.source, "fetched"),
        )
        self.assertEqual(
            state["parser_contract_version"],
            fetch._stage_contract_version(self.source, "parsed"),
        )
        self.assertEqual(
            state["issue_evidence_sha256"],
            fetch._issue_evidence_digest(
                self.paths["dir"], self.source["id"], self.day
            ),
        )

    def test_matching_versions_and_issue_evidence_skip_fetch_and_parse(self):
        self.write_issue("trusted", parsed=True)
        fetch.lib.save_json(self.paths["state"], self.current_state())
        adapter = self.adapter()

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "skipped")
        adapter.fetch.assert_not_called()
        adapter.parse.assert_not_called()

    def test_issue_tampering_invalidates_state_and_reruns_both_stages(self):
        self.write_issue("trusted", parsed=True)
        fetch.lib.save_json(self.paths["state"], self.current_state())
        tampered = fetch.lib.load_json(self.paths["issue_json"])
        tampered["units"][0]["text"] = "tampered after state was saved"
        fetch.lib.save_json(self.paths["issue_json"], tampered)
        adapter = self.adapter()

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "ok")
        adapter.fetch.assert_called_once()
        adapter.parse.assert_called_once()
        state = fetch.lib.load_json(self.paths["state"])
        self.assertEqual(
            state["issue_evidence_sha256"],
            fetch._issue_evidence_digest(
                self.paths["dir"], self.source["id"], self.day
            ),
        )

    def test_parser_contract_mismatch_reuses_fetch_but_forces_parse(self):
        self.write_issue("trusted", parsed=True)
        fetch.lib.save_json(
            self.paths["state"], self.current_state(parser_contract="founder:old")
        )
        adapter = self.adapter()

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "ok")
        adapter.fetch.assert_not_called()
        adapter.parse.assert_called_once()
        state = fetch.lib.load_json(self.paths["state"])
        self.assertEqual(
            state["parser_contract_version"],
            fetch._stage_contract_version(self.source, "parsed"),
        )

    def test_adapter_contract_mismatch_forces_fetch_and_downstream_parse(self):
        self.write_issue("trusted", parsed=True)
        fetch.lib.save_json(
            self.paths["state"],
            self.current_state(adapter_contract="founder:fetch:old"),
        )
        adapter = self.adapter()

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "ok")
        adapter.fetch.assert_called_once()
        adapter.parse.assert_called_once()

    def test_state_or_pipeline_version_mismatch_forces_fetch(self):
        self.args.stage = "fetched"
        for field, mismatch in (
            ("state_schema_version", fetch.STATE_SCHEMA_VERSION + 1),
            (
                "pipeline_contract_version",
                fetch.PIPELINE_CONTRACT_VERSION + 1,
            ),
        ):
            with self.subTest(field=field):
                self.write_issue("trusted", parsed=True)
                state = self.current_state()
                state[field] = mismatch
                fetch.lib.save_json(self.paths["state"], state)
                adapter = self.adapter()

                outcome = self.run_source(adapter)

                self.assertEqual(outcome, "ok")
                adapter.fetch.assert_called_once()
                adapter.parse.assert_not_called()

    def test_page_evidence_tampering_invalidates_state(self):
        self.write_issue("trusted", parsed=True)
        fetch.lib.save_json(self.paths["state"], self.current_state())
        page = Path(self.paths["dir"]) / "pages" / "01.jpg"
        page.write_bytes(b"tampered-page-bytes")
        adapter = self.adapter()

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "ok")
        adapter.fetch.assert_called_once()
        adapter.parse.assert_called_once()

    def test_downstream_summary_annotation_does_not_invalidate_raw_evidence(self):
        self.write_issue("trusted", parsed=True)
        fetch.lib.save_json(self.paths["state"], self.current_state())
        issue = fetch.lib.load_json(self.paths["issue_json"])
        issue["units"][0]["summary"] = {
            "summary": "这是解析完成后的编辑摘要，不是原始证据。"
        }
        fetch.lib.save_json(self.paths["issue_json"], issue)
        adapter = self.adapter()

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "skipped")
        adapter.fetch.assert_not_called()
        adapter.parse.assert_not_called()

    def test_missing_issue_never_skips_even_when_state_claims_completion(self):
        self.write_issue("temporary", parsed=True)
        state = self.current_state()
        Path(self.paths["issue_json"]).unlink()
        fetch.lib.save_json(self.paths["state"], state)
        adapter = self.adapter()

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "ok")
        adapter.fetch.assert_called_once()
        adapter.parse.assert_called_once()

    def test_parse_failure_restores_prior_complete_issue_and_state(self):
        self.write_issue("trusted-old", parsed=True)
        old_state = self.current_state(adapter_contract="founder:fetch:old")
        fetch.lib.save_json(self.paths["state"], old_state)
        before_tree = self.issue_tree_bytes()

        def shallow_fetch(_source, _day, _root):
            return self.write_issue("shallow-new", parsed=False), None

        adapter = SimpleNamespace(
            fetch=mock.Mock(side_effect=shallow_fetch),
            parse=mock.Mock(return_value=(None, "detail endpoint unavailable")),
        )

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "failed")
        self.assertEqual(self.issue_tree_bytes(), before_tree)
        restored_state = fetch.lib.load_json(self.paths["state"])
        self.assertEqual(
            restored_state["issue_evidence_sha256"],
            old_state["issue_evidence_sha256"],
        )
        self.assertEqual(
            restored_state["stages"]["parsed"],
            old_state["stages"]["parsed"],
        )
        self.assertIn("failed", restored_state["stages"])
        self.assertFalse(list(Path(self.paths["dir"]).parent.glob(".*.pipeline.*")))

    def test_parse_failure_removes_new_issue_when_no_prior_archive_exists(self):
        def shallow_fetch(_source, _day, _root):
            return self.write_issue("shallow-new", parsed=False), None

        adapter = SimpleNamespace(
            fetch=mock.Mock(side_effect=shallow_fetch),
            parse=mock.Mock(return_value=(None, "detail endpoint unavailable")),
        )

        outcome = self.run_source(adapter)

        self.assertEqual(outcome, "failed")
        self.assertFalse(Path(self.paths["dir"]).exists())
        state = fetch.lib.load_json(self.paths["state"])
        self.assertNotIn("fetched", state.get("stages", {}))
        self.assertNotIn("parsed", state.get("stages", {}))
        self.assertIn("failed", state.get("stages", {}))

    def test_transaction_flushes_old_issue_snapshot_before_refresh(self):
        self.write_issue("trusted-old", parsed=True)
        fetch.lib.save_json(self.paths["state"], self.current_state())
        flushed_trees = []
        synced_directories = []
        real_fsync_tree = fetch.lib.fsync_tree
        real_fsync_directory = fetch.lib.fsync_directory

        def observe_tree(path):
            flushed_trees.append(Path(path))
            return real_fsync_tree(path)

        def observe_directory(path):
            synced_directories.append(Path(path))
            return real_fsync_directory(path)

        with mock.patch.object(
            fetch.lib, "fsync_tree", side_effect=observe_tree
        ), mock.patch.object(
            fetch.lib, "fsync_directory", side_effect=observe_directory
        ):
            transaction = fetch._IssueRefreshTransaction(
                self.paths["dir"], self.paths["state"]
            )

        try:
            self.assertEqual(flushed_trees, [Path(transaction.snapshot_issue)])
            self.assertIn(Path(self.paths["dir"]).parent, synced_directories)
        finally:
            transaction.commit()

    def test_state_parent_fsync_failure_makes_rollback_explicit(self):
        self.write_issue("trusted-old", parsed=True)
        old_state = self.current_state(adapter_contract="founder:fetch:old")
        fetch.lib.save_json(self.paths["state"], old_state)
        before_tree = self.issue_tree_bytes()
        transaction = fetch._IssueRefreshTransaction(
            self.paths["dir"], self.paths["state"]
        )

        self.write_issue("fresh-new", parsed=False)
        fetch.lib.save_json(self.paths["state"], {"stages": {"fetched": "new"}})
        state_parent = Path(self.paths["state"]).parent
        real_fsync_directory = fetch.lib.fsync_directory
        failed = False

        def fail_state_parent(path):
            nonlocal failed
            if not failed and Path(path).resolve() == state_parent.resolve():
                failed = True
                raise OSError("state parent fsync failed")
            return real_fsync_directory(path)

        with mock.patch.object(
            fetch.lib, "fsync_directory", side_effect=fail_state_parent
        ):
            with self.assertRaisesRegex(RuntimeError, "耐久回滚未完成"):
                transaction.rollback()

        self.assertTrue(failed)
        self.assertEqual(self.issue_tree_bytes(), before_tree)
        self.assertEqual(fetch.lib.load_json(self.paths["state"]), old_state)
        self.assertFalse(transaction.finished)
        with self.assertRaisesRegex(RuntimeError, "拒绝重复宣称完成"):
            transaction.rollback()

    def test_snapshot_cleanup_fsync_failure_prevents_commit_success(self):
        self.write_issue("trusted-old", parsed=True)
        transaction = fetch._IssueRefreshTransaction(
            self.paths["dir"], self.paths["state"]
        )
        snapshot = Path(transaction.snapshot_root)
        real_fsync_directory = fetch.lib.fsync_directory
        failed = False

        def fail_after_snapshot_delete(path):
            nonlocal failed
            if (not failed
                    and Path(path) == snapshot.parent
                    and not snapshot.exists()):
                failed = True
                raise OSError("snapshot cleanup parent fsync failed")
            return real_fsync_directory(path)

        with mock.patch.object(
            fetch.lib, "fsync_directory", side_effect=fail_after_snapshot_delete
        ):
            with self.assertRaisesRegex(RuntimeError, "事务快照"):
                transaction.commit()

        self.assertTrue(failed)
        self.assertFalse(transaction.finished)
        self.assertTrue(transaction.commit_attempted)
        with self.assertRaisesRegex(RuntimeError, "提交收尾"):
            transaction.rollback()


if __name__ == "__main__":
    unittest.main()
