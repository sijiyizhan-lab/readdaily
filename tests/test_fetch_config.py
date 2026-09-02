import importlib.util
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


if __name__ == "__main__":
    unittest.main()
