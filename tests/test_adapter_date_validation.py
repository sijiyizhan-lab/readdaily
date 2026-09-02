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


def load_adapter(name):
    path = FETCH_SCRIPTS / "adapters" / (name + ".py")
    spec = importlib.util.spec_from_file_location("date_test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cms_index = load_adapter("cms_index")
founder = load_adapter("founder")


def page_jpeg(size=210000, fill=b"x"):
    """Return a real page image; legacy name keeps tests concise."""
    return page_png(min_bytes=size, fill=(fill or b"x")[:1])


class AdapterDateValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self.tmp.name) / "archive"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def cms_source():
        return {
            "id": "nmrb",
            "name": "农民日报",
            "cms": {
                "index_json": "https://example.test/index.json",
                "site": "https://example.test/",
                "paper_code": "nmrb",
                "max_pages": 16,
            },
        }

    @staticmethod
    def cms_entry(day, issue="13394", paper_code="nmrb"):
        midnight = datetime.datetime.combine(
            day,
            datetime.time(),
            tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
        )
        return {
            "paperCode": paper_code,
            "paperDate": int(midnight.timestamp() * 1000),
            "paperIssueNum": "%s_%s_%s" % (
                paper_code, day.strftime("%Y%m%d"), issue
            ),
            "pagePath": "%s/html/%s/%s/%s_%s_%s_1.html" % (
                paper_code,
                day.year,
                day.strftime("%Y%m%d"),
                paper_code,
                day.strftime("%Y%m%d"),
                issue,
            ),
        }

    def test_cms_index_rejects_previous_day_and_wrong_paper_without_writing(self):
        requested = datetime.date(2026, 9, 2)
        payload = {
            "papers": [
                self.cms_entry(datetime.date(2026, 9, 1), issue="13393"),
                self.cms_entry(requested, issue="99999", paper_code="other"),
            ]
        }

        with mock.patch.object(
            cms_index,
            "_get",
            return_value=(200, json.dumps(payload, ensure_ascii=False)),
        ):
            issue, error = cms_index.fetch(
                self.cms_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse((self.archive / "nmrb" / requested.isoformat()).exists())

    def test_cms_index_selects_requested_date_instead_of_first_entry(self):
        requested = datetime.date(2026, 9, 2)
        target = self.cms_entry(requested)
        payload = {
            "papers": [
                self.cms_entry(datetime.date(2026, 9, 1), issue="13393"),
                target,
            ]
        }
        page_url = "https://example.test/" + target["pagePath"]
        image_url = "https://example.test/group/page-01.jpg"
        page_html = (
            '<html><head><title>农民日报 2026年09月02日</title></head>'
            '<body>第1版：首页<a href="nmrb_20260902_13394_1.html">1</a>'
            '<a href="nmrb_20260902_13394_1_1.html">文章</a>'
            '<img src="%s" id="pageImg"></body></html>' % image_url
        )
        data_url = cms_index._issue_data_url(page_url, requested)
        data_payload = [{
            "pageNo": "1",
            "paperDate": requested.isoformat(),
            "issueDate": requested.isoformat(),
            "pageHref": "nmrb_20260902_13394_1.html",
            "pageName": "首页",
            "pageBigImgPath": "/group/page-01.jpg",
            "pdfHref": "/group/page-01.pdf",
            "onePageArticleList": [{
                "articleHref": "nmrb_20260902_13394_1_1.html",
            }],
        }]

        def fake_response(url, ref=None):
            del ref
            if url == data_url:
                return 200, data_url, json.dumps(data_payload, ensure_ascii=False)
            return 200, page_url, page_html

        with mock.patch.object(
            cms_index,
            "_get",
            return_value=(200, json.dumps(payload, ensure_ascii=False)),
        ), mock.patch.object(
            cms_index,
            "_get_response",
            side_effect=fake_response,
        ), mock.patch.object(
            cms_index.lib,
            "http_get",
            return_value=(200, image_url, page_jpeg(size=60000)),
        ):
            issue, error = cms_index.fetch(
                self.cms_source(), requested, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(issue["date"], requested.isoformat())
        self.assertEqual(issue["issue_no"], "13394")

    def test_cms_fetch_commit_failure_preserves_existing_issue_tree(self):
        requested = datetime.date(2026, 9, 2)
        target_entry = self.cms_entry(requested)
        page_url = "https://example.test/" + target_entry["pagePath"]
        image_url = "https://example.test/group/page-01.jpg"
        page_html = (
            '<html><head><title>农民日报 2026年09月02日</title></head>'
            '<body><a href="nmrb_20260902_13394_1.html">第1版：首页</a>'
            '<a href="nmrb_20260902_13394_1_1.html">文章</a></body></html>'
        )
        data_url = cms_index._issue_data_url(page_url, requested)
        data_payload = [{
            "pageNo": "1",
            "paperDate": requested.isoformat(),
            "issueDate": requested.isoformat(),
            "pageHref": "nmrb_20260902_13394_1.html",
            "pageName": "首页",
            "pageBigImgPath": "/group/page-01.jpg",
            "onePageArticleList": [{
                "articleHref": "nmrb_20260902_13394_1_1.html",
            }],
        }]

        def fake_response(url, ref=None):
            del ref
            if url == data_url:
                return 200, data_url, json.dumps(data_payload, ensure_ascii=False)
            return 200, page_url, page_html

        target = self.archive / "nmrb" / requested.isoformat()
        (target / "pages").mkdir(parents=True)
        old_issue = b'{"version":"old"}'
        old_page = b"old-page"
        (target / "issue.json").write_bytes(old_issue)
        (target / "pages" / "old.jpg").write_bytes(old_page)

        with mock.patch.object(
            cms_index,
            "_get",
            return_value=(
                200,
                json.dumps({"papers": [target_entry]}, ensure_ascii=False),
            ),
        ), mock.patch.object(
            cms_index, "_get_response", side_effect=fake_response
        ), mock.patch.object(
            cms_index.lib,
            "http_get",
            return_value=(200, image_url, page_jpeg(size=60000)),
        ), mock.patch.object(
            cms_index.lib,
            "commit_issue_tree",
            side_effect=OSError("disk full"),
        ):
            issue, error = cms_index.fetch(
                self.cms_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("事务", error)
        self.assertEqual((target / "issue.json").read_bytes(), old_issue)
        self.assertEqual((target / "pages" / "old.jpg").read_bytes(), old_page)
        self.assertEqual(
            sorted(
                str(path.relative_to(target))
                for path in target.rglob("*")
                if path.is_file()
            ),
            ["issue.json", "pages/old.jpg"],
        )

    def test_cms_index_rejects_target_entry_redirected_to_previous_day(self):
        requested = datetime.date(2026, 9, 2)
        target = self.cms_entry(requested)
        payload = {"papers": [target]}
        previous_url = (
            "https://example.test/nmrb/html/2026/20260901/"
            "nmrb_20260901_13393_1.html"
        )

        with mock.patch.object(
            cms_index,
            "_get",
            return_value=(200, json.dumps(payload, ensure_ascii=False)),
        ), mock.patch.object(
            cms_index,
            "_get_response",
            return_value=(200, previous_url, "<html><title>2026年09月01日</title></html>"),
        ):
            issue, error = cms_index.fetch(
                self.cms_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse((self.archive / "nmrb" / requested.isoformat()).exists())

    def test_cms_index_rejects_one_failed_discovered_edition_without_writing(self):
        requested = datetime.date(2026, 9, 2)
        target = self.cms_entry(requested)
        payload = {"papers": [target]}
        page1_url = "https://example.test/" + target["pagePath"]
        image_url = "https://example.test/group/page-01.jpg"
        page1 = (
            "<html><head><title>农民日报 2026年09月02日</title></head><body>"
            '<a href="nmrb_20260902_13394_1.html">第1版：首页</a>'
            '<a href="nmrb_20260902_13394_2.html">第2版：综合</a>'
            '<a href="nmrb_20260902_13394_1_1.html">第一版文章</a>'
            + ('<img id="pageImg" src="%s">' % image_url)
            + "</body></html>"
        )

        def fake_response(url, ref=None):
            del ref
            if url == page1_url or url.endswith("_1.html"):
                return 200, url, page1
            if url.endswith("_2.html"):
                return 503, url, ""
            raise AssertionError("unexpected URL %s" % url)

        with mock.patch.object(
            cms_index,
            "_get",
            return_value=(200, json.dumps(payload, ensure_ascii=False)),
        ), mock.patch.object(
            cms_index, "_get_response", side_effect=fake_response
        ), mock.patch.object(
            cms_index.lib,
            "http_get",
            return_value=(200, image_url, page_jpeg(size=60000)),
        ):
            issue, error = cms_index.fetch(
                self.cms_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("第2版", error)
        self.assertFalse((self.archive / "nmrb" / requested.isoformat()).exists())

    def test_cms_fetch_rejects_wrong_date_discovered_article_without_writing(self):
        requested = datetime.date(2026, 9, 2)
        target = self.cms_entry(requested)
        page_url = "https://example.test/" + target["pagePath"]
        page_html = (
            "<html><head><title>农民日报 2026年09月02日</title></head><body>"
            '<a href="nmrb_20260902_13394_1.html">第1版：首页</a>'
            '<a href="nmrb_20260901_13393_1_1.html">旧日文章</a>'
            "</body></html>"
        )

        with mock.patch.object(
            cms_index,
            "_get",
            return_value=(200, json.dumps({"papers": [target]}, ensure_ascii=False)),
        ), mock.patch.object(
            cms_index,
            "_get_response",
            return_value=(200, page_url, page_html),
        ):
            issue, error = cms_index.fetch(
                self.cms_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("文章链接日期", error)
        self.assertFalse((self.archive / "nmrb" / requested.isoformat()).exists())

    def test_cms_fetch_rejects_wrong_date_discovered_edition_without_writing(self):
        requested = datetime.date(2026, 9, 2)
        target = self.cms_entry(requested)
        page_url = "https://example.test/" + target["pagePath"]
        page_html = (
            "<html><head><title>农民日报 2026年09月02日</title></head><body>"
            '<a href="nmrb_20260902_13394_1.html">第1版：首页</a>'
            '<a href="nmrb_20260901_13393_2.html">第2版：综合</a>'
            "</body></html>"
        )

        with mock.patch.object(
            cms_index,
            "_get",
            return_value=(200, json.dumps({"papers": [target]}, ensure_ascii=False)),
        ), mock.patch.object(
            cms_index,
            "_get_response",
            return_value=(200, page_url, page_html),
        ):
            issue, error = cms_index.fetch(
                self.cms_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("版次导航链接日期", error)
        self.assertFalse((self.archive / "nmrb" / requested.isoformat()).exists())

    @staticmethod
    def founder_source():
        return {
            "id": "gmrb",
            "name": "光明日报",
            "entry": "https://example.test/",
            "index_url": "https://example.test/gmrb/html/layout/index.html",
            "node_tpl": "https://example.test/gmrb/html/layout/{y}{m}/{d}/node_{page:02d}.html",
            "max_pages": 20,
        }

    @staticmethod
    def index_html(href):
        return '<html><body><a href="%s">第01版 要闻</a></body></html>' % href

    @staticmethod
    def page_html(day_text="2026年09月02日"):
        return (
            "<html><head><title>光明日报 %s</title></head>"
            "<body>第01版 要闻</body></html>" % day_text
        )

    @staticmethod
    def founder_layout_html(pic_url, name="要闻", day_text="2026年09月02日"):
        return (
            "<html><head><title>光明日报 %s</title></head><body>"
            '<script>window.layoutData={layout:"01",layoutName:"%s",'
            'picUrl:"%s"};</script>'
            "<a href='content_1.html'>当日文章</a></body></html>"
            % (day_text, name, pic_url)
        )

    def test_founder_static_index_rejects_previous_day_link_without_fallback(self):
        requested = datetime.date(2026, 9, 2)
        prior = "../20260901/node_01.html"

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("index.html"):
                body = self.index_html(prior).encode()
                return 200, url, body
            return 200, url, self.page_html("2026年09月01日").encode()

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_static_index_rejects_undated_link(self):
        requested = datetime.date(2026, 9, 2)
        undated = "node_01.html"

        with mock.patch.object(
            founder.lib,
            "http_get",
            return_value=(
                200,
                self.founder_source()["index_url"],
                self.index_html(undated).encode(),
            ),
        ):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("确认", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_static_index_rejects_missing_or_renumbered_edition(self):
        requested = datetime.date(2026, 9, 2)
        index = (
            "<html><body>"
            '<a href="../20260902/node_01.html">第01版 要闻</a>'
            '<a href="../20260902/node_03.html">第03版 专题</a>'
            "</body></html>"
        )

        with mock.patch.object(
            founder.lib,
            "http_get",
            return_value=(200, self.founder_source()["index_url"], index.encode()),
        ):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("连续", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_static_index_rejects_mixed_date_edition_links(self):
        requested = datetime.date(2026, 9, 2)
        index = (
            "<html><body>"
            '<a href="../20260902/node_01.html">第01版 要闻</a>'
            '<a href="../20260901/node_02.html">第02版 综合</a>'
            "</body></html>"
        )

        with mock.patch.object(
            founder.lib,
            "http_get",
            return_value=(200, self.founder_source()["index_url"], index.encode()),
        ):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("版面链接", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_fetch_rejects_wrong_date_discovered_article_without_writing(self):
        requested = datetime.date(2026, 9, 2)
        target = "../20260902/node_01.html"
        page = (
            self.page_html().replace(
                "</body>",
                '<a href="../20260901/content_1.html">旧日文章</a></body>',
            )
        )

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("index.html"):
                return 200, url, self.index_html(target).encode()
            return 200, url, page.encode()

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("文章链接日期", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_rejects_target_link_redirected_to_previous_day(self):
        requested = datetime.date(2026, 9, 2)
        target = "../20260902/node_01.html"

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("index.html"):
                return 200, url, self.index_html(target).encode()
            redirected = "https://example.test/gmrb/html/layout/20260901/node_01.html"
            return 200, redirected, self.page_html("2026年09月01日").encode()

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_dynamic_probe_rejects_previous_day_redirect(self):
        requested = datetime.date(2026, 9, 2)
        source = self.founder_source()
        source.pop("index_url")
        source["max_pages"] = 1
        redirected = "https://example.test/gmrb/html/layout/20260901/node_01.html"
        old_page = (
            "<html><head><title>光明日报 2026年09月01日</title></head>"
            "<body>第01版 要闻</body></html>"
        ).encode() + (b" " * 4000)

        with mock.patch.object(
            founder.lib,
            "http_get",
            return_value=(200, redirected, old_page),
        ):
            issue, error = founder.fetch(source, requested, str(self.archive))

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_dynamic_probe_treats_server_error_as_failure_not_end_of_issue(self):
        requested = datetime.date(2026, 9, 2)
        source = self.founder_source()
        source.pop("index_url")
        source["max_pages"] = 3
        page_one = self.page_html().encode() + (b" " * 4000)

        def fake_http_get(url, referer=None):
            del referer
            if "node_01.html" in url:
                return 200, url, page_one
            if "node_02.html" in url:
                return 503, url, b""
            raise AssertionError("unexpected URL %s" % url)

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(source, requested, str(self.archive))

        self.assertIsNone(issue)
        self.assertIn("第2版", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_dynamic_probe_rejects_404_hole_before_later_page(self):
        requested = datetime.date(2026, 9, 2)
        source = self.founder_source()
        source.pop("index_url")
        source["max_pages"] = 3
        page_body = self.page_html().encode() + (b" " * 4000)
        requested_urls = []

        def fake_http_get(url, referer=None):
            del referer
            requested_urls.append(url)
            if "node_01.html" in url or "node_03.html" in url:
                return 200, url, page_body
            return 404, url, b""

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(source, requested, str(self.archive))

        self.assertIsNone(issue)
        self.assertIn("不连续", error)
        self.assertTrue(any("node_03.html" in url for url in requested_urls))
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_rejects_previous_day_image_candidate_before_request(self):
        requested = datetime.date(2026, 9, 2)
        target = "../20260902/node_01.html"
        old_image = "../../../pc/pic/202609/01/page.jpg"
        requested_urls = []

        def fake_http_get(url, referer=None):
            del referer
            requested_urls.append(url)
            if url.endswith("index.html"):
                return 200, url, self.index_html(target).encode()
            if "node_01.html" in url:
                return 200, url, self.founder_layout_html(old_image).encode()
            return 200, url, b"x" * 210000

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("版面图", error)
        self.assertNotIn("page.jpg", "\n".join(requested_urls))
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_buffers_all_images_and_rejects_old_final_url_without_writing(self):
        requested = datetime.date(2026, 9, 2)
        index = (
            "<html><body>"
            '<a href="../20260902/node_01.html">第01版 要闻</a>'
            '<a href="../20260902/node_02.html">第02版 综合</a>'
            "</body></html>"
        )

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("index.html"):
                return 200, url, index.encode()
            if "node_01.html" in url:
                return 200, url, self.founder_layout_html(
                    "../../../pc/pic/202609/02/page-01.jpg", "要闻"
                ).encode()
            if "node_02.html" in url:
                return 200, url, self.founder_layout_html(
                    "../../../pc/pic/202609/02/page-02.jpg", "综合"
                ).encode()
            if "page-01.jpg" in url:
                return 200, url, page_jpeg(fill=b"1")
            if "page-02.jpg" in url:
                previous = url.replace("202609/02", "202609/01")
                return 200, previous, page_jpeg(fill=b"2")
            raise AssertionError("unexpected URL %s" % url)

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("版面图", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_rejects_one_failed_discovered_edition_without_writing(self):
        requested = datetime.date(2026, 9, 2)
        index = (
            "<html><body>"
            '<a href="../20260902/node_01.html">第01版 要闻</a>'
            '<a href="../20260902/node_02.html">第02版 综合</a>'
            "</body></html>"
        )

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("index.html"):
                return 200, url, index.encode()
            if "node_01.html" in url:
                return 200, url, self.founder_layout_html(
                    "../../../pc/pic/202609/02/page-01.jpg", "要闻"
                ).encode()
            if "page-01.jpg" in url:
                return 200, url, page_jpeg(fill=b"1")
            if "node_02.html" in url:
                return 503, url, b""
            raise AssertionError("unexpected URL %s" % url)

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("第2版", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_accepts_exact_image_candidate_and_final_url(self):
        requested = datetime.date(2026, 9, 2)
        target = "../20260902/node_01.html"
        image = "../../../pc/pic/202609/02/page.jpg"

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("index.html"):
                return 200, url, self.index_html(target).encode()
            if "node_01.html" in url:
                return 200, url, self.founder_layout_html(image).encode()
            return 200, url, page_jpeg()

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(issue["date"], requested.isoformat())
        page_path = issue["editions"][0]["page_image"]
        self.assertEqual(issue["editions"][0]["page_image_width"], 1280)
        self.assertEqual(issue["editions"][0]["page_image_height"], 1823)
        self.assertEqual(len(issue["editions"][0]["page_image_sha256"]), 64)
        self.assertEqual(
            issue["units"][0]["page_image_sha256"],
            issue["editions"][0]["page_image_sha256"],
        )
        self.assertTrue((self.archive / "gmrb" / requested.isoformat() / page_path).is_file())

    def test_founder_rejects_decodable_thumbnail_without_writing(self):
        requested = datetime.date(2026, 9, 2)
        target = "../20260902/node_01.html"
        image = "../../../pc/pic/202609/02/page.jpg"
        thumbnail = page_png(width=32, height=32, min_bytes=60000)

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("index.html"):
                return 200, url, self.index_html(target).encode()
            if "node_01.html" in url:
                return 200, url, self.founder_layout_html(image).encode()
            return 200, url, thumbnail

        with mock.patch.object(founder.lib, "http_get", side_effect=fake_http_get):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("第1版", error)
        self.assertFalse((self.archive / "gmrb" / requested.isoformat()).exists())

    def test_founder_fetch_commit_failure_preserves_existing_issue_tree(self):
        requested = datetime.date(2026, 9, 2)
        page_href = "../20260902/node_01.html"
        image_href = "../../../pc/pic/202609/02/page.jpg"

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("index.html"):
                return 200, url, self.index_html(page_href).encode()
            if "node_01.html" in url:
                return 200, url, self.founder_layout_html(image_href).encode()
            return 200, url, page_jpeg()

        target = self.archive / "gmrb" / requested.isoformat()
        (target / "pages").mkdir(parents=True)
        old_issue = b'{"version":"old"}'
        old_page = b"old-page"
        (target / "issue.json").write_bytes(old_issue)
        (target / "pages" / "old.jpg").write_bytes(old_page)

        with mock.patch.object(
            founder.lib, "http_get", side_effect=fake_http_get
        ), mock.patch.object(
            founder.lib,
            "commit_issue_tree",
            side_effect=OSError("disk full"),
        ):
            issue, error = founder.fetch(
                self.founder_source(), requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("事务", error)
        self.assertEqual((target / "issue.json").read_bytes(), old_issue)
        self.assertEqual((target / "pages" / "old.jpg").read_bytes(), old_page)
        self.assertEqual(
            sorted(
                str(path.relative_to(target))
                for path in target.rglob("*")
                if path.is_file()
            ),
            ["issue.json", "pages/old.jpg"],
        )


if __name__ == "__main__":
    unittest.main()
