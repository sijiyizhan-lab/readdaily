import argparse
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
READER_SCRIPTS = ROOT / "skills" / "newspaper-reader" / "scripts"
sys.path.insert(0, str(READER_SCRIPTS))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


api = load_module("workbench_api", READER_SCRIPTS / "workbench_api.py")
cli = load_module("readdaily_cli", ROOT / "scripts" / "readdaily.py")


def _synchronized_atomic_writer(barrier):
    original = api._atomic_json

    def write(path, obj):
        try:
            barrier.wait(timeout=0.6)
        except threading.BrokenBarrierError:
            pass
        return original(path, obj)

    api._atomic_json = write


def _concurrent_draft_worker(archive, vault, draft, start, barrier):
    _synchronized_atomic_writer(barrier)
    start.wait(timeout=3)
    api.save_draft(archive, vault, draft)


def _delayed_draft_worker(archive, vault, draft, at_write, release_write):
    original = api._atomic_json

    def delayed_write(path, obj):
        at_write.set()
        if not release_write.wait(timeout=5):
            raise RuntimeError("草稿竞态测试未收到继续信号")
        return original(path, obj)

    api._atomic_json = delayed_write
    api.save_draft(archive, vault, draft)


def _publisher_evidence_lock_worker(archive, day, ready, entered):
    ready.set()
    with api.vault_publisher._fetch_date_evidence_lock(archive, day):
        entered.set()


def _concurrent_activity_worker(archive, vault, source, day, start, barrier):
    _synchronized_atomic_writer(barrier)
    start.wait(timeout=3)
    api.mark_reading_activity(archive, vault, source, day, "opened")


class WorkbenchAPITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.archive = self.base / "archive"
        self.vault = self.base / "vault"
        self.publisher_state_patch = mock.patch.object(
            api.vault_publisher,
            "DEFAULT_PUBLISHER_STATE_ROOT",
            self.base / "publisher-state",
        )
        self.publisher_state_patch.start()
        self.archive.mkdir()
        self.vault.mkdir()
        (self.vault / ".obsidian").mkdir()

    def tearDown(self):
        self.publisher_state_patch.stop()
        self.tmp.cleanup()

    def bind_draft(self, draft):
        bound = json.loads(json.dumps(draft, ensure_ascii=False))
        issue = api.get_issue(
            self.archive, bound["source"], bound["date"]
        )
        bound["evidence_sha256"] = issue["evidence_sha256"]
        return bound

    def write_issue(self, source="zgjsb", day="2026-08-31", issue_no="9167",
                    source_name=None, archive=None):
        issue_dir = (archive or self.archive) / source / day
        (issue_dir / "pages").mkdir(parents=True)
        (issue_dir / "text").mkdir()
        (issue_dir / "pages" / "01.jpg").write_bytes(b"jpeg-one")
        (issue_dir / "pages" / "02.jpg").write_bytes(b"jpeg-two")
        (issue_dir / "text" / "01.txt").write_text("第一版安全正文", encoding="utf-8")
        issue = {
            "source": source,
            "source_name": source_name or "中国建设报",
            "date": day,
            "issue_no": issue_no,
            "channel": "mixed-test",
            "editions": [
                {"no": 1, "name": "要闻", "page_image": "pages/01.jpg"},
                {"no": 2, "name": "城市更新", "page_image": "pages/02.jpg"},
            ],
            # 故意反序，证明优先按 page_image 匹配，而不是盲用索引。
            "units": [
                {
                    "id": "u2",
                    "title": "2版 城市更新",
                    "page_image": "pages/02.jpg",
                    "text": "第二版正文",
                    "articles": [
                        {"title": "重复标题", "text": "第二版正文"},
                        {"title": "补充标题", "text": "补充事实"},
                    ],
                },
                {
                    "id": "u1",
                    "title": "1版 要闻",
                    "page_image": "pages/01.jpg",
                    "text_path": "text/01.txt",
                    "text": "不应盖过文件正文",
                },
            ],
        }
        (issue_dir / "issue.json").write_text(
            json.dumps(issue, ensure_ascii=False), encoding="utf-8"
        )
        return issue_dir

    def valid_draft(self, source="zgjsb"):
        fact = {
            "subject": "某市",
            "action": "实施",
            "object": "城市更新",
            "value": "12",
            "unit": "个项目",
            "time": "2026年",
            "source": "中国建设报原版",
        }
        return self.bind_draft({
            "source": source,
            "date": "2026-08-31",
            "units": [
                {
                    "id": "u1",
                    "title": "  人工标题：住房制度观察  ",
                    "summary": "第一版围绕建设投资和住房制度展开，事实仍需人工逐项复核。",
                    "importance": 4,
                    "topics": ["建设投资与房地产"],
                    "facts": [fact],
                },
                {
                    "id": "u2",
                    "summary": "第二版围绕城市更新和社区治理展开，事实仍需人工逐项复核。",
                    "importance": 5,
                    "topics": ["城市更新与城市治理"],
                    "facts": [fact],
                },
            ],
        })

    def test_issue_normalizes_adapters_matches_pages_and_deduplicates_text(self):
        self.write_issue()

        result = api.get_issue(self.archive, "zgjsb", "2026-08-31")

        self.assertEqual([u["id"] for u in result["units"]], ["u1", "u2"])
        self.assertEqual(result["units"][0]["text"], "第一版安全正文")
        self.assertEqual(result["units"][0]["text_source"], "text_path")
        self.assertEqual(result["units"][1]["text"].count("第二版正文"), 1)
        self.assertIn("补充事实", result["units"][1]["text"])
        self.assertEqual(result["coverage"]["editions"], 2)
        self.assertEqual(result["coverage"]["with_text"], 2)

    def test_issue_rejects_payload_source_that_disagrees_with_requested_directory(self):
        issue_dir = self.write_issue(
            source="rmrb", source_name="人民日报", issue_no="30100"
        )
        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        raw["source"] = "zgjsb"
        raw["units"][0]["text"] = "不得展示错源旧正文"
        (issue_dir / "issue.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )
        # A completed fetch state can cause the fetcher to skip its adapter.
        # The read boundary must still reject the stale/misfiled payload.
        state_dir = self.archive / "_state" / "rmrb"
        state_dir.mkdir(parents=True)
        (state_dir / "2026-08-31.json").write_text(json.dumps({
            "stages": {
                "fetched": "2026-08-31T09:00:00+08:00",
                "parsed": "2026-08-31T09:10:00+08:00",
            }
        }), encoding="utf-8")

        with self.assertRaises(api.ValidationError) as caught:
            api.get_issue(self.archive, "rmrb", "2026-08-31")

        self.assertIn("source", str(caught.exception))
        self.assertIn("zgjsb", str(caught.exception))
        dashboard = api.get_daily_dashboard(self.archive, "2026-08-31")
        row = next(item for item in dashboard["newspapers"] if item["source"] == "rmrb")
        self.assertFalse(row["available"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["text_length"], 0)
        self.assertIsNone(row["thumbnail"])
        self.assertNotIn(
            "不得展示错源旧正文",
            json.dumps(row, ensure_ascii=False),
        )

    def test_issue_rejects_payload_date_that_disagrees_with_requested_directory(self):
        issue_dir = self.write_issue(
            source="gmrb", source_name="光明日报", day="2026-08-31", issue_no="1"
        )
        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        raw["date"] = "2026-08-30"
        raw["units"][0]["text"] = "不得展示错日旧正文"
        (issue_dir / "issue.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaises(api.ValidationError) as caught:
            api.get_issue(self.archive, "gmrb", "2026-08-31")

        self.assertIn("date", str(caught.exception))
        self.assertIn("2026-08-30", str(caught.exception))
        dashboard = api.get_daily_dashboard(self.archive, "2026-08-31")
        row = next(item for item in dashboard["newspapers"] if item["source"] == "gmrb")
        self.assertFalse(row["available"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["text_length"], 0)
        self.assertIsNone(row["thumbnail"])
        self.assertNotIn(
            "不得展示错日旧正文",
            json.dumps(row, ensure_ascii=False),
        )

    def test_issue_recovers_conventional_ocr_text_when_text_path_is_missing(self):
        issue_dir = self.write_issue()
        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        first_page_unit = next(unit for unit in raw["units"] if unit["id"] == "u1")
        first_page_unit.pop("text_path")
        first_page_unit.pop("text", None)
        (issue_dir / "text" / "01.txt").rename(issue_dir / "text" / "edition_01.txt")
        (issue_dir / "issue.json").write_text(json.dumps(raw), encoding="utf-8")

        result = api.get_issue(self.archive, "zgjsb", "2026-08-31")

        self.assertEqual(result["units"][0]["text"], "第一版安全正文")
        self.assertEqual(result["units"][0]["text_source"], "conventional_text_path")

    def test_unverified_local_pdf_date_blocks_publish_plan_even_with_complete_draft(self):
        issue_dir = self.write_issue()
        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        linked_pdf = self.archive / "_imports" / "fixture" / "paper.pdf"
        linked_pdf.parent.mkdir(parents=True)
        linked_pdf.write_bytes(b"%PDF-1.4\nfixture\n")
        raw["channel"] = "local_pdf"
        raw["files"] = {"local_pdf": str(linked_pdf)}
        raw["local_pdf_header_date"] = None
        raw["local_pdf_date_verification"] = "unverified"
        (issue_dir / "issue.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )
        api.save_draft(self.archive, self.vault, self.valid_draft())

        issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")

        self.assertEqual(issue["local_pdf_date_verification"], "unverified")
        self.assertEqual(issue["status"], "needs_review")
        self.assertTrue(any("报头日期" in item for item in issue["warnings"]))
        args = api._parser().parse_args([
            "publish-plan",
            "--archive", str(self.archive),
            "--vault", str(self.vault),
            "--source", "zgjsb",
            "--date", "2026-08-31",
        ])
        with mock.patch.object(api.vault_publisher, "create_plan") as create_plan:
            with self.assertRaisesRegex(api.ValidationError, "报头日期"):
                api._dispatch(args)
        create_plan.assert_not_called()

    def test_issue_backfills_existing_summaries_and_draft_has_priority(self):
        issue_dir = self.write_issue()
        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        raw["units"][1]["summary"] = {"summary": "unit 内已有摘要", "importance": 4}
        (issue_dir / "issue.json").write_text(json.dumps(raw), encoding="utf-8")
        sidecar = self.archive / "_summaries" / "zgjsb"
        sidecar.mkdir(parents=True)
        (sidecar / "2026-08-31.json").write_text(json.dumps({
            "units": [
                {"id": "u1", "summary": "sidecar 低优先级摘要", "importance": 2},
                {"id": "u2", "summary": "sidecar 已有摘要", "importance": 5},
            ]
        }), encoding="utf-8")

        before = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(before["units"][0]["summary"], "unit 内已有摘要")
        self.assertEqual(before["units"][0]["importance"], 4)
        self.assertEqual(before["units"][1]["summary"], "sidecar 已有摘要")
        self.assertEqual(before["units"][1]["importance"], 5)

        draft = self.valid_draft()
        draft["units"][0]["summary"] = "草稿摘要优先于已有摘要"
        draft["units"][0]["importance"] = 1
        api.save_draft(self.archive, self.vault, draft)
        after = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(after["units"][0]["summary"], "草稿摘要优先于已有摘要")
        self.assertEqual(after["units"][0]["importance"], 1)

    def test_issue_rejects_traversal_and_symlink_escape(self):
        issue_dir = self.write_issue()
        secret = self.base / "secret.txt"
        secret.write_text("不得读取", encoding="utf-8")
        outside_image = self.base / "outside.jpg"
        outside_image.write_bytes(b"outside")
        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        raw["units"][1]["text_path"] = "../../../secret.txt"
        (issue_dir / "issue.json").write_text(json.dumps(raw), encoding="utf-8")

        result = api.get_issue(self.archive, "zgjsb", "2026-08-31")

        self.assertNotIn("不得读取", result["units"][0]["text"])
        self.assertIn("越界", "\n".join(result["warnings"]))

        (issue_dir / "pages" / "escape.jpg").symlink_to(outside_image)
        raw["editions"][0]["page_image"] = "pages/escape.jpg"
        raw["units"][1]["page_image"] = "pages/escape.jpg"
        (issue_dir / "issue.json").write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaisesRegex(api.ValidationError, "证据目录"):
            api.get_issue(self.archive, "zgjsb", "2026-08-31")

    def test_draft_save_validates_all_editions_and_never_writes_vault(self):
        self.write_issue()
        bad = self.valid_draft()
        bad["units"][0]["topics"] = ["虚构分类"]
        bad["units"][1]["facts"][0].pop("source")

        with self.assertRaises(api.ValidationError) as caught:
            api.save_draft(self.archive, self.vault, bad)
        self.assertIn("topics", str(caught.exception))
        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))

        bad_importance = self.valid_draft()
        bad_importance["units"][0]["importance"] = 6
        with self.assertRaises(api.ValidationError):
            api.save_draft(self.archive, self.vault, bad_importance)

        saved = api.save_draft(self.archive, self.vault, self.valid_draft())
        self.assertEqual(saved["status"], "draft_saved")
        self.assertTrue(Path(saved["draft_path"]).is_file())
        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))
        issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(issue["units"][0]["summary"], self.valid_draft()["units"][0]["summary"])
        self.assertEqual(issue["units"][0]["importance"], 4)
        self.assertEqual(issue["units"][0]["title"], "人工标题：住房制度观察")

        optional_title = self.valid_draft()
        optional_title["units"][0].pop("title")
        api.save_draft(self.archive, self.vault, optional_title)

        blank_title = self.valid_draft()
        blank_title["units"][0]["title"] = "   "
        with self.assertRaises(api.ValidationError):
            api.save_draft(self.archive, self.vault, blank_title)

    def test_draft_save_accepts_incremental_editions_and_empty_review_fields(self):
        self.write_issue()
        first_increment = self.bind_draft({
            "source": "zgjsb",
            "date": "2026-08-31",
            "units": [{
                "id": "u1",
                "summary": "",
                "topics": [],
                "facts": [],
                "proofread_status": "unreviewed",
            }],
        })

        first = api.save_draft(self.archive, self.vault, first_increment)

        self.assertEqual(first["status"], "draft_saved")
        self.assertEqual(first["unit_count"], 1)
        stored = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(stored["units"], [{
            "id": "u1",
            "summary": "",
            "topics": [],
            "facts": [],
            "proofread_status": "unreviewed",
        }])
        self.assertEqual(
            api.get_issue(self.archive, "zgjsb", "2026-08-31")["status"],
            "needs_review",
        )

        second = api.save_draft(self.archive, self.vault, self.bind_draft({
            "source": "zgjsb",
            "date": "2026-08-31",
            "units": [{"id": "u2", "summary": "第二版先完成摘要"}],
        }))

        self.assertEqual(second["unit_count"], 2)
        stored = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual([unit["id"] for unit in stored["units"]], ["u1", "u2"])
        self.assertEqual(stored["units"][0]["facts"], [])
        self.assertEqual(stored["units"][1]["summary"], "第二版先完成摘要")
        self.assertNotIn("facts", stored["units"][1])
        self.assertNotIn("topics", stored["units"][1])

    def test_replaced_issue_quarantines_old_draft_and_blocks_reuse(self):
        issue_dir = self.write_issue()
        original_draft = self.valid_draft()
        api.save_draft(self.archive, self.vault, original_draft)
        original_issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(original_issue["status"], "ready_to_publish")
        self.assertFalse(original_issue["draft_stale"])

        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        raw["source_sha256"] = "f" * 64
        (issue_dir / "text" / "01.txt").write_text(
            "同日重新抓取后的第一版正文", encoding="utf-8"
        )
        (issue_dir / "pages" / "01.jpg").write_bytes(b"replaced-page-image")
        (issue_dir / "issue.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

        replaced = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        self.assertNotEqual(
            replaced["evidence_sha256"], original_issue["evidence_sha256"]
        )
        self.assertTrue(replaced["draft_stale"])
        self.assertEqual(replaced["status"], "needs_review")
        self.assertEqual(replaced["coverage"]["with_draft"], 0)
        self.assertNotEqual(
            replaced["units"][0]["summary"],
            original_draft["units"][0]["summary"],
        )
        self.assertTrue(any("旧版报纸证据" in item for item in replaced["warnings"]))

        with self.assertRaisesRegex(api.ValidationError, "证据已变化"):
            api.save_draft(self.archive, self.vault, original_draft)

        fresh = {
            "source": "zgjsb",
            "date": "2026-08-31",
            "evidence_sha256": replaced["evidence_sha256"],
            "units": [{"id": "u1", "summary": "基于新版证据重新复核"}],
        }
        api.save_draft(self.archive, self.vault, fresh)
        stored = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(stored["evidence_sha256"], replaced["evidence_sha256"])
        self.assertEqual(stored["units"], [
            {"id": "u1", "summary": "基于新版证据重新复核"}
        ])

    def test_missing_page_blocks_completion_dashboard_and_publish(self):
        issue_dir = self.write_issue()
        (issue_dir / "pages" / "01.jpg").unlink()
        draft = self.valid_draft()
        api.save_draft(self.archive, self.vault, draft)

        issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(issue["coverage"]["missing_page"], 1)
        self.assertEqual(issue["status"], "needs_review")
        dashboard = api.get_daily_dashboard(self.archive, "2026-08-31")
        row = next(
            item for item in dashboard["newspapers"]
            if item["source"] == "zgjsb"
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["acquisition_status"], "failed")
        self.assertEqual(row["review_status"], "blocked")
        self.assertEqual(row["daily_actions"]["acquired"]["status"], "failed")

        args = api._parser().parse_args([
            "publish-plan",
            "--archive", str(self.archive),
            "--vault", str(self.vault),
            "--source", "zgjsb",
            "--date", "2026-08-31",
        ])
        with mock.patch.object(api.vault_publisher, "create_plan") as create_plan:
            with self.assertRaisesRegex(api.ValidationError, "原始证据不完整"):
                api._dispatch(args)
        create_plan.assert_not_called()

    def test_legacy_draft_without_evidence_is_never_applied(self):
        self.write_issue()
        legacy_path = api._draft_path(self.archive, "zgjsb", "2026-08-31")
        legacy_path.parent.mkdir(parents=True)
        legacy = self.valid_draft()
        legacy.pop("evidence_sha256")
        legacy_path.write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
        )

        issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")

        self.assertTrue(issue["draft_stale"])
        self.assertEqual(issue["coverage"]["with_draft"], 0)
        self.assertEqual(issue["status"], "needs_review")

    def test_concurrent_incremental_draft_saves_do_not_lose_an_edition(self):
        self.write_issue()
        first = self.bind_draft({
            "source": "zgjsb", "date": "2026-08-31",
            "units": [{"id": "u1", "summary": "并发保存第一版"}],
        })
        second = self.bind_draft({
            "source": "zgjsb", "date": "2026-08-31",
            "units": [{"id": "u2", "summary": "并发保存第二版"}],
        })
        context = multiprocessing.get_context("fork")
        start = context.Event()
        barrier = context.Barrier(2)
        processes = [
            context.Process(
                target=_concurrent_draft_worker,
                args=(str(self.archive), str(self.vault), draft, start, barrier),
            )
            for draft in (first, second)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=5)
            self.assertFalse(process.is_alive(), "并发草稿保存进程超时")
            self.assertEqual(process.exitcode, 0)

        stored = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual([unit["id"] for unit in stored["units"]], ["u1", "u2"])
        self.assertEqual(stored["units"][0]["summary"], "并发保存第一版")
        self.assertEqual(stored["units"][1]["summary"], "并发保存第二版")

    def test_draft_save_holds_evidence_lock_through_atomic_write(self):
        self.write_issue()
        draft = self.bind_draft({
            "source": "zgjsb", "date": "2026-08-31",
            "units": [{"id": "u1", "summary": "锁内保存第一版"}],
        })
        context = multiprocessing.get_context("spawn")
        at_write = context.Event()
        release_write = context.Event()
        lock_ready = context.Event()
        lock_entered = context.Event()
        saver = context.Process(
            target=_delayed_draft_worker,
            args=(
                str(self.archive), str(self.vault), draft,
                at_write, release_write,
            ),
        )
        contender = context.Process(
            target=_publisher_evidence_lock_worker,
            args=(str(self.archive), "2026-08-31", lock_ready, lock_entered),
        )
        saver.start()
        try:
            self.assertTrue(at_write.wait(timeout=5), "草稿保存未到达原子写入点")
            contender.start()
            self.assertTrue(lock_ready.wait(timeout=5), "证据锁竞争进程未启动")
            self.assertFalse(
                lock_entered.wait(timeout=0.3),
                "草稿尚未写完时证据锁不应被另一事务取得",
            )
        finally:
            release_write.set()
        saver.join(timeout=5)
        contender.join(timeout=5)
        self.assertFalse(saver.is_alive(), "草稿保存进程超时")
        self.assertFalse(contender.is_alive(), "证据锁竞争进程超时")
        self.assertEqual(saver.exitcode, 0)
        self.assertEqual(contender.exitcode, 0)
        self.assertTrue(lock_entered.is_set())

    def test_concurrent_save_rejects_old_draft_plan_creation_and_apply(self):
        self.write_issue()
        api.save_draft(self.archive, self.vault, self.valid_draft())
        issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        old_draft = api._load_draft(
            self.archive, "zgjsb", "2026-08-31"
        )
        old_plan = api.vault_publisher.create_plan(
            self.archive, self.vault, issue, old_draft
        )
        changed = self.bind_draft({
            "source": "zgjsb", "date": "2026-08-31",
            "units": [{"id": "u1", "summary": "并发保存后的新版摘要"}],
        })
        context = multiprocessing.get_context("spawn")
        at_write = context.Event()
        release_write = context.Event()
        saver = context.Process(
            target=_delayed_draft_worker,
            args=(
                str(self.archive), str(self.vault), changed,
                at_write, release_write,
            ),
        )
        create_finished = threading.Event()
        create_result = {}

        def create_old_plan():
            try:
                api.vault_publisher.create_plan(
                    self.archive, self.vault, issue, old_draft
                )
            except Exception as exc:
                create_result["error"] = exc
            finally:
                create_finished.set()

        creator = threading.Thread(target=create_old_plan)
        saver.start()
        try:
            self.assertTrue(at_write.wait(timeout=5), "新版草稿未到达写入点")
            creator.start()
            self.assertFalse(
                create_finished.wait(timeout=0.3),
                "旧草稿生成计划必须等待并发保存完成",
            )
        finally:
            release_write.set()
        saver.join(timeout=5)
        creator.join(timeout=5)
        self.assertFalse(saver.is_alive(), "新版草稿保存进程超时")
        self.assertFalse(creator.is_alive(), "旧草稿计划线程超时")
        self.assertEqual(saver.exitcode, 0)
        self.assertIsInstance(
            create_result.get("error"), api.vault_publisher.ConflictError
        )
        with self.assertRaises(api.vault_publisher.ConflictError):
            api.vault_publisher.apply_plan(
                self.archive, self.vault, old_plan["plan_id"]
            )
        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))
        self.assertFalse((self.archive / "_transactions").exists())

    def test_published_status_tracks_current_draft_digest_and_recovers_after_republish(self):
        self.write_issue()
        api.save_draft(self.archive, self.vault, self.valid_draft())
        issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        first_draft = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        first_plan = api.vault_publisher.create_plan(
            self.archive, self.vault, issue, first_draft
        )
        api.vault_publisher.apply_plan(
            self.archive, self.vault, first_plan["plan_id"]
        )

        published = next(
            row for row in api.get_daily_dashboard(
                self.archive, "2026-08-31"
            )["newspapers"] if row["source"] == "zgjsb"
        )
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["publish_status"], "published")

        changed = self.bind_draft({
            "source": "zgjsb", "date": "2026-08-31",
            "units": [{"id": "u1", "summary": "发布后修订的第一版摘要"}],
        })
        api.save_draft(self.archive, self.vault, changed)

        pending = next(
            row for row in api.get_daily_dashboard(
                self.archive, "2026-08-31"
            )["newspapers"] if row["source"] == "zgjsb"
        )
        self.assertEqual(pending["status"], "ready_to_publish")
        self.assertEqual(pending["publish_status"], "pending")
        inbox_pending = next(
            row for row in api.get_inbox(
                self.archive, day="2026-08-31"
            )["issues"] if row["source"] == "zgjsb"
        )
        self.assertEqual(inbox_pending["status"], "ready_to_publish")
        self.assertEqual(inbox_pending["publish_status"], "pending")

        current_issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        current_draft = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        second_plan = api.vault_publisher.create_plan(
            self.archive, self.vault, current_issue, current_draft
        )
        api.vault_publisher.apply_plan(
            self.archive, self.vault, second_plan["plan_id"]
        )
        republished = next(
            row for row in api.get_daily_dashboard(
                self.archive, "2026-08-31"
            )["newspapers"] if row["source"] == "zgjsb"
        )
        self.assertEqual(republished["status"], "published")
        self.assertEqual(republished["publish_status"], "published")

    def test_incremental_draft_cannot_bypass_publish_ready_validation(self):
        self.write_issue()
        api.save_draft(self.archive, self.vault, self.bind_draft({
            "source": "zgjsb",
            "date": "2026-08-31",
            "units": [{"id": "u1", "summary": "仅完成一版"}],
        }))
        args = api._parser().parse_args([
            "publish-plan",
            "--archive", str(self.archive),
            "--vault", str(self.vault),
            "--source", "zgjsb",
            "--date", "2026-08-31",
        ])

        with self.assertRaises(api.ValidationError) as caught:
            api._dispatch(args)

        self.assertIn("不完整", str(caught.exception))

    def test_incremental_draft_filters_empty_fact_placeholder_and_keeps_partial_fact(self):
        self.write_issue()
        empty_fact = {field: "" for field in api._FACT_FIELDS}
        partial_fact = {
            "subject": "某市",
            "action": "",
            "object": "",
            "value": "",
            "unit": "",
            "time": "",
            "source": "",
        }

        api.save_draft(self.archive, self.vault, self.bind_draft({
            "source": "zgjsb",
            "date": "2026-08-31",
            "units": [{"id": "u1", "facts": [empty_fact, partial_fact]}],
        }))

        stored = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(stored["units"][0]["facts"], [partial_fact])
        with self.assertRaises(api.ValidationError):
            api.validate_draft(
                self.archive, stored, require_publish_ready=True
            )

    def test_capabilities_requires_an_obsidian_vault_marker(self):
        plain_directory = self.base / "not-a-vault"
        plain_directory.mkdir()

        with self.assertRaises(api.vault_publisher.PathSafetyError):
            api.capabilities(self.archive, plain_directory)

        result = api.capabilities(self.archive, self.vault)
        self.assertEqual(result["vault"], str(self.vault.resolve()))

        with self.assertRaises(api.PathSafetyError):
            api.capabilities(self.vault, self.vault)

    def test_publish_and_rollback_commands_reject_overlapping_archive_and_vault(self):
        commands = (
            ["publish-plan", "--source", "zgjsb", "--date", "2026-08-31"],
            ["publish-apply", "--plan-id", "plan-1"],
            ["rollback", "--transaction-id", "transaction-1"],
        )
        with mock.patch.object(api.vault_publisher, "create_plan") as create_plan, \
                mock.patch.object(api.vault_publisher, "apply_plan") as apply_plan, \
                mock.patch.object(api.vault_publisher, "rollback_transaction") as rollback:
            for command in commands:
                args = api._parser().parse_args([
                    command[0],
                    "--archive", str(self.vault),
                    "--vault", str(self.vault),
                    *command[1:],
                ])
                with self.subTest(command=command[0]):
                    with self.assertRaises(api.PathSafetyError):
                        api._dispatch(args)

        create_plan.assert_not_called()
        apply_plan.assert_not_called()
        rollback.assert_not_called()
        self.assertFalse((self.vault / "_plans").exists())
        self.assertFalse((self.vault / "_transactions").exists())

    def test_inbox_counts_authoritative_text_and_failed_is_not_success(self):
        self.write_issue()
        self.write_issue(source="failed-paper", issue_no="100")
        self.write_issue(source="recovered-paper", issue_no="101")
        state_ok = self.archive / "_state" / "zgjsb"
        state_bad = self.archive / "_state" / "failed-paper"
        state_recovered = self.archive / "_state" / "recovered-paper"
        state_ok.mkdir(parents=True)
        state_bad.mkdir(parents=True)
        state_recovered.mkdir(parents=True)
        (state_ok / "2026-08-31.json").write_text(
            json.dumps({"stages": {"parsed": "now"}}), encoding="utf-8"
        )
        (state_bad / "2026-08-31.json").write_text(
            json.dumps({"stages": {
                "parsed": "2026-08-31T10:00:00+08:00",
                "failed": "2026-08-31T11:00:00+08:00",
            }}), encoding="utf-8"
        )
        (state_recovered / "2026-08-31.json").write_text(
            json.dumps({"stages": {
                "failed": "2026-08-31T09:00:00+08:00",
                "parsed": "2026-08-31T12:00:00+08:00",
            }}), encoding="utf-8"
        )

        result = api.get_inbox(self.archive, day="2026-08-31")

        self.assertEqual(result["stats"]["issue_count"], 3)
        self.assertEqual(result["stats"]["success_count"], 2)
        failed = next(x for x in result["issues"] if x["source"] == "failed-paper")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["publish_status"], "blocked")
        recovered = next(x for x in result["issues"] if x["source"] == "recovered-paper")
        self.assertNotEqual(recovered["status"], "failed")
        # 每期 7 + 5 字；重复 article 文本不重复计数。
        ok = next(x for x in result["issues"] if x["source"] == "zgjsb")
        self.assertEqual(ok["text_length"], 23)

    def test_inbox_source_filter_is_honored_by_api_dispatch(self):
        self.write_issue(source="zgjsb")
        self.write_issue(source="rmrb", issue_no="30100")
        args = api._parser().parse_args([
            "inbox",
            "--archive", str(self.archive),
            "--vault", str(self.vault),
            "--source", "zgjsb",
        ])

        result, _warnings = api._dispatch(args)

        self.assertEqual([item["source"] for item in result["issues"]], ["zgjsb"])

    def test_all_eight_sources_can_save_archive_only_drafts_but_only_zgjsb_can_publish(self):
        for number, paper in enumerate(api.newspaper_registry()["newspapers"], 1):
            with self.subTest(source=paper["source"]):
                self.write_issue(
                    source=paper["source"],
                    source_name=paper["source_name"],
                    issue_no=str(30000 + number),
                )
                draft = self.valid_draft(paper["source"])
                saved = api.save_draft(self.archive, self.vault, draft)
                self.assertEqual(saved["status"], "draft_saved")
                self.assertEqual(saved["source"], paper["source"])
        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))

        issue = api.get_issue(self.archive, "rmrb", "2026-08-31")
        loaded_draft = api._load_draft(self.archive, "rmrb", "2026-08-31")
        with self.assertRaises(api.vault_publisher.PublisherError):
            api.vault_publisher.create_plan(self.archive, self.vault, issue, loaded_draft)

        source_pdf = self.base / "人民日报_2026-08-31_第30100期.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\nlocal-test\n%%EOF")
        with self.assertRaises(api.ValidationError):
            api.import_file(self.archive, source_pdf, source="rmrb")

    def test_draft_rejects_sources_outside_the_locked_eight(self):
        self.write_issue(source="other", source_name="其他报纸", issue_no="1")
        draft = self.valid_draft("other")

        with self.assertRaises(api.ValidationError) as caught:
            api.save_draft(self.archive, self.vault, draft)

        self.assertIn("8家", str(caught.exception))

    def test_draft_save_rejects_archive_root_equal_to_vault(self):
        same_root = self.base / "same-draft-root"
        same_root.mkdir()
        (same_root / ".obsidian").mkdir()
        self.write_issue(archive=same_root)

        with self.assertRaises(api.PathSafetyError):
            api.save_draft(same_root, same_root, {
                "source": "zgjsb", "date": "2026-08-31", "units": []
            })

        self.assertFalse((same_root / "_drafts").exists())

    def test_draft_save_reports_persistence_failure_without_touching_vault(self):
        self.write_issue(source="bjrb", source_name="北京日报")
        draft = self.valid_draft("bjrb")

        with mock.patch.object(api, "_atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(api.APIError) as caught:
                api.save_draft(self.archive, self.vault, draft)

        self.assertIn("草稿保存失败", str(caught.exception))
        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))

    def test_archive_json_writes_fsync_parent_directories(self):
        self.write_issue(source="kjrb", source_name="科技日报")
        directory_syncs = []
        real_fsync = api.os.fsync

        def track_fsync(descriptor):
            descriptor_stat = os.fstat(descriptor)
            if stat.S_ISDIR(descriptor_stat.st_mode):
                directory_syncs.append(
                    (descriptor_stat.st_dev, descriptor_stat.st_ino)
                )
            return real_fsync(descriptor)

        with mock.patch.object(api.os, "fsync", side_effect=track_fsync):
            api.save_draft(
                self.archive, self.vault, self.valid_draft("kjrb")
            )
            api.mark_reading_activity(
                self.archive, self.vault, "kjrb", "2026-08-31", "opened"
            )

        expected = {
            (path.stat().st_dev, path.stat().st_ino)
            for path in (
                self.archive,
                self.archive / "_drafts",
                self.archive / "_drafts" / "kjrb",
                self.archive / "_activity",
            )
        }
        self.assertTrue(expected.issubset(set(directory_syncs)))

    def test_archive_json_parent_swap_to_vault_never_writes_vault(self):
        self.write_issue(source="kjrb", source_name="科技日报")
        vault_destination = self.vault / "archive-write-trap"
        vault_destination.mkdir()
        drafts = self.archive / "_drafts"
        displaced = self.archive / "_drafts-displaced"
        real_link = api.os.link
        swapped = False

        def swap_parent_before_commit(source, destination, **kwargs):
            nonlocal swapped
            if not swapped and destination == "2026-08-31.json":
                swapped = True
                drafts.rename(displaced)
                drafts.symlink_to(vault_destination, target_is_directory=True)
            return real_link(source, destination, **kwargs)

        with mock.patch.object(
            api.os, "link", side_effect=swap_parent_before_commit
        ):
            with self.assertRaisesRegex(
                api.PathSafetyError, "祖先目录.*被替换"
            ):
                api.save_draft(
                    self.archive, self.vault, self.valid_draft("kjrb")
                )

        self.assertTrue(swapped)
        self.assertEqual(list(vault_destination.rglob("*")), [])

    def test_archive_root_swap_after_entry_pin_rejects_draft_and_activity(self):
        self.write_issue(source="kjrb", source_name="科技日报")
        vault_destination = self.vault / "archive-root-write-trap"
        vault_destination.mkdir()

        operations = (
            lambda: api.save_draft(
                self.archive, self.vault, self.valid_draft("kjrb")
            ),
            lambda: api.mark_reading_activity(
                self.archive, self.vault, "kjrb", "2026-08-31", "opened"
            ),
        )
        for index, operation in enumerate(operations):
            displaced = self.base / ("archive-displaced-%s" % index)
            real_atomic = api._atomic_json
            swapped = False

            def swap_root_before_atomic(target, payload):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    self.archive.rename(displaced)
                    self.archive.symlink_to(
                        vault_destination, target_is_directory=True
                    )
                return real_atomic(target, payload)

            with self.subTest(operation=index), mock.patch.object(
                api, "_atomic_json", side_effect=swap_root_before_atomic
            ):
                with self.assertRaises(api.PathSafetyError):
                    operation()
            self.assertTrue(swapped)
            self.assertEqual(list(vault_destination.rglob("*")), [])
            self.archive.unlink()
            displaced.rename(self.archive)

    def test_directory_fsync_failure_is_reported_for_draft_and_activity(self):
        self.write_issue(source="kjrb", source_name="科技日报")
        real_fsync = api.os.fsync

        def fail_directory_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("injected directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(
            api.os, "fsync", side_effect=fail_directory_fsync
        ):
            with self.assertRaisesRegex(
                api.PersistenceError, "草稿保存失败"
            ):
                api.save_draft(
                    self.archive, self.vault, self.valid_draft("kjrb")
                )

        with mock.patch.object(
            api.os, "fsync", side_effect=fail_directory_fsync
        ):
            with self.assertRaisesRegex(
                api.PersistenceError, "阅读动作保存失败"
            ):
                api.mark_reading_activity(
                    self.archive, self.vault, "kjrb", "2026-08-31", "opened"
                )

    def test_registry_contract_has_locked_sources_categories_and_order(self):
        result = api.newspaper_registry()

        self.assertEqual(
            [item["source"] for item in result["newspapers"]],
            ["rmrb", "gmrb", "jjrb", "zgjsb", "kjrb", "nmrb", "nfrb", "bjrb"],
        )
        self.assertEqual(
            [category["name"] for category in result["categories"]],
            ["中央党报", "部委行业报", "地方党报"],
        )
        self.assertEqual(
            [[paper["source"] for paper in category["newspapers"]]
             for category in result["categories"]],
            [["rmrb", "gmrb", "jjrb"], ["zgjsb", "kjrb", "nmrb"], ["nfrb", "bjrb"]],
        )
        self.assertEqual(result["expected_count"], 8)
        self.assertTrue(all(item["enabled"] for item in result["newspapers"]))
        construction = next(x for x in result["newspapers"] if x["source"] == "zgjsb")
        self.assertTrue(construction["can_publish"])
        self.assertFalse(next(x for x in result["newspapers"] if x["source"] == "rmrb")["can_publish"])

    def test_daily_dashboard_returns_all_eight_with_missing_placeholders_by_date(self):
        self.write_issue(source="zgjsb", source_name="中国建设报", issue_no="9167")
        self.write_issue(source="rmrb", source_name="人民日报", issue_no="30100")
        state_bad = self.archive / "_state" / "rmrb"
        state_bad.mkdir(parents=True)
        (state_bad / "2026-08-31.json").write_text(json.dumps({
            "stages": {
                "parsed": "2026-08-31T10:00:00+08:00",
                "failed": "2026-08-31T11:00:00+08:00",
            }
        }), encoding="utf-8")

        result = api.get_daily_dashboard(self.archive, "2026-08-31")

        self.assertEqual(result["date"], "2026-08-31")
        self.assertEqual(len(result["newspapers"]), 8)
        self.assertEqual(result["stats"]["expected_count"], 8)
        self.assertEqual(result["stats"]["available_count"], 2)
        self.assertEqual(result["stats"]["missing_count"], 6)
        self.assertEqual(result["stats"]["failed_count"], 1)
        self.assertEqual(
            [row["source"] for row in result["categories"][0]["newspapers"]],
            ["rmrb", "gmrb", "jjrb"],
        )
        missing = next(row for row in result["newspapers"] if row["source"] == "gmrb")
        self.assertFalse(missing["available"])
        self.assertEqual(missing["status"], "missing")
        self.assertEqual(missing["review_status"], "not_started")
        self.assertEqual(missing["daily_actions"]["acquired"]["status"], "pending")
        failed = next(row for row in result["newspapers"] if row["source"] == "rmrb")
        self.assertEqual(failed["daily_actions"]["acquired"]["status"], "failed")
        construction = next(row for row in result["newspapers"] if row["source"] == "zgjsb")
        self.assertTrue(construction["available"])
        self.assertEqual(construction["acquisition_status"], "complete")
        self.assertEqual(construction["reading_status"], "unread")
        self.assertIsNone(construction["last_read_at"])
        self.assertEqual(construction["daily_actions"]["acquired"]["status"], "complete")
        self.assertEqual(construction["daily_actions"]["read"]["status"], "pending")

    def test_daily_dashboard_defaults_to_latest_locked_source_date_and_lists_available_dates(self):
        self.write_issue(source="rmrb", source_name="人民日报", day="2026-08-30")
        self.write_issue(source="gmrb", source_name="光明日报", day="2026-08-31")
        self.write_issue(source="other", source_name="其他报纸", day="2026-09-01")

        result = api.get_daily_dashboard(self.archive)

        self.assertEqual(result["date"], "2026-08-31")
        self.assertEqual(result["available_dates"], ["2026-08-31", "2026-08-30"])
        self.assertEqual(len(result["newspapers"]), 8)

    def test_issue_preserves_raw_ocr_and_round_trips_manual_proofreading(self):
        self.write_issue()
        draft = self.valid_draft()
        draft["units"][0].update({
            "text": "不得用草稿覆盖原OCR",
            "ocr_text": "也不得用此字段覆盖原OCR",
            "corrected_ocr_text": "第一版安全正文（人工校对）",
            "proofread_status": "edited",
            "ocr_suspicions": ["“安企”疑为“安全”", "  “安企”疑为“安全”  "],
        })

        api.save_draft(self.archive, self.vault, draft)
        result = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        unit = result["units"][0]

        self.assertEqual(unit["text"], "第一版安全正文")
        self.assertEqual(unit["ocr_text"], "第一版安全正文")
        self.assertEqual(unit["ocr_blocks"], [
            {"kind": "paragraph", "text": "第一版安全正文"}
        ])
        self.assertEqual(unit["corrected_ocr_text"], "第一版安全正文（人工校对）")
        self.assertEqual(unit["proofread_status"], "edited")
        self.assertEqual(unit["ocr_suspicions"], ["“安企”疑为“安全”"])

        stored = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        self.assertNotIn("text", stored["units"][0])
        self.assertNotIn("ocr_text", stored["units"][0])

    def test_restoring_original_clears_previous_corrected_text(self):
        self.write_issue()
        first = self.bind_draft({
            "source": "zgjsb",
            "date": "2026-08-31",
            "units": [{
                "id": "u1",
                "corrected_ocr_text": "第一版人工校订",
                "proofread_status": "edited",
            }],
        })
        api.save_draft(self.archive, self.vault, first)

        restored = self.bind_draft({
            "source": "zgjsb",
            "date": "2026-08-31",
            "units": [{"id": "u1", "proofread_status": "unreviewed"}],
        })
        api.save_draft(self.archive, self.vault, restored)

        stored = api._load_draft(self.archive, "zgjsb", "2026-08-31")
        unit = next(item for item in stored["units"] if item["id"] == "u1")
        self.assertIn("corrected_ocr_text", unit)
        self.assertIsNone(unit["corrected_ocr_text"])
        reloaded = api.get_issue(self.archive, "zgjsb", "2026-08-31")["units"][0]
        self.assertIsNone(reloaded["corrected_ocr_text"])
        self.assertEqual(reloaded["proofread_status"], "unreviewed")

    def test_proofreading_fields_are_optional_and_validated_without_guessing(self):
        self.write_issue()

        api.save_draft(self.archive, self.vault, self.valid_draft())
        unit = api.get_issue(self.archive, "zgjsb", "2026-08-31")["units"][0]
        self.assertIsNone(unit["corrected_ocr_text"])
        self.assertEqual(unit["proofread_status"], "unreviewed")
        self.assertEqual(unit["ocr_suspicions"], [])

        bad_status = self.valid_draft()
        bad_status["units"][0]["proofread_status"] = "auto_corrected"
        with self.assertRaises(api.ValidationError):
            api.save_draft(self.archive, self.vault, bad_status)

        bad_suspicions = self.valid_draft()
        bad_suspicions["units"][0]["ocr_suspicions"] = [{"guess": "未知"}]
        with self.assertRaises(api.ValidationError):
            api.save_draft(self.archive, self.vault, bad_suspicions)

    def test_ocr_blocks_follow_article_order_without_rewriting_text(self):
        self.write_issue()

        unit = api.get_issue(self.archive, "zgjsb", "2026-08-31")["units"][1]

        self.assertEqual(unit["ocr_blocks"], [
            {"kind": "article", "title": "重复标题", "text": "第二版正文"},
            {"kind": "article", "title": "补充标题", "text": "补充事实"},
        ])

    def test_ocr_article_blocks_preserve_supplied_whitespace_and_characters(self):
        issue_dir = self.write_issue()
        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        raw["units"][0]["articles"][0]["title"] = "  原题  "
        raw["units"][0]["articles"][0]["text"] = "  原文第一行\n原文第二行  "
        (issue_dir / "issue.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

        block = api.get_issue(self.archive, "zgjsb", "2026-08-31")["units"][1]["ocr_blocks"][0]

        self.assertEqual(block["title"], "  原题  ")
        self.assertEqual(block["text"], "  原文第一行\n原文第二行  ")

    def test_reading_activity_is_archive_only_and_dashboard_round_trips_state(self):
        self.write_issue(source="gmrb", source_name="光明日报", issue_no="1")

        opened = api.mark_reading_activity(
            self.archive, self.vault, "gmrb", "2026-08-31", "opened"
        )
        self.assertEqual(opened["reading_status"], "opened")
        self.assertTrue(opened["last_read_at"])
        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))
        completed = api.mark_reading_activity(
            self.archive, self.vault, "gmrb", "2026-08-31", "completed"
        )
        self.assertEqual(completed["reading_status"], "completed")
        self.assertEqual(completed["opened_at"], opened["opened_at"])
        dashboard = api.get_daily_dashboard(self.archive, "2026-08-31")
        row = next(x for x in dashboard["newspapers"] if x["source"] == "gmrb")
        self.assertEqual(row["reading_status"], "completed")
        self.assertEqual(row["last_read_at"], completed["last_read_at"])
        self.assertEqual(row["daily_actions"]["read"]["status"], "complete")

        unread = api.mark_reading_activity(
            self.archive, self.vault, "gmrb", "2026-08-31", "unread"
        )
        self.assertEqual(unread["reading_status"], "unread")

    def test_concurrent_reading_updates_do_not_lose_another_newspaper(self):
        context = multiprocessing.get_context("fork")
        start = context.Event()
        barrier = context.Barrier(2)
        processes = [
            context.Process(
                target=_concurrent_activity_worker,
                args=(
                    str(self.archive), str(self.vault), source,
                    "2026-08-31", start, barrier,
                ),
            )
            for source in ("zgjsb", "rmrb")
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=5)
            self.assertFalse(process.is_alive(), "并发阅读状态保存进程超时")
            self.assertEqual(process.exitcode, 0)

        activity = api._load_daily_activity(self.archive, "2026-08-31")
        self.assertEqual(set(activity["newspapers"]), {"zgjsb", "rmrb"})
        self.assertEqual(
            {row["reading_status"] for row in activity["newspapers"].values()},
            {"opened"},
        )

    def test_reading_activity_rejects_invalid_source_status_and_missing_completion(self):
        with self.assertRaises(api.ValidationError):
            api.mark_reading_activity(
                self.archive, self.vault, "other", "2026-08-31", "opened"
            )
        with self.assertRaises(api.ValidationError):
            api.mark_reading_activity(
                self.archive, self.vault, "rmrb", "2026-08-31", "guessed"
            )
        with self.assertRaises(api.ValidationError) as caught:
            api.mark_reading_activity(
                self.archive, self.vault, "rmrb", "2026-08-31", "completed"
            )
        self.assertIn("缺报", str(caught.exception))

    def test_reading_activity_reports_persistence_failure_without_touching_vault(self):
        self.write_issue(source="kjrb", source_name="科技日报")

        with mock.patch.object(api, "_atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(api.APIError) as caught:
                api.mark_reading_activity(
                    self.archive, self.vault, "kjrb", "2026-08-31", "opened"
                )

        self.assertIn("阅读动作保存失败", str(caught.exception))
        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))

    def test_reading_activity_rejects_archive_symlink_into_vault(self):
        self.write_issue(source="kjrb", source_name="科技日报")
        (self.archive / "_activity").symlink_to(self.vault, target_is_directory=True)

        with self.assertRaises(api.PathSafetyError):
            api.mark_reading_activity(
                self.archive, self.vault, "kjrb", "2026-08-31", "opened"
            )

        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))

    def test_reading_activity_rejects_archive_root_equal_to_or_aliasing_vault(self):
        same_root = self.base / "same-root"
        same_root.mkdir()
        (same_root / ".obsidian").mkdir()
        self.write_issue(source="kjrb", source_name="科技日报")

        with self.assertRaises(api.PathSafetyError):
            api.mark_reading_activity(
                same_root, same_root, "kjrb", "2026-08-31", "opened"
            )
        self.assertFalse((same_root / "_activity").exists())

        vault_alias = self.base / "vault-alias"
        vault_alias.symlink_to(self.vault, target_is_directory=True)
        with self.assertRaises(api.PathSafetyError):
            api.mark_reading_activity(
                self.vault, vault_alias, "kjrb", "2026-08-31", "opened"
            )
        self.assertFalse((self.vault / "_activity").exists())

    def test_non_construction_draft_allows_topics_and_facts_to_be_omitted(self):
        self.write_issue(source="nfrb", source_name="南方日报", issue_no="100")
        draft = self.valid_draft("nfrb")
        for unit in draft["units"]:
            unit.pop("topics")
            unit.pop("facts")

        saved = api.save_draft(self.archive, self.vault, draft)
        self.assertEqual(saved["source"], "nfrb")
        stored = api._load_draft(self.archive, "nfrb", "2026-08-31")
        self.assertNotIn("topics", stored["units"][0])
        self.assertNotIn("facts", stored["units"][0])
        issue = api.get_issue(self.archive, "nfrb", "2026-08-31")
        self.assertEqual(issue["status"], "review_complete")
        dashboard = api.get_daily_dashboard(self.archive, "2026-08-31")
        row = next(item for item in dashboard["newspapers"] if item["source"] == "nfrb")
        self.assertEqual(row["status"], "review_complete")
        self.assertEqual(row["review_status"], "complete")
        self.assertEqual(row["publish_status"], "not_supported")

    def test_non_construction_archived_stage_is_not_reported_as_published(self):
        self.write_issue(source="nmrb", source_name="农民日报", issue_no="13394")
        state_dir = self.archive / "_state" / "nmrb"
        state_dir.mkdir(parents=True)
        (state_dir / "2026-08-31.json").write_text(json.dumps({
            "stages": {"archived": "2026-08-31T12:00:00+08:00"}
        }), encoding="utf-8")

        row = next(
            item for item in api.get_daily_dashboard(self.archive, "2026-08-31")["newspapers"]
            if item["source"] == "nmrb"
        )

        self.assertNotEqual(row["status"], "published")
        self.assertEqual(row["publish_status"], "not_supported")
        self.assertEqual(row["daily_actions"]["published"]["status"], "not_applicable")

    def test_import_file_deduplicates_and_surfaces_filename_header_conflict(self):
        self.write_issue(issue_no="9167")
        source_pdf = self.base / "《中国建设报》2026-08-31_第9867期_电子报_高清.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\nlocal-test\n%%EOF")

        def fake_render(_pdf, output_dir, _archive, accurate=True):
            output_dir = Path(output_dir)
            (output_dir / "pages").mkdir(parents=True)
            (output_dir / "text").mkdir(parents=True)
            (output_dir / "pages" / "01版.jpg").write_bytes(b"jpeg")
            (output_dir / "pages" / "02版.jpg").write_bytes(b"jpeg-two")
            (output_dir / "text" / "edition_01.txt").write_text(
                "中国建设报 2026年8月31日 第9167期 今日8版",
                encoding="utf-8",
            )
            (output_dir / "text" / "edition_02.txt").write_text(
                "中国建设报第二版正文", encoding="utf-8"
            )
            return {
                "page_count": 2,
                "pages": [
                    {
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 48,
                    },
                    {
                        "number": 2,
                        "image": "pages/02版.jpg",
                        "text": "text/edition_02.txt",
                        "characters": 10,
                    },
                ],
            }

        with mock.patch.object(api.local_pdf, "run_pdfocr", side_effect=fake_render):
            first = api.import_file(self.archive, source_pdf, source="zgjsb")
            second = api.import_file(self.archive, source_pdf, source="zgjsb")

        self.assertEqual(first["status"], "needs_review")
        self.assertTrue(first["issue_linked"])
        self.assertIn("9867", "\n".join(first["warnings"]))
        self.assertIn("9167", "\n".join(first["warnings"]))
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        imported = Path(first["pdf_path"])
        self.assertTrue(imported.is_file())
        self.assertEqual(imported.parent.name, first["sha256"])
        issue = api.get_issue(self.archive, "zgjsb", "2026-08-31")
        self.assertEqual(issue["pdf_path"], str(imported))

    def test_import_without_issue_delegates_to_local_pdf_and_validates_input(self):
        source_pdf = self.base / "建设报_2026-09-02_第9169期.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\npending\n%%EOF")
        delegated = {
            "source": "zgjsb",
            "date": "2026-09-02",
            "issue_no": "9169",
            "page_count": 8,
            "pdf_path": str(source_pdf),
            "issue_path": str(self.archive / "zgjsb" / "2026-09-02" / "issue.json"),
            "source_sha256": api.local_pdf.sha256_file(source_pdf),
            "warnings": ["第2版 OCR 文字不足，需人工复核或重跑。"],
            "needs_review": True,
            "imported": True,
        }
        with mock.patch.object(api.local_pdf, "import_pdf", return_value=delegated) as importer:
            result = api.import_file(self.archive, source_pdf, source="zgjsb")
        self.assertEqual(result["status"], "needs_review")
        self.assertFalse(result["issue_linked"])
        importer.assert_called_once()

        fake = self.base / "fake.pdf"
        fake.write_text("not a pdf", encoding="utf-8")
        with self.assertRaises(api.ValidationError):
            api.import_file(self.archive, fake, source="zgjsb")
        with mock.patch.object(api, "MAX_PDF_BYTES", 8):
            with self.assertRaises(api.ValidationError):
                api.import_file(self.archive, source_pdf, source="zgjsb")

    def test_import_file_passes_explicit_custom_vault_to_local_importer(self):
        source_pdf = self.base / "建设报_2026-09-02_第9169期.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\ncustom-vault\n%%EOF")
        custom_vault = self.base / "custom-vault"
        custom_vault.mkdir()
        (custom_vault / ".obsidian").mkdir()
        delegated = {
            "source": "zgjsb",
            "date": "2026-09-02",
            "issue_no": "9169",
            "page_count": 1,
            "pdf_path": str(source_pdf),
            "issue_path": str(
                self.archive / "zgjsb" / "2026-09-02" / "issue.json"
            ),
            "source_sha256": api.local_pdf.sha256_file(source_pdf),
            "warnings": [],
            "needs_review": False,
            "imported": True,
        }
        with mock.patch.object(
                api.local_pdf, "import_pdf", return_value=delegated) as importer:
            api.import_file(
                self.archive,
                source_pdf,
                source="zgjsb",
                vault_root=custom_vault,
            )

        self.assertEqual(importer.call_args.kwargs["vault_root"], custom_vault)

    def test_import_file_rejects_archive_equal_to_or_aliasing_vault_before_import(self):
        source_pdf = self.base / "建设报_2026-09-02_第9169期.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\nlocal-test\n%%EOF")
        vault_alias = self.base / "vault-alias"
        vault_alias.symlink_to(self.vault, target_is_directory=True)

        with mock.patch.object(api.local_pdf, "import_pdf") as importer:
            with self.assertRaises(api.PathSafetyError):
                api.import_file(
                    self.vault,
                    source_pdf,
                    source="zgjsb",
                    vault_root=self.vault,
                )
            with self.assertRaises(api.PathSafetyError):
                api.import_file(
                    vault_alias,
                    source_pdf,
                    source="zgjsb",
                    vault_root=self.vault,
                )
            with self.assertRaises(api.PathSafetyError):
                api.import_file(
                    self.base,
                    source_pdf,
                    source="zgjsb",
                    vault_root=self.vault,
                )

        importer.assert_not_called()
        self.assertFalse((self.vault / "_imports").exists())

    def test_import_file_rejects_archive_descendant_of_vault_via_api_dispatch(self):
        source_pdf = self.base / "建设报_2026-09-02_第9169期.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\nlocal-test\n%%EOF")
        archive_in_vault = self.vault / "private-archive"
        args = api._parser().parse_args([
            "import-file",
            "--archive", str(archive_in_vault),
            "--vault", str(self.vault),
            "--source", "zgjsb",
            "--path", str(source_pdf),
        ])

        with mock.patch.object(api.local_pdf, "import_pdf") as importer:
            with self.assertRaises(api.PathSafetyError):
                api._dispatch(args)

        importer.assert_not_called()
        self.assertFalse(archive_in_vault.exists())

    def test_import_file_rejects_write_namespace_symlinked_into_vault(self):
        source_pdf = self.base / "建设报_2026-09-02_第9169期.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\nlocal-test\n%%EOF")
        (self.archive / "_imports").symlink_to(self.vault, target_is_directory=True)

        with mock.patch.object(api.local_pdf, "import_pdf") as importer:
            with self.assertRaises(api.PathSafetyError):
                api.import_file(
                    self.archive,
                    source_pdf,
                    source="zgjsb",
                    vault_root=self.vault,
                )

        importer.assert_not_called()
        self.assertFalse(any(path.is_file() for path in self.vault.rglob("*")))

    def test_cli_api_envelope_and_public_routes(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "readdaily.py"),
                "api",
                "capabilities",
                "--archive",
                str(self.archive),
                "--vault",
                str(self.vault),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(payload["ok"])
        self.assertIn("import-file", payload["data"]["commands"])
        self.assertIn("daily-dashboard", payload["data"]["commands"])
        self.assertIn("reading-mark", payload["data"]["commands"])
        self.assertEqual(payload["data"]["newspaper_registry"]["expected_count"], 8)

        dashboard_proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "readdaily.py"),
                "api",
                "daily-dashboard",
                "--archive",
                str(self.archive),
                "--date",
                "2026-08-31",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        dashboard_payload = json.loads(dashboard_proc.stdout)
        self.assertEqual(dashboard_proc.returncode, 0, dashboard_proc.stderr)
        self.assertEqual(len(dashboard_payload["data"]["newspapers"]), 8)

        fetch_args = argparse.Namespace(date=None, source=None, stage=None, offline=False)
        reader_args = argparse.Namespace(cmd="prepare", date="2026-09-02", entity=None)
        with mock.patch.object(cli.subprocess, "run") as run_mock:
            run_mock.return_value.returncode = 0
            cli.cmd_fetch(fetch_args)
            fetch_cmd = run_mock.call_args.args[0]
            self.assertEqual(fetch_cmd[:2], [sys.executable, cli.FETCHER])
            self.assertIn("--registry", fetch_cmd)
            self.assertIn(cli.REGISTRY, fetch_cmd)
        with mock.patch.object(cli, "run", return_value=0) as reader_run:
            cli.cmd_reader(reader_args)
            self.assertEqual(reader_run.call_args.args[1], "prepare")


if __name__ == "__main__":
    unittest.main()
