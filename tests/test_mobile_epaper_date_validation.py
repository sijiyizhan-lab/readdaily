import datetime
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(FETCH_SCRIPTS))

from tests.image_fixtures import page_png


def load_adapter():
    path = FETCH_SCRIPTS / "adapters" / "mobile_epaper.py"
    spec = importlib.util.spec_from_file_location(
        "mobile_epaper_date_test_adapter", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mobile_epaper = load_adapter()


class MobileEpaperDateValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self.tmp.name) / "archive"
        self.requested = datetime.date(2026, 9, 2)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def source():
        return {
            "id": "bjrb",
            "name": "北京日报",
            "mob": {
                "index_tpl": (
                    "https://example.test/bjrb/mobile/{y}/"
                    "{yymmdd}/{yymmdd}_m.html"
                ),
                "site": "https://example.test/",
                "max_pages": 24,
            },
        }

    @staticmethod
    def page_image_bytes():
        return page_png(min_bytes=60000, fill=b"I")

    @staticmethod
    def index_html(nav_day=None, page_no=1):
        if nav_day is None:
            pdf_href = "../edition_001/page.pdf"
            article_href = "edition_001/content_1.htm"
        else:
            stamp = nav_day.strftime("%Y%m%d")
            pdf_href = "../%s_%03d/page.pdf" % (stamp, page_no)
            article_href = "%s_%03d/content_1.htm" % (stamp, page_no)
        return (
            '<html><head><title>北京日报</title></head><body>'
            '<a pdf_href="%s">第%d版 要闻</a>'
            '<a data-href="%s">目标日新闻标题</a>'
            "</body></html>"
        ) % (pdf_href, page_no, article_href)

    def target_dir(self):
        return self.archive / "bjrb" / self.requested.isoformat()

    def write_issue(self, *, source="bjrb", day=None, article_url=None):
        day = day or self.requested
        article_url = article_url or (
            "https://example.test/bjrb/mobile/2026/20260902/"
            "20260902_001/content_20260902_001_1.htm"
        )
        issue = {
            "source": source,
            "source_name": "北京日报",
            "date": day.isoformat(),
            "channel": "mobile_epaper",
            "editions": [],
            "units": [
                {
                    "id": "bjrb_20260902_01",
                    "title": "1版 要闻",
                    "url": article_url,
                    "articles": [
                        {"title": "目标日新闻标题", "url": article_url}
                    ],
                }
            ],
        }
        issue_path = self.target_dir() / "issue.json"
        issue_path.parent.mkdir(parents=True)
        issue_path.write_text(
            json.dumps(issue, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return issue_path, issue

    def test_fetch_rejects_index_redirected_to_previous_day_without_writing(self):
        previous = datetime.date(2026, 9, 1)
        final_url = (
            "https://example.test/bjrb/mobile/2026/"
            "20260901/20260901_m.html"
        )

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, final_url, self.index_html(previous).encode()),
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse(self.target_dir().exists())

    def test_fetch_rejects_previous_day_navigation_without_writing(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        previous = datetime.date(2026, 9, 1)

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, final_url, self.index_html(previous).encode()),
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse(self.target_dir().exists())

    def test_fetch_rejects_undated_final_url_without_writing(self):
        final_url = "https://example.test/bjrb/mobile/latest/index.html"

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(
                200,
                final_url,
                self.index_html(self.requested).encode(),
            ),
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("确认", error)
        self.assertFalse(self.target_dir().exists())

    def test_fetch_rejects_undated_navigation_without_writing(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, final_url, self.index_html().encode()),
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("确认", error)
        self.assertFalse(self.target_dir().exists())

    def test_probe_does_not_report_success_without_dated_navigation(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, final_url, b"<html><body></body></html>"),
        ):
            result = mobile_epaper.probe(self.source(), self.requested)

        self.assertNotIn("index_ok", result[0])
        self.assertIn("确认", result[0]["note"])

    def test_fetch_rejects_article_reference_from_previous_day(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        previous_stamp = "20260901"
        html = (
            '<html><body><a pdf_href="../%s_001/page.pdf">第1版 要闻</a>'
            '<a data-href="%s_001/content_1.htm">错日新闻标题</a>'
            "</body></html>"
        ) % (requested_stamp, previous_stamp)

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, final_url, html.encode()),
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse(self.target_dir().exists())

    def test_fetch_rejects_target_day_article_outside_discovered_editions(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        html = (
            '<html><body><a pdf_href="../%s_001/page.pdf">第1版 要闻</a>'
            '<a data-href="./%s_002/content_orphan.htm">未归属文章</a>'
            "</body></html>"
        ) % (requested_stamp, requested_stamp)

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, final_url, html.encode()),
        ) as http_get:
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("不属于任何版次", error)
        self.assertEqual(http_get.call_count, 1)
        self.assertFalse(self.target_dir().exists())

    def test_fetch_accepts_matching_final_url_and_navigation_date(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        html = self.index_html(self.requested).encode()

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("_m.html"):
                return 200, final_url, html
            return 200, url, self.page_image_bytes()

        with mock.patch.object(
            mobile_epaper.lib, "http_get", side_effect=fake_http_get
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(issue["date"], self.requested.isoformat())
        self.assertEqual(issue["editions"][0]["pdf_href"], "../20260902_001/page.pdf")
        self.assertEqual(
            issue["units"][0]["articles"][0]["url"],
            "https://example.test/bjrb/mobile/2026/20260902/"
            "20260902_001/content_1.htm",
        )
        self.assertEqual(issue["editions"][0]["page_image_width"], 1280)
        self.assertEqual(issue["editions"][0]["page_image_height"], 1823)
        self.assertEqual(len(issue["editions"][0]["page_image_sha256"]), 64)
        self.assertEqual(
            issue["units"][0]["page_image_sha256"],
            issue["editions"][0]["page_image_sha256"],
        )
        self.assertTrue((self.target_dir() / "issue.json").is_file())

    def test_fetch_rejects_decodable_thumbnail_before_creating_issue_tree(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        html = self.index_html(self.requested).encode()
        thumbnail = page_png(width=32, height=32, min_bytes=60000)

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("_m.html"):
                return 200, final_url, html
            return 200, url, thumbnail

        with mock.patch.object(
            mobile_epaper.lib, "http_get", side_effect=fake_http_get
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("尺寸过小", error)
        self.assertFalse(self.target_dir().exists())

    def test_fetch_commit_failure_preserves_existing_issue_tree(self):
        target = self.target_dir()
        (target / "pages").mkdir(parents=True)
        old_issue = b'{"version":"old"}'
        old_page = b"old-page"
        (target / "issue.json").write_bytes(old_issue)
        (target / "pages" / "old.jpg").write_bytes(old_page)
        stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (stamp, stamp)
        )
        index = self.index_html(self.requested).encode()

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("_m.html"):
                return 200, final_url, index
            return 200, url, self.page_image_bytes()

        with mock.patch.object(
            mobile_epaper.lib, "http_get", side_effect=fake_http_get
        ), mock.patch.object(
            mobile_epaper.lib,
            "commit_issue_tree",
            side_effect=OSError("disk full"),
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("事务", error)
        self.assertEqual((target / "issue.json").read_bytes(), old_issue)
        self.assertEqual((target / "pages" / "old.jpg").read_bytes(), old_page)
        self.assertEqual(
            sorted(str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()),
            ["issue.json", "pages/old.jpg"],
        )

    def test_fetch_keeps_short_article_title_instead_of_dropping_link(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        html = (
            '<html><body><a pdf_href="../%s_001/page.pdf">第1版 要闻</a>'
            '<a data-href="%s_001/content_1.htm">两字</a>'
            "</body></html>"
        ) % (requested_stamp, requested_stamp)

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("_m.html"):
                return 200, final_url, html.encode()
            return 200, url, self.page_image_bytes()

        with mock.patch.object(
            mobile_epaper.lib, "http_get", side_effect=fake_http_get
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(issue["units"][0]["articles"][0]["title"], "两字")

    def test_fetch_accepts_single_quoted_attributes_and_all_title_lengths(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        long_title = "建设日报长标题" * 300
        html = (
            "<html><body>"
            "<div class='nav' pdf_href='../%s_001/page.pdf'>第1版 </div>"
            "<a class='article' data-href='./%s_001/content_1.htm'></a>"
            "<a data-href='./%s_001/content_2.htm' class='article'>两字</a>"
            "<a data-href='./%s_001/content_3.htm'>%s</a>"
            "</body></html>"
        ) % (
            requested_stamp,
            requested_stamp,
            requested_stamp,
            requested_stamp,
            long_title,
        )

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("_m.html"):
                return 200, final_url, html.encode()
            return 200, url, self.page_image_bytes()

        with mock.patch.object(
            mobile_epaper.lib, "http_get", side_effect=fake_http_get
        ):
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(
            [article["title"] for article in issue["units"][0]["articles"]],
            ["", "两字", long_title],
        )
        self.assertTrue((self.target_dir() / "pages" / "01版.jpg").is_file())

    def test_fetch_rejects_an_edition_without_articles_before_writing(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        html = (
            '<div pdf_href="../%s_001/page.pdf">第1版 要闻</div>'
            '<a data-href="./%s_001/content_1.htm">第一版文章</a>'
            '<div pdf_href="../%s_002/page.pdf">第2版 综合</div>'
        ) % (requested_stamp, requested_stamp, requested_stamp)

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("_m.html"):
                return 200, final_url, html.encode()
            return 200, url, self.page_image_bytes()

        with mock.patch.object(
            mobile_epaper.lib, "http_get", side_effect=fake_http_get
        ) as http_get:
            issue, error = mobile_epaper.fetch(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("第2版", error)
        self.assertIn("文章", error)
        self.assertEqual(http_get.call_count, 1)
        self.assertFalse(self.target_dir().exists())

    def test_fetch_rejects_every_page_image_failure_without_writing(self):
        requested_stamp = self.requested.strftime("%Y%m%d")
        final_url = (
            "https://example.test/bjrb/mobile/2026/%s/%s_m.html"
            % (requested_stamp, requested_stamp)
        )
        html = self.index_html(self.requested).encode()
        cases = (
            ("exception", OSError("offline")),
            ("non_200", (404, "unused", b"")),
            ("too_small", (200, "unused", b"not-an-image")),
            (
                "large_html_shell",
                (200, "unused", b"<html><body>blocked</body></html>" + b"x" * 70000),
            ),
            (
                "truncated_jpeg",
                (200, "unused", b"\xff\xd8\xff" + b"x" * 70000),
            ),
        )

        for label, image_result in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "archive"

                def fake_http_get(url, referer=None):
                    del referer
                    if url.endswith("_m.html"):
                        return 200, final_url, html
                    if isinstance(image_result, Exception):
                        raise image_result
                    status, _unused_url, body = image_result
                    return status, url, body

                with mock.patch.object(
                    mobile_epaper.lib, "http_get", side_effect=fake_http_get
                ) as http_get:
                    issue, error = mobile_epaper.fetch(
                        self.source(), self.requested, str(archive)
                    )

                self.assertIsNone(issue)
                self.assertIn("版面图", error)
                self.assertEqual(http_get.call_count, 2)
                self.assertFalse(
                    (archive / "bjrb" / self.requested.isoformat()).exists()
                )

    def test_parse_rejects_issue_with_wrong_source_before_network_or_write(self):
        issue_path, original = self.write_issue(source="gmrb")

        with mock.patch.object(mobile_epaper.lib, "http_get") as http_get:
            issue, error = mobile_epaper.parse(
                self.source(), self.requested, str(self.archive)
            )

        self.assertEqual(issue, original)
        self.assertIn("来源", error)
        http_get.assert_not_called()
        self.assertEqual(
            json.loads(issue_path.read_text(encoding="utf-8")), original
        )

    def test_parse_rejects_issue_with_wrong_date_before_network_or_write(self):
        issue_path, original = self.write_issue(
            day=datetime.date(2026, 9, 1)
        )

        with mock.patch.object(mobile_epaper.lib, "http_get") as http_get:
            issue, error = mobile_epaper.parse(
                self.source(), self.requested, str(self.archive)
            )

        self.assertEqual(issue, original)
        self.assertIn("日期", error)
        http_get.assert_not_called()
        self.assertEqual(
            json.loads(issue_path.read_text(encoding="utf-8")), original
        )

    def test_parse_rejects_article_redirected_to_previous_day_without_write(self):
        issue_path, original = self.write_issue()
        redirected = (
            "https://example.test/bjrb/mobile/2026/20260901/"
            "20260901_001/content_20260901_001_1.htm"
        )
        body = b'<html><body><div id="content">stale article body</div></body></html>'

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, redirected, body),
        ):
            issue, error = mobile_epaper.parse(
                self.source(), self.requested, str(self.archive)
            )

        self.assertEqual(issue, original)
        self.assertIn("日期", error)
        self.assertEqual(
            json.loads(issue_path.read_text(encoding="utf-8")), original
        )

    def test_parse_accepts_target_dated_article_final_url(self):
        issue_path, _ = self.write_issue()
        final_url = (
            "https://example.test/bjrb/mobile/2026/20260902/"
            "20260902_001/content_20260902_001_1.htm"
        )
        body = (
            '<html><body><div id="content">'
            + ("目标日正文内容。" * 12)
            + "</div></body></html>"
        ).encode()

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, final_url, body),
        ):
            issue, error = mobile_epaper.parse(
                self.source(), self.requested, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertIn("目标日正文内容", issue["units"][0]["text"])
        saved = json.loads(issue_path.read_text(encoding="utf-8"))
        self.assertIn("目标日正文内容", saved["units"][0]["text"])


if __name__ == "__main__":
    unittest.main()
