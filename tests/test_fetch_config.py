import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(FETCH_SCRIPTS))


def load_fetch():
    spec = importlib.util.spec_from_file_location("readdaily_fetch", FETCH_SCRIPTS / "fetch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetch = load_fetch()


def load_paper_api():
    spec = importlib.util.spec_from_file_location(
        "readdaily_paper_api",
        FETCH_SCRIPTS / "adapters" / "paper_api.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


paper_api = load_paper_api()


class FetchConfigurationTests(unittest.TestCase):
    def test_environment_archive_overrides_registry_for_app_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            configured = str(Path(td) / "isolated-archive")
            registry = {"archive_root": "/must/not/be/used"}
            with mock.patch.dict(os.environ, {"READDAILY_ARCHIVE": configured}):
                self.assertEqual(fetch.resolve_archive_root(registry), configured)

    def test_registry_archive_is_used_without_environment_override(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                fetch.resolve_archive_root({"archive_root": "~/newspaper-fixture"}),
                str(Path.home() / "newspaper-fixture"),
            )

    def test_public_registry_has_no_developer_home_paths(self):
        registry_path = ROOT / "skills" / "newspaper-fetch" / "sources.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        serialized = json.dumps(registry, ensure_ascii=False)
        self.assertNotIn("/Users/guopeijun", serialized)
        self.assertIs(fetch.validate_registry(registry), registry)

    def test_registry_rejects_unsafe_duplicate_or_unknown_source_identity(self):
        valid = {
            "archive_root": "/tmp/readdaily-archive",
            "sources": [{
                "id": "paper_1",
                "name": "示例报纸",
                "channel": "founder",
                "enabled": True,
            }],
        }
        self.assertIs(fetch.validate_registry(valid), valid)

        invalid_registries = (
            {"sources": [{
                "id": "../vault", "name": "越界", "channel": "founder",
            }]},
            {"sources": [{
                "id": "/tmp/vault", "name": "绝对路径", "channel": "founder",
            }]},
            {"sources": [{
                "id": "中文", "name": "非 ASCII", "channel": "founder",
            }]},
            {"sources": [
                {"id": "same", "name": "甲", "channel": "founder"},
                {"id": "same", "name": "乙", "channel": "paper_api"},
            ]},
            {"sources": [{
                "id": "paper", "name": "未知渠道", "channel": "../adapter",
            }]},
            {"sources": [{
                "id": "paper", "name": "", "channel": "founder",
            }]},
            {"sources": [{
                "id": "paper", "name": "示例", "channel": "founder",
                "enabled": "yes",
            }]},
            {"sources": []},
            {"sources": "not-a-list"},
        )
        for registry in invalid_registries:
            with self.subTest(registry=registry):
                with self.assertRaises(ValueError):
                    fetch.validate_registry(registry)

    def test_paper_api_uses_shared_thread_local_http_post(self):
        source = {
            "entry": "https://example.test/",
            "api": {"base": "https://example.test/api"},
        }
        with mock.patch.object(
                paper_api.lib,
                "http_post_json",
                return_value=(
                    200,
                    "https://example.test/api/period",
                    b'{"obj":{"editionList":[]}}',
                ),
        ) as posted:
            result = paper_api._api(source, "/period", {"date": "2026-09-03"})

        self.assertEqual(result, {"obj": {"editionList": []}})
        self.assertEqual(posted.call_args.args[:2], (
            "https://example.test/api/period",
            {"date": "2026-09-03"},
        ))
        self.assertEqual(
            posted.call_args.kwargs["timeout"], 20
        )

    def test_wechat_engine_does_not_require_pillow(self):
        engine = (FETCH_SCRIPTS / "wechat_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("from PIL import Image", engine)

    def test_cli_rejects_empty_or_unknown_stage_and_source(self):
        registry = {
            "archive_root": "/unused/archive",
            "sources": [{
                "id": "known", "name": "已知报纸",
                "channel": "founder", "enabled": True,
            }],
        }
        invalid_argv = (
            (["fetch.py", "--stage", ""], "--stage"),
            (["fetch.py", "--stage", "summarized"], "summarized"),
            (["fetch.py", "--source", "unknown"], "未知来源 id"),
            (["fetch.py", "--source", "known,"], "--source"),
        )
        for argv, expected in invalid_argv:
            with self.subTest(argv=argv), mock.patch.object(
                    sys, "argv", argv), mock.patch.object(
                        fetch, "load_registry", return_value=registry
                    ), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    fetch.main()
            self.assertNotEqual(caught.exception.code, 0)
            self.assertIn(expected, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
