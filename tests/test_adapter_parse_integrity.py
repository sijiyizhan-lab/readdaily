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
    spec = importlib.util.spec_from_file_location(
        "parse_integrity_test_" + name, path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


paper_api = load_adapter("paper_api")
mobile_epaper = load_adapter("mobile_epaper")
cms_index = load_adapter("cms_index")
founder = load_adapter("founder")


def page_jpeg(size=210000, fill=b"x"):
    return page_png(min_bytes=size, fill=(fill or b"x")[:1])


class AdapterParseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.archive = Path(self.temporary.name) / "archive"
        self.day = datetime.date(2026, 9, 3)

    def tearDown(self):
        self.temporary.cleanup()

    def write_issue(self, source, issue):
        issue_path = self.archive / source / self.day.isoformat() / "issue.json"
        issue_path.parent.mkdir(parents=True)
        original = json.dumps(issue, ensure_ascii=False, indent=2).encode("utf-8")
        issue_path.write_bytes(original)
        return issue_path, original

    def paper_source(self):
        return {
            "id": "kjrb",
            "name": "科技日报",
            "entry": "https://example.test/",
            "api": {"base": "https://example.test/api", "code": "KJRB"},
        }

    def paper_issue(self):
        return {
            "source": "kjrb",
            "source_name": "科技日报",
            "date": self.day.isoformat(),
            "period_time": self.day.isoformat(),
            "editions": [{"no": 1, "name": "要闻"}],
            "units": [{
                "id": "kjrb_20260903_01",
                "api_id": "edition-1",
                "period_id": "period-1",
                "articles": [{"title": "旧内容", "text": "必须保留"}],
                "text": "旧内容\n必须保留",
            }],
        }

    def test_paper_api_empty_edition_response_is_error_and_preserves_issue(self):
        issue_path, original = self.write_issue("kjrb", self.paper_issue())

        with mock.patch.object(paper_api, "_api", return_value=None):
            _issue, error = paper_api.parse(
                self.paper_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("第1版", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_paper_api_empty_article_detail_is_error_and_preserves_issue(self):
        issue_path, original = self.write_issue("kjrb", self.paper_issue())
        responses = [
            {"list": [{
                "id": "article-1",
                "title": "标题",
                "content": "简短导语",
                "publishTime": self.day.isoformat(),
            }]},
            None,
        ]

        with mock.patch.object(paper_api, "_api", side_effect=responses) as api:
            _issue, error = paper_api.parse(
                self.paper_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("全文", error)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_paper_api_long_list_lead_still_fetches_authoritative_detail(self):
        self.write_issue("kjrb", self.paper_issue())
        lead = "列表接口长导语。" * 75
        detail_tail = "详情接口全文末尾标记"
        detail = "详情接口完整正文。" * 120 + detail_tail
        responses = [
            {"list": [{
                "id": "article-1",
                "title": "长导语报道",
                "content": lead,
                "publishTime": self.day.isoformat(),
            }]},
            {"obj": {"articleVo": {
                "id": "article-1",
                "content": detail,
                "publishTime": self.day.isoformat(),
            }}},
        ]

        with mock.patch.object(paper_api, "_api", side_effect=responses) as api:
            parsed, error = paper_api.parse(
                self.paper_source(), self.day, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(
            api.call_args_list[1].args[1],
            "/uv/article/article/articleId",
        )
        self.assertEqual(api.call_args_list[1].args[2], {"id": "article-1"})
        text = parsed["units"][0]["articles"][0]["text"]
        self.assertEqual(text, detail)
        self.assertTrue(text.endswith(detail_tail))

    def test_paper_api_missing_article_id_is_error_and_preserves_issue(self):
        issue_path, original = self.write_issue("kjrb", self.paper_issue())
        response = {"list": [{
            "title": "缺少标识的文章",
            "content": "即使列表正文很长也不能当全文。" * 80,
            "publishTime": self.day.isoformat(),
        }]}

        with mock.patch.object(paper_api, "_api", return_value=response) as api:
            _issue, error = paper_api.parse(
                self.paper_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("文章标识", error)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_paper_api_empty_article_detail_body_is_error_and_preserves_issue(self):
        issue_path, original = self.write_issue("kjrb", self.paper_issue())
        responses = [
            {"list": [{
                "id": "article-1",
                "title": "标题",
                "content": "列表接口长导语。" * 75,
                "publishTime": self.day.isoformat(),
            }]},
            {"obj": {"articleVo": {
                "id": "article-1",
                "content": "   ",
                "publishTime": self.day.isoformat(),
            }}},
        ]

        with mock.patch.object(paper_api, "_api", side_effect=responses) as api:
            _issue, error = paper_api.parse(
                self.paper_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("全文正文为空", error)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_paper_api_rejects_explicit_previous_day_article_metadata(self):
        issue_path, original = self.write_issue("kjrb", self.paper_issue())
        response = {"list": [{
            "id": "article-1",
            "title": "标题",
            "content": "足够长的全文" * 100,
            "publishTime": "2026-09-02",
        }]}

        with mock.patch.object(paper_api, "_api", return_value=response):
            _issue, error = paper_api.parse(
                self.paper_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("日期", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_paper_api_rejects_article_response_without_text(self):
        issue_path, original = self.write_issue("kjrb", self.paper_issue())
        response = {"list": [{
            "id": None,
            "title": "标题",
            "content": "",
            "publishTime": self.day.isoformat(),
        }]}

        with mock.patch.object(paper_api, "_api", return_value=response):
            _issue, error = paper_api.parse(
                self.paper_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("正文", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_paper_api_preserves_article_text_beyond_legacy_limit(self):
        self.write_issue("kjrb", self.paper_issue())
        tail = "全文末尾不可丢失"
        content = "建设行业长文。" * 5000 + tail
        responses = [
            {"list": [{
                "id": "article-1",
                "title": "长篇报道",
                "content": "列表导语",
                "publishTime": self.day.isoformat(),
            }]},
            {"obj": {"articleVo": {
                "id": "article-1",
                "content": content,
                "publishTime": self.day.isoformat(),
            }}},
        ]

        with mock.patch.object(paper_api, "_api", side_effect=responses):
            parsed, error = paper_api.parse(
                self.paper_source(), self.day, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertTrue(parsed["units"][0]["articles"][0]["text"].endswith(tail))

    def test_paper_api_fetch_rejects_non_contiguous_real_edition_numbers(self):
        cases = (
            ["第01版：要闻", "第03版：综合"],
            ["第01版：要闻", "第01版：综合"],
            ["第02版：要闻", "第03版：综合"],
        )
        for names in cases:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "archive"
                response = {
                    "obj": {
                        "periodTime": self.day.isoformat(),
                        "editionList": [
                            {
                                "id": "edition-%s" % index,
                                "periodId": "period-1",
                                "editionName": name,
                                "editionImg": None,
                            }
                            for index, name in enumerate(names, 1)
                        ],
                    },
                }

                with mock.patch.object(paper_api, "_api", return_value=response):
                    issue, error = paper_api.fetch(
                        self.paper_source(), self.day, str(archive)
                    )

                self.assertIsNone(issue)
                self.assertIsNotNone(error)
                self.assertIn("版号", error)
                self.assertFalse(
                    (archive / "kjrb" / self.day.isoformat()).exists()
                )

    def mobile_source(self):
        return {
            "id": "bjrb",
            "name": "北京日报",
            "mob": {
                "site": "https://example.test/",
                "index_tpl": (
                    "https://example.test/bjrb/mobile/{y}/{yymmdd}/"
                    "{yymmdd}_m.html"
                ),
                "max_pages": 24,
            },
        }

    def mobile_issue(self):
        base = "https://example.test/bjrb/mobile/2026/20260903/20260903_001/"
        return {
            "source": "bjrb",
            "source_name": "北京日报",
            "date": self.day.isoformat(),
            "editions": [{"no": 1, "name": "要闻"}],
            "units": [{
                "id": "bjrb_20260903_01",
                "url": base + "page.htm",
                "articles": [
                    {"title": "文章一", "url": base + "content_1.htm"},
                    {"title": "文章二", "url": base + "content_2.htm"},
                ],
                "text": "原有正文",
            }],
        }

    def test_mobile_checks_every_discovered_article_and_rejects_one_503(self):
        issue_path, original = self.write_issue("bjrb", self.mobile_issue())
        urls = [
            article["url"] for article in self.mobile_issue()["units"][0]["articles"]
        ]
        good_body = (
            '<html><body><div id="content">' + "当日正文内容。" * 12
            + "</div></body></html>"
        ).encode()

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            side_effect=[(200, urls[0], good_body), (503, urls[1], b"")],
        ) as http_get:
            _issue, error = mobile_epaper.parse(
                self.mobile_source(), self.day, str(self.archive),
                max_per_edition=1,
            )

        self.assertIsNotNone(error)
        self.assertIn("503", error)
        self.assertEqual(http_get.call_count, 2)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_mobile_fetch_rejects_navigation_above_cap_without_target_write(self):
        source = self.mobile_source()
        source["mob"]["max_pages"] = 2
        final_url = (
            "https://example.test/bjrb/mobile/2026/20260903/20260903_m.html"
        )
        navigation = "".join(
            '<div pdf_href="../20260903_%03d/page.pdf">第%d版 要闻%d</div>'
            % (number, number, number)
            for number in range(1, 4)
        ).encode()

        def fake_http_get(url, referer=None):
            del referer
            if url.endswith("20260903_m.html"):
                return 200, final_url, navigation
            return 404, url, b""

        with mock.patch.object(
            mobile_epaper.lib, "http_get", side_effect=fake_http_get
        ) as http_get:
            issue, error = mobile_epaper.fetch(
                source, self.day, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIsNotNone(error)
        self.assertIn("上限", error)
        self.assertEqual(http_get.call_count, 1)
        self.assertFalse(
            (self.archive / "bjrb" / self.day.isoformat()).exists()
        )

    def test_mobile_fetch_rejects_non_contiguous_real_edition_numbers(self):
        cases = ([1, 3], [1, 1], [2, 3])
        for numbers in cases:
            with self.subTest(numbers=numbers), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "archive"
                source = self.mobile_source()
                final_url = (
                    "https://example.test/bjrb/mobile/2026/20260903/"
                    "20260903_m.html"
                )
                navigation = "".join(
                    '<div pdf_href="../20260903_%03d/page.pdf">第%d版 要闻%d</div>'
                    % (ordinal, number, number)
                    for ordinal, number in enumerate(numbers, 1)
                ).encode()

                def fake_http_get(url, referer=None):
                    del referer
                    if url.endswith("20260903_m.html"):
                        return 200, final_url, navigation
                    return 404, url, b""

                with mock.patch.object(
                    mobile_epaper.lib, "http_get", side_effect=fake_http_get
                ) as http_get:
                    issue, error = mobile_epaper.fetch(
                        source, self.day, str(archive)
                    )

                self.assertIsNone(issue)
                self.assertIsNotNone(error)
                self.assertIn("版号", error)
                self.assertEqual(http_get.call_count, 1)
                self.assertFalse(
                    (archive / "bjrb" / self.day.isoformat()).exists()
                )

    def test_mobile_request_exception_is_returned_without_overwrite(self):
        issue = self.mobile_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        issue_path, original = self.write_issue("bjrb", issue)

        with mock.patch.object(
            mobile_epaper.lib, "http_get", side_effect=OSError("offline")
        ):
            _issue, error = mobile_epaper.parse(
                self.mobile_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("异常", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_mobile_empty_success_response_is_error_without_overwrite(self):
        issue = self.mobile_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        article_url = issue["units"][0]["articles"][0]["url"]
        issue_path, original = self.write_issue("bjrb", issue)

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, article_url, b""),
        ):
            _issue, error = mobile_epaper.parse(
                self.mobile_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("响应为空", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_mobile_empty_edition_article_list_is_error_without_overwrite(self):
        issue = self.mobile_issue()
        issue["units"][0]["articles"] = []
        issue_path, original = self.write_issue("bjrb", issue)

        with mock.patch.object(mobile_epaper.lib, "http_get") as http_get:
            _issue, error = mobile_epaper.parse(
                self.mobile_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("文章清单为空", error)
        http_get.assert_not_called()
        self.assertEqual(issue_path.read_bytes(), original)

    def test_mobile_requires_explicit_content_container_for_200_error_shell(self):
        issue = self.mobile_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        article_url = issue["units"][0]["articles"][0]["url"]
        issue_path, original = self.write_issue("bjrb", issue)
        busy_shell = (
            "<html><body><main>"
            + ("系统繁忙，请稍后再试。" * 30)
            + "</main></body></html>"
        ).encode()

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, article_url, busy_shell),
        ):
            _issue, error = mobile_epaper.parse(
                self.mobile_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("id=content", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_mobile_extracts_only_single_quoted_content_container(self):
        issue = self.mobile_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        article_url = issue["units"][0]["articles"][0]["url"]
        self.write_issue("bjrb", issue)
        article_body = "目标日建设新闻正文。" * 12
        raw = (
            "<html><body><p>容器外标记不应入库</p>"
            "<section class='story' data-kind='article' id='content'>"
            "<p>%s</p></section><footer>页脚不应入库</footer>"
            "</body></html>" % article_body
        ).encode()

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, article_url, raw),
        ):
            parsed, error = mobile_epaper.parse(
                self.mobile_source(), self.day, str(self.archive)
            )

        self.assertIsNone(error)
        text = parsed["units"][0]["articles"][0]["text"]
        self.assertIn("目标日建设新闻正文", text)
        self.assertNotIn("容器外标记", text)
        self.assertNotIn("页脚不应入库", text)

    def test_mobile_rejects_short_content_even_when_surrounding_shell_is_long(self):
        issue = self.mobile_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        article_url = issue["units"][0]["articles"][0]["url"]
        issue_path, original = self.write_issue("bjrb", issue)
        raw = (
            "<html><body><div id='content'>短文</div><footer>"
            + ("页脚与推荐内容。" * 50)
            + "</footer></body></html>"
        ).encode()

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, article_url, raw),
        ):
            _issue, error = mobile_epaper.parse(
                self.mobile_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("正文为空或过短", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_mobile_preserves_article_text_beyond_legacy_limits(self):
        issue = self.mobile_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        self.write_issue("bjrb", issue)
        article_url = issue["units"][0]["articles"][0]["url"]
        tail = "全文末尾不可丢失"
        body = "建设行业长文。" * 20000 + tail
        raw = ('<html><body><div id="content">' + body
               + "</div></body></html>").encode()

        with mock.patch.object(
            mobile_epaper.lib,
            "http_get",
            return_value=(200, article_url, raw),
        ):
            parsed, error = mobile_epaper.parse(
                self.mobile_source(), self.day, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertIn(tail, parsed["units"][0]["articles"][0]["text"])

    def cms_source(self):
        return {
            "id": "nmrb",
            "name": "农民日报",
            "cms": {"site": "https://example.test/"},
        }

    def cms_issue(self):
        base = "https://example.test/nmrb/html/2026/20260903/"
        return {
            "source": "nmrb",
            "source_name": "农民日报",
            "date": self.day.isoformat(),
            "editions": [{"no": 1, "name": "要闻"}],
            "units": [{
                "id": "nmrb_20260903_01",
                "url": base + "nmrb_20260903_1.html",
                "article_urls": [
                    base + "nmrb_20260903_1_1.html",
                    base + "nmrb_20260903_1_2.html",
                ],
                "articles": [{"title": "旧内容", "text": "必须保留"}],
                "text": "旧内容\n必须保留",
            }],
        }

    def test_cms_utf8_decode_does_not_choose_cjk_mojibake(self):
        text = '<meta charset="utf-8"><p>' + "建设新闻" * 100 + "全文尾部标记</p>"
        decoded = cms_index._best(text.encode("utf-8"))

        self.assertEqual(decoded, text)
        self.assertTrue(decoded.endswith("全文尾部标记</p>"))

    def test_cms_checks_every_discovered_article_and_rejects_one_503(self):
        issue_path, original = self.write_issue("nmrb", self.cms_issue())
        urls = self.cms_issue()["units"][0]["article_urls"]
        good_html = (
            '<html><head><title>2026年9月3日 文章一</title></head>'
            '<body><div id="ozoom"><p>' + "当日正文内容。" * 12
            + "</p></div></body></html>"
        )

        with mock.patch.object(
            cms_index,
            "_get_response",
            side_effect=[(200, urls[0], good_html), (503, urls[1], "")],
        ) as get_response:
            _issue, error = cms_index.parse(
                self.cms_source(), self.day, str(self.archive),
                max_per_edition=1,
            )

        self.assertIsNotNone(error)
        self.assertIn("503", error)
        self.assertEqual(get_response.call_count, 2)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_cms_wrong_dated_article_url_is_error_not_silent_skip(self):
        issue = self.cms_issue()
        issue["units"][0]["article_urls"] = [
            "https://example.test/nmrb/html/2026/20260902/"
            "nmrb_20260902_1_1.html"
        ]
        issue_path, original = self.write_issue("nmrb", issue)

        with mock.patch.object(cms_index, "_get_response") as get_response:
            _issue, error = cms_index.parse(
                self.cms_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("日期", error)
        get_response.assert_not_called()
        self.assertEqual(issue_path.read_bytes(), original)

    def test_cms_request_exception_is_returned_without_overwrite(self):
        issue = self.cms_issue()
        issue["units"][0]["article_urls"] = issue["units"][0]["article_urls"][:1]
        issue_path, original = self.write_issue("nmrb", issue)

        with mock.patch.object(
            cms_index, "_get_response", side_effect=OSError("offline")
        ):
            _issue, error = cms_index.parse(
                self.cms_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("异常", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_cms_preserves_all_paragraphs_beyond_legacy_limits(self):
        issue = self.cms_issue()
        issue["units"][0]["article_urls"] = issue["units"][0]["article_urls"][:1]
        self.write_issue("nmrb", issue)
        article_url = issue["units"][0]["article_urls"][0]
        tail = "全文末尾不可丢失"
        paragraphs = ["建设行业长文。" * 400 for _ in range(80)] + [tail * 2]
        article_html = (
            '<html><head><title>2026年9月3日 长篇报道</title></head>'
            '<body><div id="ozoom">'
            + "".join("<p>%s</p>" % paragraph for paragraph in paragraphs)
            + "</div></body></html>"
        )

        with mock.patch.object(
            cms_index,
            "_get_response",
            return_value=(200, article_url, article_html),
        ):
            parsed, error = cms_index.parse(
                self.cms_source(), self.day, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertIn(tail, parsed["units"][0]["articles"][0]["text"])

    def test_cms_preserves_short_paragraphs_and_untruncated_title_fields(self):
        issue = self.cms_issue()
        urls = issue["units"][0]["article_urls"]
        self.write_issue("nmrb", issue)
        long_title = "城市更新与产业协同" * 12
        responses = [
            (
                200,
                urls[0],
                '<html><body><div id="PreTitle">导语</div>'
                '<h1 id="Title">民生</h1><div id="ozoom">'
                '<p>记者</p><p>短题</p><p>这是完整的较长正文段落。</p>'
                '</div></body></html>',
            ),
            (
                200,
                urls[1],
                '<html><body><div id="PreTitle">前置标题不得抢主标题</div>'
                '<h1 id="Title">%s</h1><div id="ozoom">'
                '<p>署名</p><p>第二篇完整正文。</p>'
                '</div></body></html>' % long_title,
            ),
        ]

        with mock.patch.object(cms_index, "_get_response", side_effect=responses):
            parsed, error = cms_index.parse(
                self.cms_source(), self.day, str(self.archive)
            )

        self.assertIsNone(error)
        articles = parsed["units"][0]["articles"]
        self.assertEqual(articles[0]["title"], "民生")
        self.assertEqual(articles[0]["pretitle"], "导语")
        self.assertEqual(articles[0]["text"].splitlines(), [
            "记者", "短题", "这是完整的较长正文段落。",
        ])
        self.assertEqual(articles[1]["title"], long_title)
        self.assertEqual(articles[1]["pretitle"], "前置标题不得抢主标题")

    def test_cms_fetch_rejects_non_contiguous_or_aliased_edition_numbers(self):
        cases = (("1", "3"), ("01", "1"), ("2", "3"))
        for tokens in cases:
            with self.subTest(tokens=tokens), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "archive"
                source = self.cms_source()
                source["cms"].update({
                    "index_json": "https://example.test/index.json",
                    "paper_code": "nmrb",
                    "max_pages": 16,
                })
                page_path = (
                    "nmrb/html/2026/20260903/nmrb_20260903_13395_1.html"
                )
                index_payload = json.dumps({
                    "papers": [{
                        "paperCode": "nmrb",
                        "paperDate": self.day.isoformat(),
                        "paperIssueNum": "nmrb_20260903_13395",
                        "pagePath": page_path,
                    }],
                })
                page1_url = "https://example.test/" + page_path
                page_html = (
                    "<html><head><title>农民日报 2026年9月3日</title></head>"
                    "<body>" + "".join(
                        '<a href="nmrb_20260903_13395_%s.html">第%s版：要闻</a>'
                        % (token, token)
                        for token in tokens
                    ) + "</body></html>"
                )

                with mock.patch.object(
                    cms_index, "_get", return_value=(200, index_payload)
                ), mock.patch.object(
                    cms_index,
                    "_get_response",
                    return_value=(200, page1_url, page_html),
                ) as get_response:
                    issue, error = cms_index.fetch(
                        source, self.day, str(archive)
                    )

                self.assertIsNone(issue)
                self.assertIsNotNone(error)
                self.assertIn("版号", error)
                self.assertEqual(get_response.call_count, 1)
                self.assertFalse(
                    (archive / "nmrb" / self.day.isoformat()).exists()
                )

    def founder_source(self):
        return {
            "id": "gmrb",
            "name": "光明日报",
            "entry": "https://example.test/",
        }

    def founder_issue(self):
        base = "https://example.test/gmrb/html/2026-09/03/"
        return {
            "source": "gmrb",
            "source_name": "光明日报",
            "date": self.day.isoformat(),
            "editions": [{"no": 1, "name": "要闻", "url": base + "node_01.html"}],
            "units": [{
                "id": "gmrb_20260903_01",
                "url": base + "node_01.html",
                "articles": [
                    {"title": "文章一", "text": "", "url": base + "content_1.html"},
                    {"title": "文章二", "text": "", "url": base + "content_2.html"},
                ],
                "text": "原有正文",
            }],
        }

    def test_founder_fetch_keeps_every_discovered_article(self):
        source = self.founder_source()
        source["index_url"] = (
            "https://example.test/gmrb/html/2026-09/03/index.html"
        )
        page_url = "https://example.test/gmrb/html/2026-09/03/node_01.html"
        index_html = (
            '<a href="node_01.html">第01版 要闻</a>'
        ).encode()
        page_html = (
            "<html><body>" + "".join(
                '<a href="content_%s.html">文章%s</a>' % (number, number)
                for number in range(1, 14)
            ) + '<img id="map" src="../../../pic/202609/03/page.jpg">'
            "</body></html>"
        ).encode()

        with mock.patch.object(
            founder.lib,
            "http_get",
            side_effect=[
                (200, source["index_url"], index_html),
                (200, page_url, page_html),
                (200,
                 "https://example.test/gmrb/html/pic/202609/03/page.jpg",
                 page_jpeg()),
            ],
        ):
            issue, error = founder.fetch(
                source, self.day, str(self.archive), max_articles=1
            )

        self.assertIsNone(error)
        self.assertEqual(len(issue["units"][0]["articles"]), 13)

    def test_founder_parse_preserves_article_text_beyond_30000_characters(self):
        issue = self.founder_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        _issue_path, _original = self.write_issue("gmrb", issue)
        article_url = issue["units"][0]["articles"][0]["url"]
        long_text = "建设新闻" * 8000 + "全文尾部标记"
        article_html = (
            '<html><head><title>2026-09-03 长文</title></head>'
            '<body><div id="ozoom"><p>' + long_text + "</p></div></body></html>"
        ).encode()

        with mock.patch.object(
            founder.lib,
            "http_get",
            return_value=(200, article_url, article_html),
        ):
            parsed, error = founder.parse(
                self.founder_source(), self.day, str(self.archive), max_articles=1
            )

        self.assertIsNone(error)
        text = parsed["units"][0]["articles"][0]["text"]
        self.assertGreater(len(text), 30000)
        self.assertTrue(text.endswith("全文尾部标记"))

    def test_founder_preserves_short_byline_heading_and_long_paragraph_order(self):
        issue = self.founder_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        self.write_issue("gmrb", issue)
        article_url = issue["units"][0]["articles"][0]["url"]
        long_title = "建设投资与城市更新协同观察" * 10
        article_html = (
            '<html><head><title>2026-09-03 完整性</title></head>'
            '<body><h1>%s</h1><div id="ozoom"><p>记者</p><p>小标题</p>'
            '<p>建设投资、城市更新与产业创新共同形成完整的长段落。</p>'
            '</div></body></html>'
            % long_title
        ).encode()

        with mock.patch.object(
            founder.lib, "http_get",
            return_value=(200, article_url, article_html),
        ):
            parsed, error = founder.parse(
                self.founder_source(), self.day, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(
            parsed["units"][0]["articles"][0]["title"], long_title
        )
        self.assertEqual(
            parsed["units"][0]["articles"][0]["text"].splitlines(),
            [
                "记者",
                "小标题",
                "建设投资、城市更新与产业创新共同形成完整的长段落。",
            ],
        )

    def test_founder_long_embedded_lead_still_fetches_authoritative_detail(self):
        issue = self.founder_issue()
        article = issue["units"][0]["articles"][0]
        issue["units"][0]["articles"] = [article]
        embedded_lead = "版面嵌入长导语。" * 120
        article["text"] = embedded_lead
        issue_path, _original = self.write_issue("gmrb", issue)
        detail_tail = "详情页权威全文末尾标记"
        detail = "详情页完整正文。" * 180 + detail_tail
        article_html = (
            '<html><head><title>2026-09-03 长文</title></head>'
            '<body><div id="ozoom"><p>' + detail + "</p></div></body></html>"
        ).encode()

        with mock.patch.object(
            founder.lib,
            "http_get",
            return_value=(200, article["url"], article_html),
        ) as http_get:
            parsed, error = founder.parse(
                self.founder_source(), self.day, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(http_get.call_count, 1)
        text = parsed["units"][0]["articles"][0]["text"]
        self.assertNotEqual(text, embedded_lead)
        self.assertTrue(text.endswith(detail_tail))
        saved = json.loads(issue_path.read_text(encoding="utf-8"))
        self.assertTrue(
            saved["units"][0]["articles"][0]["text"].endswith(detail_tail)
        )

    def test_founder_long_embedded_lead_does_not_mask_detail_failure(self):
        issue = self.founder_issue()
        article = issue["units"][0]["articles"][0]
        issue["units"][0]["articles"] = [article]
        article["text"] = "版面嵌入长导语。" * 120
        issue_path, original = self.write_issue("gmrb", issue)

        with mock.patch.object(
            founder.lib,
            "http_get",
            return_value=(503, article["url"], b""),
        ) as http_get:
            parsed, error = founder.parse(
                self.founder_source(), self.day, str(self.archive)
            )

        self.assertEqual(parsed, issue)
        self.assertIn("503", error)
        self.assertEqual(http_get.call_count, 1)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_founder_checks_every_discovered_article_and_rejects_one_503(self):
        issue_path, original = self.write_issue("gmrb", self.founder_issue())
        urls = [
            article["url"] for article in self.founder_issue()["units"][0]["articles"]
        ]
        good_html = (
            '<html><head><title>2026年9月3日 文章一</title></head>'
            '<body><div id="ozoom"><p>' + "当日正文内容。" * 12
            + "</p></div></body></html>"
        ).encode()

        with mock.patch.object(
            founder.lib,
            "http_get",
            side_effect=[(200, urls[0], good_html), (503, urls[1], b"")],
        ) as http_get:
            _issue, error = founder.parse(
                self.founder_source(), self.day, str(self.archive),
                max_articles=1,
            )

        self.assertIsNotNone(error)
        self.assertIn("503", error)
        self.assertEqual(http_get.call_count, 2)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_founder_request_exception_is_error_and_preserves_issue(self):
        issue = self.founder_issue()
        issue["units"][0]["articles"] = issue["units"][0]["articles"][:1]
        issue_path, original = self.write_issue("gmrb", issue)

        with mock.patch.object(
            founder.lib, "http_get", side_effect=OSError("offline")
        ):
            _issue, error = founder.parse(
                self.founder_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("异常", error)
        self.assertEqual(issue_path.read_bytes(), original)

    def test_founder_wrong_dated_article_url_is_error_not_silent_skip(self):
        issue = self.founder_issue()
        issue["units"][0]["articles"] = [{
            "title": "旧日文章",
            "text": "",
            "url": (
                "https://example.test/gmrb/html/2026-09/02/content_1.html"
            ),
        }]
        issue_path, original = self.write_issue("gmrb", issue)

        with mock.patch.object(founder.lib, "http_get") as http_get:
            _issue, error = founder.parse(
                self.founder_source(), self.day, str(self.archive)
            )

        self.assertIsNotNone(error)
        self.assertIn("日期", error)
        http_get.assert_not_called()
        self.assertEqual(issue_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
