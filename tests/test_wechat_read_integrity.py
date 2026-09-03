import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(FETCH_SCRIPTS))

from tests.image_fixtures import page_png


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


wechat_engine = load_module(
    "wechat_engine_integrity_test", FETCH_SCRIPTS / "wechat_engine.py"
)
wechat_read = load_module(
    "wechat_read_integrity_test", FETCH_SCRIPTS / "adapters" / "wechat_read.py"
)


def page_jpeg(width=1280, height=1823, fill=b"A", complete=True):
    """Build a real page image, or an intentionally truncated JPEG."""
    if complete:
        return page_png(
            width=width,
            height=height,
            min_bytes=4096,
            fill=fill,
        )
    if len(fill) != 1 or fill == b"\xff":
        raise ValueError("fill must be one non-0xff byte")
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    sos = b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    return b"\xff\xd8" + app0 + sof0 + sos + (fill * 2048)


class WechatGuideIntegrityTests(unittest.TestCase):
    def test_guide_parser_rejects_partial_page_image_set_instead_of_truncating_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            html_path = Path(temporary) / "article.html"
            html_path.write_text(
                """
                <section>各版导读
                  <span leaf="">1版</span><span leaf="">要闻</span>
                  <span leaf="">2版</span><span leaf="">综合新闻</span>
                  <span leaf="">3版</span><span leaf="">城市更新</span>
                  <img src="assets/page-01.jpg">
                  <img src="assets/page-03.jpg">
                  左右滑动
                </section>
                """,
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                wechat_engine.parse_guide_and_pages(html_path)

        self.assertIn("rows=3", str(caught.exception))
        self.assertIn("imgs=2", str(caught.exception))

    def test_guide_parser_rejects_extra_page_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            html_path = Path(temporary) / "article.html"
            html_path.write_text(
                """
                <section>各版导读
                  <span leaf="">1版</span><span leaf="">要闻</span>
                  <img src="assets/page-01.jpg">
                  <img src="assets/page-02.jpg">
                  左右滑动
                </section>
                """,
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                wechat_engine.parse_guide_and_pages(html_path)

        self.assertIn("rows=1", str(caught.exception))
        self.assertIn("imgs=2", str(caught.exception))

    def test_wechat_page_gate_remains_stricter_than_common_kjrb_size(self):
        with self.assertRaisesRegex(ValueError, "尺寸过小"):
            wechat_read._validated_page_image(
                page_png(width=1000, height=1417, min_bytes=60000)
            )

    def test_guide_parser_rejects_unequal_counts_when_one_side_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            html_path = Path(temporary) / "article.html"
            html_path.write_text(
                """
                <section>各版导读
                  <span leaf="">1版</span><span leaf="">要闻</span>
                  左右滑动
                </section>
                """,
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                wechat_engine.parse_guide_and_pages(html_path)

        self.assertIn("rows=1", str(caught.exception))
        self.assertIn("imgs=0", str(caught.exception))


class WechatReadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.out = self.base / "wechat"
        self.archive = self.base / "archive"
        self.account_dir = self.out / "中国建设报"
        self.account_dir.mkdir(parents=True)
        self.day = datetime.date(2026, 9, 3)
        self.source = {
            "id": "zgjsb",
            "name": "中国建设报",
            "channel": "wechat_read",
            "out": str(self.out),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def article_html(self):
        path = self.account_dir / "2026-09-03_读报_原文.html"
        path.write_text("<html>fixture</html>", encoding="utf-8")
        return path

    def engine(self, html_path, rows, page_srcs, **artifact_overrides):
        artifacts = {
            "images_complete": True,
            "publish_date": self.day.isoformat(),
            "html": html_path.name,
        }
        artifacts.update(artifact_overrides)
        return types.SimpleNamespace(
            already_done=mock.Mock(return_value=True),
            verify_artifacts=mock.Mock(return_value=artifacts),
            find_article_html=mock.Mock(return_value=str(html_path)),
            parse_guide_and_pages=mock.Mock(return_value=(rows, page_srcs)),
        )

    def target_issue_dir(self):
        return self.archive / "zgjsb" / self.day.isoformat()

    def write_parse_issue(self):
        issue_dir = self.target_issue_dir()
        pages = issue_dir / "pages"
        text = issue_dir / "text"
        pages.mkdir(parents=True)
        text.mkdir()
        (pages / "01版_要闻.jpg").write_bytes(page_jpeg(fill=b"A"))
        (pages / "02版_综合.jpg").write_bytes(page_jpeg(fill=b"B"))
        issue = {
            "source": "zgjsb",
            "source_name": "中国建设报",
            "date": self.day.isoformat(),
            "channel": "wechat_read",
            "editions": [
                {"no": 1, "name": "要闻", "page_image": "pages/01版_要闻.jpg"},
                {"no": 2, "name": "综合", "page_image": "pages/02版_综合.jpg"},
            ],
            "units": [
                {
                    "id": "zgjsb_20260903_01",
                    "type": "edition_ocr",
                    "title": "1版 要闻",
                    "page_image": "pages/01版_要闻.jpg",
                },
                {
                    "id": "zgjsb_20260903_02",
                    "type": "edition_ocr",
                    "title": "2版 综合",
                    "page_image": "pages/02版_综合.jpg",
                },
            ],
        }
        issue_path = issue_dir / "issue.json"
        original = json.dumps(issue, ensure_ascii=False, indent=2).encode("utf-8")
        issue_path.write_bytes(original)
        return issue_path, original, text

    def write_bound_ocr(self, edition, text):
        issue_dir = self.target_issue_dir()
        issue = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        unit = issue["units"][edition - 1]
        page_path = issue_dir / unit["page_image"]
        filename = f"edition_{edition:02d}.txt"
        relative_text = f"text/{filename}"
        text_path = issue_dir / relative_text
        text_path.write_text(text, encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "source": self.source["id"],
            "date": self.day.isoformat(),
            "unit_id": unit["id"],
            "page_image": unit["page_image"],
            "page_image_sha256": hashlib.sha256(page_path.read_bytes()).hexdigest(),
            "text_path": relative_text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        manifest_path = issue_dir / "text" / f"edition_{edition:02d}.ocr.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        unit["page_image_sha256"] = manifest["page_image_sha256"]
        unit["text_path"] = relative_text
        unit["ocr_manifest_path"] = (
            f"text/edition_{edition:02d}.ocr.json"
        )
        (issue_dir / "issue.json").write_text(
            json.dumps(issue, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return text_path, manifest_path

    def test_acquire_rejects_existing_artifacts_unless_images_complete_is_true(self):
        for value in (False, None, 1):
            with self.subTest(images_complete=value):
                engine = types.SimpleNamespace(
                    already_done=mock.Mock(return_value=True),
                    verify_artifacts=mock.Mock(return_value={
                        "images_complete": value,
                        "publish_date": self.day.isoformat(),
                    }),
                )
                with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                        mock.patch.object(wechat_read.subprocess, "run") as run:
                    ok, note = wechat_read.acquire(
                        self.source, self.day, self.archive, offline_ok=True
                    )

                self.assertFalse(ok)
                self.assertIn("images_complete", note)
                engine.verify_artifacts.assert_called_once_with(str(self.out), self.day)
                run.assert_not_called()

    def test_acquire_rechecks_images_complete_after_a_new_download(self):
        engine = types.SimpleNamespace(
            already_done=mock.Mock(side_effect=[False, True]),
            verify_artifacts=mock.Mock(return_value={
                "images_complete": False,
                "publish_date": self.day.isoformat(),
            }),
        )
        completed = subprocess.CompletedProcess(
            args=["wechat_engine.py"], returncode=0, stdout="完成", stderr=""
        )
        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read.subprocess, "run", return_value=completed):
            ok, note = wechat_read.acquire(
                self.source, self.day, self.archive, offline_ok=False
            )

        self.assertFalse(ok)
        self.assertIn("images_complete", note)
        engine.verify_artifacts.assert_called_once_with(str(self.out), self.day)

    def test_fetch_rejects_parser_count_mismatch_without_creating_issue_or_pages(self):
        html_path = self.article_html()
        engine = self.engine(
            html_path,
            [(1, "要闻"), (2, "综合新闻")],
            ["assets/page-01.jpg"],
        )

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine):
            issue, error = wechat_read.fetch(self.source, self.day, self.archive)

        self.assertIsNone(issue)
        self.assertIn("rows=2", error)
        self.assertIn("imgs=1", error)
        self.assertFalse((self.target_issue_dir() / "issue.json").exists())
        self.assertFalse((self.target_issue_dir() / "pages").exists())

    def test_fetch_rejects_non_contiguous_real_edition_numbers_without_writing(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(page_jpeg(fill=b"A"))
        (assets / "page-02.jpg").write_bytes(page_jpeg(fill=b"B"))
        engine = self.engine(
            html_path,
            [(1, "要闻"), (3, "城市更新")],
            ["assets/page-01.jpg", "assets/page-02.jpg"],
        )

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value="9170") as ocr:
            issue, error = wechat_read.fetch(
                self.source, self.day, self.archive
            )

        self.assertIsNone(issue)
        self.assertIsNotNone(error)
        self.assertIn("版号", error)
        ocr.assert_not_called()
        self.assertFalse((self.target_issue_dir() / "issue.json").exists())
        self.assertFalse((self.target_issue_dir() / "pages").exists())

    def test_parse_preserves_full_ocr_output_beyond_legacy_article_limit(self):
        _issue_path, _original, text_dir = self.write_parse_issue()
        tail = "版面 OCR 末尾不可丢失"
        full_text = "建设行业长文。" * 5000 + tail

        with mock.patch.object(
            wechat_read, "ocr_image", side_effect=[full_text, full_text]
        ):
            parsed, error = wechat_read.parse(
                self.source, self.day, self.archive
            )

        self.assertIsNone(error)
        self.assertEqual(
            (text_dir / "edition_01.txt").read_text(encoding="utf-8"),
            full_text,
        )
        self.assertTrue(parsed["units"][0]["text_path"].endswith("edition_01.txt"))

    def test_fetch_buffers_every_page_before_archive_and_missing_page_leaves_no_output(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(page_jpeg(fill=b"A"))
        engine = self.engine(
            html_path,
            [(1, "要闻"), (2, "综合新闻")],
            ["assets/page-01.jpg", "assets/page-02.jpg"],
        )

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value=None):
            issue, error = wechat_read.fetch(self.source, self.day, self.archive)

        self.assertIsNone(issue)
        self.assertIn("缺图", error)
        self.assertFalse((self.target_issue_dir() / "issue.json").exists())
        self.assertFalse((self.target_issue_dir() / "pages").exists())

    def test_fetch_rejects_wrong_metadata_date_before_writing_archive(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(page_jpeg(fill=b"A"))
        engine = self.engine(
            html_path,
            [(1, "要闻")],
            ["assets/page-01.jpg"],
            publish_date="2026-09-02",
        )

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value=None):
            issue, error = wechat_read.fetch(self.source, self.day, self.archive)

        self.assertIsNone(issue)
        self.assertIn("2026-09-02", error)
        self.assertFalse((self.target_issue_dir() / "issue.json").exists())
        self.assertFalse((self.target_issue_dir() / "pages").exists())

    def test_fetch_commits_one_complete_issue_after_all_pages_are_buffered(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        first_page = page_jpeg(fill=b"A")
        second_page = page_jpeg(fill=b"B")
        (assets / "page-01.jpg").write_bytes(first_page)
        (assets / "page-02.jpg").write_bytes(second_page)
        engine = self.engine(
            html_path,
            [(1, "要闻"), (2, "综合新闻")],
            ["assets/page-01.jpg", "assets/page-02.jpg"],
        )

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value="9170"):
            issue, error = wechat_read.fetch(self.source, self.day, self.archive)

        self.assertIsNone(error)
        self.assertEqual(issue["source"], "zgjsb")
        self.assertEqual(issue["date"], self.day.isoformat())
        self.assertEqual(issue["issue_no"], "9170")
        saved = json.loads(
            (self.target_issue_dir() / "issue.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(saved["editions"]), 2)
        self.assertEqual(
            (self.target_issue_dir() / "pages" / "01版_要闻.jpg").read_bytes(),
            first_page,
        )
        self.assertEqual(
            (self.target_issue_dir() / "pages" / "02版_综合新闻.jpg").read_bytes(),
            second_page,
        )
        self.assertEqual(
            [path.name for path in (self.archive / "zgjsb").iterdir()],
            [self.day.isoformat()],
        )

    def test_fetch_creates_source_archive_through_durable_helper(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(page_jpeg(fill=b"A"))
        engine = self.engine(
            html_path, [(1, "要闻")], ["assets/page-01.jpg"]
        )
        calls = []
        real_durable_makedirs = wechat_read.lib.durable_makedirs

        def observe(path, *args, **kwargs):
            calls.append(Path(path))
            return real_durable_makedirs(path, *args, **kwargs)

        with mock.patch.object(
            wechat_read, "_load_engine", return_value=engine
        ), mock.patch.object(
            wechat_read, "ocr_issue", return_value="9170"
        ), mock.patch.object(
            wechat_read.lib, "durable_makedirs", side_effect=observe
        ):
            issue, error = wechat_read.fetch(
                self.source, self.day, self.archive
            )

        self.assertIsNone(error)
        self.assertIsNotNone(issue)
        self.assertIn(self.archive / "zgjsb", calls)

    def test_fetch_does_not_report_success_when_source_parent_fsync_fails(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(page_jpeg(fill=b"A"))
        engine = self.engine(
            html_path, [(1, "要闻")], ["assets/page-01.jpg"]
        )
        real_fsync_directory = wechat_read.lib.fsync_directory
        failed = False

        def fail_after_source_directory_creation(path):
            nonlocal failed
            if (not failed
                    and Path(path).resolve() == self.archive.resolve()
                    and (self.archive / "zgjsb").is_dir()):
                failed = True
                raise OSError("source parent fsync failed")
            return real_fsync_directory(path)

        with mock.patch.object(
            wechat_read, "_load_engine", return_value=engine
        ), mock.patch.object(
            wechat_read, "ocr_issue", return_value="9170"
        ), mock.patch.object(
            wechat_read.lib,
            "fsync_directory",
            side_effect=fail_after_source_directory_creation,
        ):
            with self.assertRaisesRegex(
                OSError, "source parent fsync failed"
            ):
                wechat_read.fetch(self.source, self.day, self.archive)

        self.assertTrue(failed)
        self.assertFalse((self.target_issue_dir() / "issue.json").exists())
        self.assertEqual(
            list((self.archive / "zgjsb").glob(".*")), []
        )

    def test_fetch_rejects_one_byte_new_page_and_preserves_old_ocr_issue(self):
        issue_path, _original, _text_dir = self.write_parse_issue()
        old_text_path, old_manifest_path = self.write_bound_ocr(1, "旧 OCR" * 100)
        old_issue = issue_path.read_bytes()
        old_text = old_text_path.read_bytes()
        old_manifest = old_manifest_path.read_bytes()
        old_page = (
            self.target_issue_dir() / "pages" / "01版_要闻.jpg"
        ).read_bytes()

        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(b"x")
        engine = self.engine(html_path, [(1, "要闻")], ["assets/page-01.jpg"])

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value="9170"):
            issue, error = wechat_read.fetch(self.source, self.day, self.archive)

        self.assertIsNone(issue)
        self.assertIn("图片", error)
        self.assertEqual(issue_path.read_bytes(), old_issue)
        self.assertEqual(old_text_path.read_bytes(), old_text)
        self.assertEqual(old_manifest_path.read_bytes(), old_manifest)
        self.assertEqual(
            (self.target_issue_dir() / "pages" / "01版_要闻.jpg").read_bytes(),
            old_page,
        )

    def test_fetch_rejects_truncated_jpeg_before_creating_archive(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(
            page_jpeg(fill=b"T", complete=False)
        )
        engine = self.engine(html_path, [(1, "要闻")], ["assets/page-01.jpg"])

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value="9170"):
            issue, error = wechat_read.fetch(self.source, self.day, self.archive)

        self.assertIsNone(issue)
        self.assertIn("图片", error)
        self.assertFalse(self.target_issue_dir().exists())

    def test_fetch_rejects_large_html_waf_body_disguised_as_page_image(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        waf_body = (
            b"<!doctype html><html><title>Access Denied</title><body>"
            + (b"request blocked by waf " * 300)
            + b"</body></html>"
        )
        self.assertGreater(len(waf_body), 1024)
        (assets / "page-01.jpg").write_bytes(waf_body)
        engine = self.engine(html_path, [(1, "要闻")], ["assets/page-01.jpg"])

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value="9170"):
            issue, error = wechat_read.fetch(self.source, self.day, self.archive)

        self.assertIsNone(issue)
        self.assertIn("图片", error)
        self.assertFalse(self.target_issue_dir().exists())

    def test_fetch_rejects_image_magic_with_thumbnail_dimensions(self):
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(
            page_jpeg(width=320, height=480, fill=b"S")
        )
        engine = self.engine(html_path, [(1, "要闻")], ["assets/page-01.jpg"])

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value="9170"):
            issue, error = wechat_read.fetch(self.source, self.day, self.archive)

        self.assertIsNone(issue)
        self.assertIn("尺寸过小", error)
        self.assertFalse(self.target_issue_dir().exists())

    def test_fetch_new_valid_pages_discards_old_ocr_before_next_parse(self):
        _issue_path, _original, _text_dir = self.write_parse_issue()
        self.write_bound_ocr(1, "旧 OCR" * 100)
        self.write_bound_ocr(2, "旧 OCR" * 100)
        html_path = self.article_html()
        assets = self.account_dir / "assets"
        assets.mkdir()
        (assets / "page-01.jpg").write_bytes(page_jpeg(fill=b"C"))
        (assets / "page-02.jpg").write_bytes(page_jpeg(fill=b"D"))
        engine = self.engine(
            html_path,
            [(1, "要闻"), (2, "综合")],
            ["assets/page-01.jpg", "assets/page-02.jpg"],
        )

        with mock.patch.object(wechat_read, "_load_engine", return_value=engine), \
                mock.patch.object(wechat_read, "ocr_issue", return_value="9170"):
            fetched, fetch_error = wechat_read.fetch(
                self.source, self.day, self.archive
            )

        self.assertIsNone(fetch_error)
        self.assertEqual(list((self.target_issue_dir() / "text").iterdir()), [])
        with mock.patch.object(
            wechat_read, "ocr_image", side_effect=["新 OCR" * 100, "新 OCR" * 100]
        ) as ocr:
            parsed, parse_error = wechat_read.parse(
                self.source, self.day, self.archive
            )
        self.assertIsNone(parse_error)
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(parsed["units"][0]["page_image_sha256"], hashlib.sha256(
            (self.target_issue_dir() / "pages" / "01版_要闻.jpg").read_bytes()
        ).hexdigest())

    def test_parse_hash_mismatch_reruns_ocr_and_rebinds_sidecar(self):
        _issue_path, _original, _text_dir = self.write_parse_issue()
        stale_text_path, stale_manifest_path = self.write_bound_ocr(
            1, "旧 OCR" * 100
        )
        self.write_bound_ocr(2, "第二版旧 OCR" * 80)
        stale_manifest = json.loads(stale_manifest_path.read_text(encoding="utf-8"))
        stale_manifest["page_image_sha256"] = "0" * 64
        stale_manifest_path.write_text(
            json.dumps(stale_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with mock.patch.object(
            wechat_read,
            "ocr_image",
            side_effect=["第一版新 OCR" * 80],
        ) as ocr:
            parsed, error = wechat_read.parse(
                self.source, self.day, self.archive
            )

        self.assertIsNone(error)
        ocr.assert_called_once()
        self.assertEqual(stale_text_path.read_text(encoding="utf-8"), "第一版新 OCR" * 80)
        rebound = json.loads(stale_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            rebound["page_image_sha256"],
            hashlib.sha256(
                (self.target_issue_dir() / "pages" / "01版_要闻.jpg").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            rebound["text_sha256"],
            hashlib.sha256(("第一版新 OCR" * 80).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            parsed["units"][0]["ocr_manifest_path"],
            "text/edition_01.ocr.json",
        )

    def test_parse_reuses_only_fully_matching_ocr_manifest(self):
        _issue_path, _original, _text_dir = self.write_parse_issue()
        with mock.patch.object(
            wechat_read,
            "ocr_image",
            side_effect=["第一版 OCR" * 100, "第二版 OCR" * 100],
        ):
            first, first_error = wechat_read.parse(
                self.source, self.day, self.archive
            )
        self.assertIsNone(first_error)

        with mock.patch.object(
            wechat_read, "ocr_image", side_effect=AssertionError("不应重跑 OCR")
        ) as ocr:
            second, second_error = wechat_read.parse(
                self.source, self.day, self.archive
            )

        self.assertIsNone(second_error)
        ocr.assert_not_called()
        self.assertEqual(first["units"], second["units"])
        for edition in (1, 2):
            manifest = json.loads(
                (
                    self.target_issue_dir()
                    / "text"
                    / f"edition_{edition:02d}.ocr.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(len(manifest["page_image_sha256"]), 64)
            self.assertEqual(len(manifest["text_sha256"]), 64)

    def test_parse_hash_mismatch_then_ocr_failure_preserves_old_issue_and_sidecars(self):
        issue_path, _original, _text_dir = self.write_parse_issue()
        first_text, first_manifest = self.write_bound_ocr(1, "第一版旧 OCR" * 80)
        second_text, second_manifest = self.write_bound_ocr(2, "第二版旧 OCR" * 80)
        mismatch = json.loads(first_manifest.read_text(encoding="utf-8"))
        mismatch["page_image_sha256"] = "f" * 64
        first_manifest.write_text(
            json.dumps(mismatch, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        snapshots = {
            path: path.read_bytes()
            for path in (
                issue_path,
                first_text,
                first_manifest,
                second_text,
                second_manifest,
            )
        }

        with mock.patch.object(
            wechat_read, "ocr_image", side_effect=RuntimeError("helper failed")
        ):
            _parsed, error = wechat_read.parse(
                self.source, self.day, self.archive
            )

        self.assertIn("第1版 OCR 失败", error)
        for path, expected in snapshots.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_ocr_memory_error_is_not_masked_by_temp_cleanup_failure(self):
        self.write_parse_issue()
        primary = MemoryError("OCR out of memory")
        cleanup = OSError("temporary unlink failed")
        real_unlink = wechat_read.os.unlink

        def fail_only_ocr_temporary(path, *args, **kwargs):
            if "readdaily-wechat-ocr-" in os.fspath(path):
                raise cleanup
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
                wechat_read, "ocr_image", side_effect=primary
        ), mock.patch.object(
                wechat_read.os, "unlink", side_effect=fail_only_ocr_temporary
        ):
            with self.assertRaises(MemoryError) as caught:
                wechat_read.parse(self.source, self.day, self.archive)

        self.assertIs(caught.exception, primary)
        self.assertIs(caught.exception.__cause__, cleanup)

    def test_directory_swap_restores_previous_issue_when_new_tree_commit_fails(self):
        target = self.base / "swap-target"
        staging = self.base / "swap-staging"
        target.mkdir()
        staging.mkdir()
        (target / "sentinel.txt").write_text("旧期次", encoding="utf-8")
        (staging / "sentinel.txt").write_text("新期次", encoding="utf-8")
        original_replace = wechat_read.os.replace
        failed = False

        def fail_new_tree_once(source, destination, **kwargs):
            nonlocal failed
            if (not failed
                    and Path(source).name == staging.name
                    and Path(destination).name == target.name):
                failed = True
                raise OSError("simulated commit failure")
            return original_replace(source, destination, **kwargs)

        with mock.patch.object(
            wechat_read.os, "replace", side_effect=fail_new_tree_once
        ):
            with self.assertRaisesRegex(OSError, "simulated commit failure"):
                wechat_read._replace_issue_directory(str(staging), str(target))

        self.assertTrue(failed)
        self.assertEqual(
            (target / "sentinel.txt").read_text(encoding="utf-8"), "旧期次"
        )
        self.assertEqual(
            (staging / "sentinel.txt").read_text(encoding="utf-8"), "新期次"
        )
        self.assertEqual(
            list(self.base.glob(".swap-target.previous.*")), []
        )

    def test_ocr_image_rejects_missing_nonzero_and_empty_helper_results(self):
        helper = self.base / "vocr"
        helper.write_text("helper", encoding="utf-8")
        helper.chmod(0o755)
        image = self.base / "page.jpg"
        image.write_bytes(b"image")

        with mock.patch.object(wechat_read, "VOCR", str(self.base / "missing")):
            with self.assertRaisesRegex(RuntimeError, "VOCR|OCR"):
                wechat_read.ocr_image(str(image))

        outcomes = (
            subprocess.CompletedProcess(
                args=[str(helper), str(image)], returncode=7,
                stdout="partial output", stderr="helper failed",
            ),
            subprocess.CompletedProcess(
                args=[str(helper), str(image)], returncode=0,
                stdout="   \n", stderr="",
            ),
        )
        for completed in outcomes:
            with self.subTest(returncode=completed.returncode):
                with mock.patch.object(wechat_read, "VOCR", str(helper)), \
                        mock.patch.object(
                            wechat_read.subprocess, "run", return_value=completed
                        ):
                    with self.assertRaisesRegex(RuntimeError, "OCR|VOCR"):
                        wechat_read.ocr_image(str(image))

        with mock.patch.object(wechat_read, "VOCR", str(helper)), \
                mock.patch.object(
                    wechat_read.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(str(helper), 180),
                ):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                wechat_read.ocr_image(str(image))

    def test_parse_empty_second_ocr_preserves_issue_and_writes_no_text(self):
        issue_path, original, text_dir = self.write_parse_issue()

        with mock.patch.object(
            wechat_read, "ocr_image", side_effect=["A" * 240, ""]
        ):
            _issue, error = wechat_read.parse(
                self.source, self.day, self.archive
            )

        self.assertIsNotNone(error)
        self.assertIn("第2版", error)
        self.assertEqual(issue_path.read_bytes(), original)
        self.assertEqual(list(text_dir.iterdir()), [])

    def test_parse_nonzero_second_helper_preserves_issue_and_writes_no_text(self):
        issue_path, original, text_dir = self.write_parse_issue()
        helper = self.base / "vocr"
        helper.write_text("helper", encoding="utf-8")
        helper.chmod(0o755)
        outcomes = [
            subprocess.CompletedProcess(
                args=[str(helper)], returncode=0,
                stdout="A" * 240, stderr="",
            ),
            subprocess.CompletedProcess(
                args=[str(helper)], returncode=9,
                stdout="", stderr="bad page",
            ),
        ]
        real_run = subprocess.run

        def run_helper_or_image_decoder(args, *positional, **keywords):
            if args and args[0] == str(helper):
                return outcomes.pop(0)
            return real_run(args, *positional, **keywords)

        with mock.patch.object(wechat_read, "VOCR", str(helper)), \
                mock.patch.object(
                    wechat_read.subprocess,
                    "run",
                    side_effect=run_helper_or_image_decoder,
                ):
            _issue, error = wechat_read.parse(
                self.source, self.day, self.archive
            )

        self.assertIsNotNone(error)
        self.assertIn("第2版", error)
        self.assertEqual(issue_path.read_bytes(), original)
        self.assertEqual(list(text_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
