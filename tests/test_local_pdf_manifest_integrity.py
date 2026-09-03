import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LOCAL_PDF_PATH = (
    ROOT / "skills" / "newspaper-fetch" / "scripts" / "local_pdf.py"
)
SWIFT_HELPER_PATH = ROOT / "scripts" / "pdfocr.swift"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


local_pdf = load_module("local_pdf_manifest_integrity_test", LOCAL_PDF_PATH)


class PDFOCRSwiftSourceTests(unittest.TestCase):
    def test_missing_pdfkit_page_throws_instead_of_silently_continuing(self):
        source = SWIFT_HELPER_PATH.read_text(encoding="utf-8")

        self.assertNotRegex(
            source,
            r"guard\s+let\s+page\s*=\s*document\.page\(at:\s*index\)\s+else\s*\{\s*continue\s*\}",
        )
        self.assertRegex(
            source,
            r"guard\s+let\s+page\s*=\s*document\.page\(at:\s*index\)\s+else\s*\{\s*throw\s+PDFOCRError\.cannotRender\(number\)\s*\}",
        )


class LocalPDFManifestIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.archive = self.base / "archive"
        self.pdf = self.base / "《中国建设报》2026-09-03_第9170期_电子报_高清.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nmanifest-integrity-fixture\n%%EOF")
        self.target = self.archive / "zgjsb" / "2026-09-03"

    def tearDown(self):
        self.temporary.cleanup()

    def write_page_files(self, output_dir, number, image=None, text=None):
        output_dir = Path(output_dir)
        image_path = output_dir / (image or f"pages/{number:02d}版.jpg")
        text_path = output_dir / (text or f"text/edition_{number:02d}.txt")
        image_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes((f"page-{number}").encode("utf-8"))
        text_path.write_text(
            "中国建设报 2026年9月3日 星期四 第9170期 今日2版 正文",
            encoding="utf-8",
        )

    def assert_no_target_artifacts(self):
        self.assertFalse((self.target / "issue.json").exists())
        self.assertFalse((self.target / "pages").exists())
        source_dir = self.archive / "zgjsb"
        if source_dir.exists():
            self.assertFalse(any(source_dir.glob(".2026-09-03.import-*")))

    def archive_snapshot(self):
        if not self.archive.exists():
            return []
        return [
            (path.relative_to(self.archive).as_posix(), path.read_bytes())
            for path in sorted(self.archive.rglob("*"))
            if path.is_file() and not path.is_symlink()
        ]

    def write_existing_issue(self, issue_no="9170", page_count=1):
        self.target.mkdir(parents=True)
        (self.target / "pages").mkdir()
        (self.target / "text").mkdir()
        editions = []
        units = []
        for number in range(1, page_count + 1):
            image = f"pages/{number:02d}版.jpg"
            text = f"text/edition_{number:02d}.txt"
            (self.target / image).write_bytes(f"existing-page-{number}".encode())
            (self.target / text).write_text(
                f"旧归档第{number}版正文，不得被新 PDF 证据覆盖。",
                encoding="utf-8",
            )
            editions.append({"no": number, "name": "要闻", "page_image": image})
            units.append({
                "id": f"zgjsb_20260903_{number:02d}",
                "title": f"{number}版 要闻",
                "page_image": image,
                "text_path": text,
            })
        issue = {
            "source": "zgjsb",
            "source_name": "中国建设报",
            "date": "2026-09-03",
            "issue_no": issue_no,
            "editions": editions,
            "units": units,
            "files": {"article_html": "kept.html"},
        }
        (self.target / "issue.json").write_text(
            json.dumps(issue, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def one_page_manifest(self, _pdf, output_dir, _archive, accurate=True):
        self.write_page_files(output_dir, 1)
        return {
            "page_count": 1,
            "pages": [{
                "number": 1,
                "image": "pages/01版.jpg",
                "text": "text/edition_01.txt",
                "characters": 50,
            }],
        }

    def test_existing_issue_number_conflict_rejects_without_changing_archive_bytes(self):
        self.write_existing_issue(issue_no="9169", page_count=1)
        before = self.archive_snapshot()

        with mock.patch.object(
            local_pdf, "run_pdfocr", side_effect=self.one_page_manifest
        ):
            with self.assertRaisesRegex(ValueError, "既有归档期号.*9169.*导入.*9170.*不一致"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assertEqual(self.archive_snapshot(), before)
        self.assertFalse(any(
            (self.archive / "zgjsb").glob(".2026-09-03.import-*")
        ))

    def test_existing_edition_count_conflict_rejects_without_changing_archive_bytes(self):
        self.write_existing_issue(issue_no="9170", page_count=2)
        issue_path = self.target / "issue.json"
        issue = json.loads(issue_path.read_text(encoding="utf-8"))
        issue["units"] = issue["units"][:1]
        issue_path.write_text(
            json.dumps(issue, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        before = self.archive_snapshot()

        with mock.patch.object(
            local_pdf, "run_pdfocr", side_effect=self.one_page_manifest
        ):
            with self.assertRaisesRegex(ValueError, "PDF.*1.*editions.*2.*不一致"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assertEqual(self.archive_snapshot(), before)
        self.assertFalse(any(
            (self.archive / "zgjsb").glob(".2026-09-03.import-*")
        ))

    def test_existing_unit_count_conflict_rejects_without_changing_archive_bytes(self):
        self.write_existing_issue(issue_no="9170", page_count=2)
        issue_path = self.target / "issue.json"
        issue = json.loads(issue_path.read_text(encoding="utf-8"))
        issue["editions"] = issue["editions"][:1]
        issue_path.write_text(
            json.dumps(issue, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        before = self.archive_snapshot()

        with mock.patch.object(
            local_pdf, "run_pdfocr", side_effect=self.one_page_manifest
        ):
            with self.assertRaisesRegex(ValueError, "PDF.*1.*units.*2.*不一致"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assertEqual(self.archive_snapshot(), before)
        self.assertFalse(any(
            (self.archive / "zgjsb").glob(".2026-09-03.import-*")
        ))

    def test_explicit_empty_existing_page_lists_reject_pdf_binding(self):
        self.write_existing_issue(issue_no="9170", page_count=1)
        issue_path = self.target / "issue.json"
        issue = json.loads(issue_path.read_text(encoding="utf-8"))
        issue["editions"] = []
        issue["units"] = []
        issue_path.write_text(
            json.dumps(issue, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        before = self.archive_snapshot()

        with mock.patch.object(
            local_pdf, "run_pdfocr", side_effect=self.one_page_manifest
        ):
            with self.assertRaisesRegex(ValueError, "PDF.*1.*editions.*0.*不一致"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assertEqual(self.archive_snapshot(), before)

    def test_import_rejects_partial_manifest_before_target_write(self):
        def partial_manifest(_pdf, output_dir, _archive, accurate=True):
            self.write_page_files(output_dir, 1)
            return {
                "page_count": 2,
                "pages": [{
                    "number": 1,
                    "image": "pages/01版.jpg",
                    "text": "text/edition_01.txt",
                    "characters": 50,
                }],
            }

        with mock.patch.object(local_pdf, "run_pdfocr", side_effect=partial_manifest):
            with self.assertRaisesRegex(RuntimeError, "page_count|页面数量"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assert_no_target_artifacts()

    def test_import_rejects_duplicate_or_non_contiguous_page_numbers(self):
        def duplicate_manifest(_pdf, output_dir, _archive, accurate=True):
            self.write_page_files(output_dir, 1)
            self.write_page_files(output_dir, 2)
            return {
                "page_count": 2,
                "pages": [
                    {
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 50,
                    },
                    {
                        "number": 1,
                        "image": "pages/02版.jpg",
                        "text": "text/edition_02.txt",
                        "characters": 50,
                    },
                ],
            }

        with mock.patch.object(local_pdf, "run_pdfocr", side_effect=duplicate_manifest):
            with self.assertRaisesRegex(RuntimeError, "编号"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assert_no_target_artifacts()

    def test_import_rejects_out_of_order_page_numbers(self):
        def out_of_order_manifest(_pdf, output_dir, _archive, accurate=True):
            self.write_page_files(output_dir, 1)
            self.write_page_files(output_dir, 2)
            return {
                "page_count": 2,
                "pages": [
                    {
                        "number": 2,
                        "image": "pages/02版.jpg",
                        "text": "text/edition_02.txt",
                        "characters": 50,
                    },
                    {
                        "number": 1,
                        "image": "pages/01版.jpg",
                        "text": "text/edition_01.txt",
                        "characters": 50,
                    },
                ],
            }

        with mock.patch.object(local_pdf, "run_pdfocr", side_effect=out_of_order_manifest):
            with self.assertRaisesRegex(RuntimeError, "编号"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assert_no_target_artifacts()

    def test_import_rejects_traversal_paths_before_target_write(self):
        def traversal_manifest(_pdf, output_dir, _archive, accurate=True):
            output_dir = Path(output_dir)
            outside_image = output_dir.parent / "outside.jpg"
            outside_text = output_dir.parent / "outside.txt"
            outside_image.write_bytes(b"outside")
            outside_text.write_text("outside", encoding="utf-8")
            return {
                "page_count": 1,
                "pages": [{
                    "number": 1,
                    "image": "../outside.jpg",
                    "text": "../outside.txt",
                    "characters": 7,
                }],
            }

        with mock.patch.object(local_pdf, "run_pdfocr", side_effect=traversal_manifest):
            with self.assertRaisesRegex(RuntimeError, "安全相对路径|越界"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assert_no_target_artifacts()

    def test_import_rejects_manifest_whose_referenced_file_is_missing(self):
        def missing_file_manifest(_pdf, output_dir, _archive, accurate=True):
            output_dir = Path(output_dir)
            text_path = output_dir / "text" / "edition_01.txt"
            text_path.parent.mkdir(parents=True)
            text_path.write_text(
                "中国建设报 2026年9月3日 星期四 第9170期 今日1版 正文",
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

        with mock.patch.object(local_pdf, "run_pdfocr", side_effect=missing_file_manifest):
            with self.assertRaisesRegex(RuntimeError, "普通文件|不存在"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assert_no_target_artifacts()

    def test_import_rejects_missing_manifest_fields_before_target_write(self):
        with mock.patch.object(local_pdf, "run_pdfocr", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "页面清单|page_count"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assert_no_target_artifacts()

    def test_invalid_manifest_does_not_modify_an_existing_target(self):
        self.target.mkdir(parents=True)
        issue_path = self.target / "issue.json"
        issue_bytes = json.dumps({
            "source": "zgjsb",
            "date": "2026-09-03",
            "issue_no": "existing",
            "editions": [{"no": 1, "name": "要闻"}],
            "units": [],
            "files": {},
        }, ensure_ascii=False).encode("utf-8")
        issue_path.write_bytes(issue_bytes)
        page_path = self.target / "pages" / "01版.jpg"
        page_path.parent.mkdir()
        page_path.write_bytes(b"existing-page")

        with mock.patch.object(local_pdf, "run_pdfocr", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "页面清单|page_count"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assertEqual(issue_path.read_bytes(), issue_bytes)
        self.assertEqual(page_path.read_bytes(), b"existing-page")
        self.assertFalse(any(
            (self.archive / "zgjsb").glob(".2026-09-03.import-*")
        ))

    def test_import_rejects_symlinked_page_instead_of_accepting_it_as_a_file(self):
        def symlink_manifest(_pdf, output_dir, _archive, accurate=True):
            output_dir = Path(output_dir)
            outside = output_dir.parent / "outside.jpg"
            outside.write_bytes(b"outside")
            image = output_dir / "pages" / "01版.jpg"
            image.parent.mkdir(parents=True)
            image.symlink_to(outside)
            text = output_dir / "text" / "edition_01.txt"
            text.parent.mkdir(parents=True)
            text.write_text(
                "中国建设报 2026年9月3日 星期四 第9170期 今日1版 正文",
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

        with mock.patch.object(local_pdf, "run_pdfocr", side_effect=symlink_manifest):
            with self.assertRaisesRegex(RuntimeError, "普通文件|符号链接"):
                local_pdf.import_pdf(self.pdf, self.archive)

        self.assert_no_target_artifacts()

    def test_run_pdfocr_validates_manifest_before_returning_to_importer(self):
        output_dir = self.base / "helper-output"
        self.write_page_files(output_dir, 1)
        manifest = {
            "page_count": 2,
            "pages": [{
                "number": 1,
                "image": "pages/01版.jpg",
                "text": "text/edition_01.txt",
                "characters": 50,
            }],
        }
        completed = subprocess.CompletedProcess(
            args=["pdfocr"], returncode=0, stdout=json.dumps(manifest), stderr=""
        )

        with mock.patch.object(local_pdf, "_helper_binary", return_value=Path("/pdfocr")), \
                mock.patch.object(local_pdf.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "page_count|页面数量"):
                local_pdf.run_pdfocr(
                    self.pdf, output_dir, self.archive, accurate=True
                )


if __name__ == "__main__":
    unittest.main()
