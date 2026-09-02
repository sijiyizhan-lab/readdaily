import datetime
import importlib.util
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
    path = FETCH_SCRIPTS / "adapters" / "paper_api.py"
    spec = importlib.util.spec_from_file_location(
        "paper_api_date_test_adapter", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


paper_api = load_adapter()


def page_jpeg(size=60000, fill=b"x"):
    # 科技日报真实黄金样本为 1000x1417，必须通过公共版面门槛。
    return page_png(
        width=1000,
        height=1417,
        min_bytes=size,
        fill=(fill or b"x")[:1],
    )


class PaperAPIDateValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self.tmp.name) / "archive"
        self.requested = datetime.date(2026, 9, 3)
        self.source = {
            "id": "kjrb",
            "name": "科技日报",
            "entry": "https://example.test/",
            "api": {"base": "https://example.test/api", "code": "KJRB"},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def target_dir(self):
        return self.archive / "kjrb" / self.requested.isoformat()

    @staticmethod
    def response(period_time, image_day="2026-09/03"):
        return {
            "obj": {
                "periodTime": period_time,
                "editionList": [{
                    "id": "edition-1",
                    "periodId": "period-1",
                    "editionName": "第01版：今日要闻",
                    "editionImg": (
                        "https://img.example.test/edition/%s/KJRB/page.jpg"
                        % image_day
                    ),
                }],
            },
        }

    def test_fetch_rejects_api_fallback_to_previous_period_without_writing(self):
        with mock.patch.object(
            paper_api,
            "_api",
            return_value=self.response("2026-09-02", "2026-09/02"),
        ), mock.patch.object(paper_api.lib, "http_get") as http_get:
            issue, error = paper_api.fetch(
                self.source, self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        http_get.assert_not_called()
        self.assertFalse(self.target_dir().exists())

    def test_fetch_rejects_missing_period_date_and_mismatched_image_date(self):
        without_period = self.response(None)
        with mock.patch.object(paper_api, "_api", return_value=without_period):
            issue, error = paper_api.fetch(
                self.source, self.requested, str(self.archive)
            )
        self.assertIsNone(issue)
        self.assertIn("确认", error)

        wrong_image = self.response("2026-09-03", "2026-09/02")
        with mock.patch.object(paper_api, "_api", return_value=wrong_image), \
                mock.patch.object(paper_api.lib, "http_get") as http_get:
            issue, error = paper_api.fetch(
                self.source, self.requested, str(self.archive)
            )
        self.assertIsNone(issue)
        self.assertIn("日期", error)
        http_get.assert_not_called()
        self.assertFalse(self.target_dir().exists())

    def test_fetch_accepts_exact_period_and_image_dates(self):
        response = self.response("2026-09-03")
        image_url = response["obj"]["editionList"][0]["editionImg"]
        with mock.patch.object(paper_api, "_api", return_value=response), \
                mock.patch.object(
                    paper_api.lib,
                    "http_get",
                    return_value=(200, image_url, page_jpeg()),
                ):
            issue, error = paper_api.fetch(
                self.source, self.requested, str(self.archive)
            )

        self.assertIsNone(error)
        self.assertEqual(issue["date"], "2026-09-03")
        self.assertEqual(issue["period_time"], "2026-09-03")
        self.assertEqual(issue["editions"][0]["page_image_width"], 1000)
        self.assertEqual(issue["editions"][0]["page_image_height"], 1417)
        self.assertEqual(len(issue["editions"][0]["page_image_sha256"]), 64)
        self.assertEqual(
            issue["units"][0]["page_image_sha256"],
            issue["editions"][0]["page_image_sha256"],
        )
        self.assertTrue((self.target_dir() / "issue.json").is_file())

    def test_fetch_rejects_decodable_thumbnail_before_creating_issue_tree(self):
        response = self.response("2026-09-03")
        image_url = response["obj"]["editionList"][0]["editionImg"]
        thumbnail = page_png(width=32, height=32, min_bytes=60000)

        with mock.patch.object(paper_api, "_api", return_value=response), \
                mock.patch.object(
                    paper_api.lib,
                    "http_get",
                    return_value=(200, image_url, thumbnail),
                ):
            issue, error = paper_api.fetch(
                self.source, self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("尺寸过小", error)
        self.assertFalse(self.target_dir().exists())

    def test_fetch_rejects_any_edition_without_page_image_before_writing(self):
        response = self.response("2026-09-03")
        response["obj"]["editionList"][0].pop("editionImg")
        with mock.patch.object(paper_api, "_api", return_value=response), \
                mock.patch.object(paper_api.lib, "http_get") as http_get:
            issue, error = paper_api.fetch(
                self.source, self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("缺少版面图", error)
        http_get.assert_not_called()
        self.assertFalse(self.target_dir().exists())

    def test_fetch_commit_failure_preserves_existing_issue_tree(self):
        target = self.target_dir()
        (target / "pages").mkdir(parents=True)
        old_issue = b'{"version":"old"}'
        old_page = b"old-page"
        (target / "issue.json").write_bytes(old_issue)
        (target / "pages" / "old.jpg").write_bytes(old_page)
        response = self.response("2026-09-03")
        image_url = response["obj"]["editionList"][0]["editionImg"]

        with mock.patch.object(paper_api, "_api", return_value=response), \
                mock.patch.object(
                    paper_api.lib,
                    "http_get",
                    return_value=(200, image_url, page_jpeg()),
                ), mock.patch.object(
                    paper_api.lib,
                    "commit_issue_tree",
                    side_effect=OSError("disk full"),
                ):
            issue, error = paper_api.fetch(
                self.source, self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("事务", error)
        self.assertEqual((target / "issue.json").read_bytes(), old_issue)
        self.assertEqual((target / "pages" / "old.jpg").read_bytes(), old_page)
        self.assertEqual(
            sorted(str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()),
            ["issue.json", "pages/old.jpg"],
        )

    def test_fetch_rejects_image_redirect_to_previous_date_without_writing(self):
        response = self.response("2026-09-03")
        previous_image = (
            "https://img.example.test/edition/2026-09/02/KJRB/page.jpg"
        )
        with mock.patch.object(paper_api, "_api", return_value=response), \
                mock.patch.object(
                    paper_api.lib,
                    "http_get",
                    return_value=(200, previous_image, page_jpeg()),
                ):
            issue, error = paper_api.fetch(
                self.source, self.requested, str(self.archive)
            )

        self.assertIsNone(issue)
        self.assertIn("日期", error)
        self.assertFalse(self.target_dir().exists())


if __name__ == "__main__":
    unittest.main()
