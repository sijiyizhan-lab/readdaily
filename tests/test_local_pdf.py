import json
import multiprocessing
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(FETCH_SCRIPTS))

import local_pdf  # noqa: E402


PDF_BYTES = b"%PDF-1.4\n% local fixture\n"


def _shared_evidence_lock_worker(kind, archive, day, ready, entered, rejected):
    if kind == "fetch":
        import fetch
        lock = fetch.fetch_source_evidence_lock
        expected_rejection = None
    else:
        reader_scripts = ROOT / "skills" / "newspaper-reader" / "scripts"
        sys.path.insert(0, str(reader_scripts))
        import vault_publisher
        lock = vault_publisher._fetch_source_evidence_lock
        expected_rejection = None
    ready.set()
    try:
        with lock(archive, "zgjsb", day):
            entered.set()
    except Exception as exc:
        if expected_rejection is None or not isinstance(exc, expected_rejection):
            raise
        rejected.set()


class LocalPDFMetadataTests(unittest.TestCase):
    def test_filename_metadata_extracts_date_and_issue(self):
        meta = local_pdf.parse_filename_metadata(
            "《中国建设报》2026-09-01_第9168期_电子报_高清.pdf"
        )
        self.assertEqual(meta["date"], "2026-09-01")
        self.assertEqual(meta["issue_no"], "9168")

    def test_header_issue_wins_and_conflict_requires_review(self):
        issue, warnings, needs_review = local_pdf.resolve_issue_no(
            "9867", "2026年8月31日 星期一 第9167期 今日4版"
        )
        self.assertEqual(issue, "9167")
        self.assertTrue(needs_review)
        self.assertTrue(any("9867" in item and "9167" in item for item in warnings))

    def test_header_date_requires_one_unambiguous_calendar_date(self):
        self.assertEqual(
            local_pdf.issue_dates_from_header(
                "中国建设报\n2026年9月3日星期四 第9170期 今日4版\n正文"
            ),
            ["2026-09-03"],
        )
        self.assertEqual(
            local_pdf.issue_dates_from_header(
                "2026年9月3日 第9170期\n另见2026-09-02"
            ),
            ["2026-09-02", "2026-09-03"],
        )

    def test_rejects_non_pdf_magic_even_with_pdf_extension(self):
        with tempfile.TemporaryDirectory() as td:
            fake = pathlib.Path(td) / "fake.pdf"
            fake.write_bytes(b"not really a pdf")
            with self.assertRaisesRegex(ValueError, "PDF"):
                local_pdf.validate_pdf(fake)

    def test_prebuilt_pdfocr_is_used_without_swift_compiler(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            helper = root / "pdfocr"
            helper.write_bytes(b"prebuilt")
            helper.chmod(0o755)
            archive = root / "archive"

            with mock.patch.object(local_pdf, "PREBUILT_PDFOCR", helper, create=True), \
                    mock.patch.object(local_pdf.shutil, "which", return_value=None):
                self.assertEqual(local_pdf._helper_binary(archive), helper)


class LocalPDFImportTests(unittest.TestCase):
    def test_empty_archive_import_uses_durable_directory_chains(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            pdf = root / "《中国建设报》2026-09-03_第9170期_电子报_高清.pdf"
            pdf.write_bytes(PDF_BYTES)
            archive = root / "archive"
            resolved_archive = archive.resolve()
            digest = local_pdf.sha256_file(pdf)
            durable_directory_calls = []
            real_durable_makedirs = local_pdf.lib.durable_makedirs

            def observe_durable_makedirs(path, *args, **kwargs):
                durable_directory_calls.append(pathlib.Path(path))
                return real_durable_makedirs(path, *args, **kwargs)

            def fake_render(_pdf, output_dir, _archive, accurate=True):
                output_dir = pathlib.Path(output_dir)
                (output_dir / "pages").mkdir(parents=True)
                (output_dir / "text").mkdir(parents=True)
                (output_dir / "pages" / "01版.jpg").write_bytes(b"jpeg")
                (output_dir / "text" / "edition_01.txt").write_text(
                    "中国建设报 2026年9月3日 第9170期 今日1版 正文" * 4,
                    encoding="utf-8",
                )
                return {
                    "page_count": 1,
                    "pages": [{
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 100,
                    }],
                }

            with mock.patch.object(
                local_pdf.lib,
                "durable_makedirs",
                side_effect=observe_durable_makedirs,
            ), mock.patch.object(
                local_pdf, "run_pdfocr", side_effect=fake_render
            ):
                result = local_pdf.import_pdf(pdf, archive)

            expected = {
                resolved_archive,
                resolved_archive / "_imports",
                resolved_archive / "_imports" / digest,
                resolved_archive / "zgjsb",
                resolved_archive / "_state" / "zgjsb",
            }
            self.assertTrue(
                expected.issubset(set(durable_directory_calls)),
                "missing durable directory calls: %s; observed=%s"
                % (
                    sorted(str(path) for path in expected - set(durable_directory_calls)),
                    [str(path) for path in durable_directory_calls],
                ),
            )
            self.assertTrue(pathlib.Path(result["issue_path"]).is_file())

    def test_content_addressed_copy_reports_parent_fsync_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            source = root / "source.pdf"
            source.write_bytes(PDF_BYTES)
            archive = root / "archive"
            digest = local_pdf.sha256_file(source)
            target_dir = archive / "_imports" / digest
            target = target_dir / source.name
            real_fsync_directory = local_pdf.lib.fsync_directory
            failed = False

            def fail_after_final_replace(path):
                nonlocal failed
                if (not failed
                        and pathlib.Path(path).resolve() == target_dir.resolve()
                        and target.exists()):
                    failed = True
                    raise OSError("content parent fsync failed")
                return real_fsync_directory(path)

            with mock.patch.object(
                local_pdf.lib,
                "fsync_directory",
                side_effect=fail_after_final_replace,
            ):
                with self.assertRaisesRegex(
                    local_pdf.lib.ArchiveTransactionError, "耐久提交"
                ):
                    local_pdf._copy_content_addressed(
                        source, archive, digest
                    )

            self.assertTrue(failed)
            self.assertEqual(target.read_bytes(), PDF_BYTES)
            self.assertEqual(list(target_dir.glob(".*.tmp")), [])

    def test_local_import_lock_blocks_fetch_and_publisher_for_same_archive_date(self):
        context = multiprocessing.get_context("spawn")
        for kind in ("fetch", "publisher"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                archive = pathlib.Path(td) / "archive"
                archive.mkdir()
                ready = context.Event()
                entered = context.Event()
                rejected = context.Event()
                process = context.Process(
                    target=_shared_evidence_lock_worker,
                    args=(
                        kind, str(archive), "2026-09-01",
                        ready, entered, rejected,
                    ),
                )
                with local_pdf._issue_date_lock(archive, "2026-09-01"):
                    process.start()
                    self.assertTrue(ready.wait(timeout=5), "锁竞争进程未启动")
                    self.assertFalse(
                        entered.wait(timeout=0.3),
                        "本地导入锁持有期间共享证据锁不应被取得",
                    )
                self.assertTrue(entered.wait(timeout=5))
                process.join(timeout=5)
                self.assertFalse(process.is_alive(), "锁竞争进程超时")
                self.assertEqual(process.exitcode, 0)
                self.assertFalse(entered.is_set() and rejected.is_set())

    def test_source_change_before_ocr_uses_one_verified_snapshot_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            original_bytes = b"%PDF-1.4\nORIGINAL-EVIDENCE\n%%EOF"
            mutated_bytes = b"%PDF-1.4\nMUTATED-SOURCE\n%%EOF"
            pdf = root / "《中国建设报》2026-09-01_第9168期_电子报_高清.pdf"
            pdf.write_bytes(original_bytes)
            expected_digest = local_pdf.sha256_file(pdf)
            archive = root / "archive"
            observed = {}

            def fake_render(stable_pdf, output_dir, _archive, accurate=True):
                stable_pdf = pathlib.Path(stable_pdf)
                pdf.write_bytes(mutated_bytes)
                observed["path"] = stable_pdf
                observed["bytes"] = stable_pdf.read_bytes()
                output_dir = pathlib.Path(output_dir)
                (output_dir / "pages").mkdir(parents=True)
                (output_dir / "text").mkdir(parents=True)
                (output_dir / "pages" / "01版.jpg").write_bytes(b"jpeg")
                (output_dir / "text" / "edition_01.txt").write_text(
                    "中国建设报 2026年9月1日 第9168期 "
                    + stable_pdf.read_text(encoding="latin-1"),
                    encoding="utf-8",
                )
                return {
                    "page_count": 1,
                    "pages": [{
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 80,
                    }],
                }

            with mock.patch.object(local_pdf, "run_pdfocr", side_effect=fake_render):
                result = local_pdf.import_pdf(pdf, archive)

            archived_pdf = pathlib.Path(result["pdf_path"])
            ocr_text = (
                archive / "zgjsb" / "2026-09-01" / "text" / "edition_01.txt"
            ).read_text(encoding="utf-8")
            self.assertNotEqual(observed["path"], pdf.resolve())
            self.assertEqual(observed["bytes"], original_bytes)
            self.assertEqual(result["source_sha256"], expected_digest)
            self.assertEqual(archived_pdf.parent.name, expected_digest)
            self.assertEqual(archived_pdf.read_bytes(), original_bytes)
            self.assertIn("ORIGINAL-EVIDENCE", ocr_text)
            self.assertNotIn("MUTATED-SOURCE", ocr_text)
            self.assertFalse(list((archive / "_imports").glob(".source-snapshot-*")))

    def test_rejects_non_construction_source_before_any_archive_write(self):
        for source in ("../escaped", "/tmp/escaped", "rmrb"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                pdf = root / "《中国建设报》2026-09-01_第9168期_电子报_高清.pdf"
                pdf.write_bytes(PDF_BYTES)
                archive = root / "archive"
                with mock.patch.object(local_pdf, "run_pdfocr") as renderer:
                    with self.assertRaisesRegex(ValueError, "仅允许.*zgjsb"):
                        local_pdf.import_pdf(pdf, archive, source=source)
                renderer.assert_not_called()
                self.assertFalse(archive.exists())

    def test_existing_issue_is_preserved_and_only_linked_to_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            pdf = root / "《中国建设报》2026-09-01_第9168期_电子报_高清.pdf"
            pdf.write_bytes(PDF_BYTES)
            archive = root / "archive"
            issue_dir = archive / "zgjsb" / "2026-09-01"
            issue_dir.mkdir(parents=True)
            original_units = [{
                "id": "zgjsb_20260901_01",
                "title": "1版 要闻",
                "text_path": "text/edition_01.txt",
            }]
            issue = {
                "source": "zgjsb",
                "source_name": "中国建设报",
                "date": "2026-09-01",
                "issue_no": "9168",
                "editions": [{"no": 1, "name": "要闻"}],
                "units": original_units,
                "files": {"article_html": "kept.html"},
            }
            (issue_dir / "issue.json").write_text(
                json.dumps(issue, ensure_ascii=False), encoding="utf-8"
            )

            def fake_render(_pdf, output_dir, _archive, accurate=True):
                output_dir = pathlib.Path(output_dir)
                (output_dir / "pages").mkdir(parents=True)
                (output_dir / "text").mkdir(parents=True)
                (output_dir / "pages" / "01版.jpg").write_bytes(b"jpeg")
                (output_dir / "text" / "edition_01.txt").write_text(
                    "2026年9月1日 星期二 第9168期 今日8版", encoding="utf-8"
                )
                return {
                    "page_count": 1,
                    "pages": [{
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 40,
                    }],
                }

            with mock.patch.object(local_pdf, "run_pdfocr", side_effect=fake_render):
                result = local_pdf.import_pdf(pdf, archive, date="2026-09-01")
            saved = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))

            self.assertEqual(saved["units"], original_units)
            self.assertEqual(saved["files"]["article_html"], "kept.html")
            self.assertEqual(pathlib.Path(saved["files"]["local_pdf"]).read_bytes(), PDF_BYTES)
            self.assertEqual(result["page_count"], 1)
            self.assertEqual(saved["local_pdf_date_verification"], "verified")

    def test_header_date_conflict_rejects_before_target_issue_is_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            pdf = root / "《中国建设报》2026-09-03_第9170期_电子报_高清.pdf"
            pdf.write_bytes(PDF_BYTES)
            archive = root / "archive"

            def fake_render(_pdf, output_dir, _archive, accurate=True):
                output_dir = pathlib.Path(output_dir)
                (output_dir / "pages").mkdir(parents=True)
                (output_dir / "text").mkdir(parents=True)
                (output_dir / "pages" / "01版.jpg").write_bytes(b"jpeg")
                (output_dir / "text" / "edition_01.txt").write_text(
                    "中国建设报 2026年9月1日星期二 第9168期 今日8版",
                    encoding="utf-8",
                )
                return {
                    "page_count": 1,
                    "pages": [{
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 50,
                    }],
                }

            with mock.patch.object(local_pdf, "run_pdfocr", side_effect=fake_render):
                with self.assertRaisesRegex(ValueError, "报头日期.*2026-09-01.*归档日期.*2026-09-03"):
                    local_pdf.import_pdf(pdf, archive)

            self.assertFalse((archive / "zgjsb" / "2026-09-03" / "issue.json").exists())
            self.assertFalse(any((archive / "zgjsb").glob(".2026-09-03.import-*")))

    def test_unrecognized_header_date_is_review_only_and_not_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            pdf = root / "《中国建设报》2026-09-03_第9170期_电子报_高清.pdf"
            pdf.write_bytes(PDF_BYTES)
            archive = root / "archive"

            def fake_render(_pdf, output_dir, _archive, accurate=True):
                output_dir = pathlib.Path(output_dir)
                (output_dir / "pages").mkdir(parents=True)
                (output_dir / "text").mkdir(parents=True)
                (output_dir / "pages" / "01版.jpg").write_bytes(b"jpeg")
                (output_dir / "text" / "edition_01.txt").write_text(
                    "中国建设报 日期OCR不清 第9170期 今日1版 正文内容" * 3,
                    encoding="utf-8",
                )
                return {
                    "page_count": 1,
                    "pages": [{
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 90,
                    }],
                }

            with mock.patch.object(local_pdf, "run_pdfocr", side_effect=fake_render):
                result = local_pdf.import_pdf(pdf, archive)

            issue = json.loads(
                (archive / "zgjsb" / "2026-09-03" / "issue.json").read_text(encoding="utf-8")
            )
            state = json.loads(
                (archive / "_state" / "zgjsb" / "2026-09-03.json").read_text(encoding="utf-8")
            )
            self.assertTrue(result["needs_review"])
            self.assertEqual(issue["local_pdf_date_verification"], "unverified")
            self.assertNotIn("parsed", state["stages"])
            self.assertTrue(any("报头日期" in item for item in result["warnings"]))

    def test_new_issue_uses_render_manifest_and_marks_empty_page_for_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            pdf = root / "《中国建设报》2026-09-03_第9170期_电子报_高清.pdf"
            pdf.write_bytes(PDF_BYTES)
            archive = root / "archive"

            def fake_render(_pdf, output_dir, _archive, accurate=True):
                output_dir = pathlib.Path(output_dir)
                (output_dir / "pages").mkdir(parents=True)
                (output_dir / "text").mkdir(parents=True)
                (output_dir / "pages" / "01版.jpg").write_bytes(b"jpeg")
                (output_dir / "pages" / "02版.jpg").write_bytes(b"jpeg")
                (output_dir / "text" / "edition_01.txt").write_text(
                    "2026年9月3日 第9170期 今日2版 要闻正文" * 4, encoding="utf-8"
                )
                (output_dir / "text" / "edition_02.txt").write_text("", encoding="utf-8")
                return {
                    "page_count": 2,
                    "pages": [
                        {"number": 1, "image": "pages/01版.jpg", "text": "text/edition_01.txt", "characters": 92},
                        {"number": 2, "image": "pages/02版.jpg", "text": "text/edition_02.txt", "characters": 0},
                    ],
                }

            with mock.patch.object(local_pdf, "run_pdfocr", side_effect=fake_render):
                result = local_pdf.import_pdf(pdf, archive)

            issue = json.loads(
                (archive / "zgjsb" / "2026-09-03" / "issue.json").read_text(encoding="utf-8")
            )
            state = json.loads(
                (archive / "_state" / "zgjsb" / "2026-09-03.json").read_text(encoding="utf-8")
            )
            self.assertEqual(issue["issue_no"], "9170")
            self.assertEqual(len(issue["units"]), 2)
            self.assertNotIn("parsed", state.get("stages", {}))
            self.assertTrue(result["needs_review"])
            self.assertTrue(any("第2版" in item for item in result["warnings"]))

    def test_import_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            pdf = root / "《中国建设报》2026-09-01_第9168期_电子报_高清.pdf"
            pdf.write_bytes(PDF_BYTES)
            archive = root / "archive"
            issue_dir = archive / "zgjsb" / "2026-09-01"
            issue_dir.mkdir(parents=True)
            (issue_dir / "issue.json").write_text(json.dumps({
                "source": "zgjsb", "source_name": "中国建设报",
                "date": "2026-09-01", "issue_no": "9168",
                "editions": [{"no": 1, "name": "要闻"}],
                "units": [{
                    "id": "zgjsb_20260901_01",
                    "title": "1版 要闻",
                    "text_path": "text/edition_01.txt",
                }],
                "files": {},
            }), encoding="utf-8")

            def fake_render(_pdf, output_dir, _archive, accurate=True):
                output_dir = pathlib.Path(output_dir)
                (output_dir / "pages").mkdir(parents=True)
                (output_dir / "text").mkdir(parents=True)
                (output_dir / "pages" / "01版.jpg").write_bytes(b"jpeg")
                (output_dir / "text" / "edition_01.txt").write_text(
                    "2026年9月1日 星期二 第9168期 今日8版", encoding="utf-8"
                )
                return {
                    "page_count": 1,
                    "pages": [{
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 40,
                    }],
                }

            with mock.patch.object(local_pdf, "run_pdfocr", side_effect=fake_render):
                first = local_pdf.import_pdf(pdf, archive)
                second = local_pdf.import_pdf(pdf, archive)

            self.assertEqual(first["source_sha256"], second["source_sha256"])
            self.assertEqual(first["pdf_path"], second["pdf_path"])
            copies = list((archive / "_imports" / first["source_sha256"]).glob("*.pdf"))
            self.assertEqual(len(copies), 1)


if __name__ == "__main__":
    unittest.main()
