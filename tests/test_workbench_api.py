import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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


class WorkbenchAPITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.archive = self.base / "archive"
        self.vault = self.base / "vault"
        self.archive.mkdir()
        self.vault.mkdir()
        (self.vault / ".obsidian").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_issue(self, source="zgjsb", day="2026-08-31", issue_no="9167"):
        issue_dir = self.archive / source / day
        (issue_dir / "pages").mkdir(parents=True)
        (issue_dir / "text").mkdir()
        (issue_dir / "pages" / "01.jpg").write_bytes(b"jpeg-one")
        (issue_dir / "pages" / "02.jpg").write_bytes(b"jpeg-two")
        (issue_dir / "text" / "01.txt").write_text("第一版安全正文", encoding="utf-8")
        issue = {
            "source": source,
            "source_name": "中国建设报",
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

    def valid_draft(self):
        fact = {
            "subject": "某市",
            "action": "实施",
            "object": "城市更新",
            "value": "12",
            "unit": "个项目",
            "time": "2026年",
            "source": "中国建设报原版",
        }
        return {
            "source": "zgjsb",
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
        }

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
        (issue_dir / "pages" / "escape.jpg").symlink_to(outside_image)
        raw = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        raw["units"][1]["text_path"] = "../../../secret.txt"
        raw["editions"][0]["page_image"] = "pages/escape.jpg"
        raw["units"][1]["page_image"] = "pages/escape.jpg"
        (issue_dir / "issue.json").write_text(json.dumps(raw), encoding="utf-8")

        result = api.get_issue(self.archive, "zgjsb", "2026-08-31")

        self.assertNotIn("不得读取", result["units"][0]["text"])
        self.assertIsNone(result["units"][0]["page_image"])
        joined = "\n".join(result["warnings"])
        self.assertIn("越界", joined)
        self.assertIn("符号链接", joined)

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

    def test_capabilities_requires_an_obsidian_vault_marker(self):
        plain_directory = self.base / "not-a-vault"
        plain_directory.mkdir()

        with self.assertRaises(api.vault_publisher.PathSafetyError):
            api.capabilities(self.archive, plain_directory)

        result = api.capabilities(self.archive, self.vault)
        self.assertEqual(result["vault"], str(self.vault.resolve()))

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

    def test_mutating_construction_api_rejects_non_zgjsb_source(self):
        self.write_issue(source="rmrb", issue_no="30100")
        draft = self.valid_draft()
        draft["source"] = "rmrb"

        with self.assertRaises(api.ValidationError):
            api.save_draft(self.archive, self.vault, draft)

        source_pdf = self.base / "人民日报_2026-08-31_第30100期.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\nlocal-test\n%%EOF")
        with self.assertRaises(api.ValidationError):
            api.import_file(self.archive, source_pdf, source="rmrb")

    def test_import_file_deduplicates_and_surfaces_filename_header_conflict(self):
        self.write_issue(issue_no="9167")
        source_pdf = self.base / "《中国建设报》2026-08-31_第9867期_电子报_高清.pdf"
        source_pdf.write_bytes(b"%PDF-1.4\nlocal-test\n%%EOF")

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
