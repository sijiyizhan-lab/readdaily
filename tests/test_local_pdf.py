import json
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

    def test_rejects_non_pdf_magic_even_with_pdf_extension(self):
        with tempfile.TemporaryDirectory() as td:
            fake = pathlib.Path(td) / "fake.pdf"
            fake.write_bytes(b"not really a pdf")
            with self.assertRaisesRegex(ValueError, "PDF"):
                local_pdf.validate_pdf(fake)


class LocalPDFImportTests(unittest.TestCase):
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

            result = local_pdf.import_pdf(pdf, archive, date="2026-09-01")
            saved = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))

            self.assertEqual(saved["units"], original_units)
            self.assertEqual(saved["files"]["article_html"], "kept.html")
            self.assertEqual(pathlib.Path(saved["files"]["local_pdf"]).read_bytes(), PDF_BYTES)
            self.assertEqual(result["page_count"], 1)

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
                "editions": [], "units": [], "files": {},
            }), encoding="utf-8")

            first = local_pdf.import_pdf(pdf, archive)
            second = local_pdf.import_pdf(pdf, archive)

            self.assertEqual(first["source_sha256"], second["source_sha256"])
            self.assertEqual(first["pdf_path"], second["pdf_path"])
            copies = list((archive / "_imports" / first["source_sha256"]).glob("*.pdf"))
            self.assertEqual(len(copies), 1)


if __name__ == "__main__":
    unittest.main()
