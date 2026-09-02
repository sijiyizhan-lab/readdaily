import sys
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FETCH_SCRIPTS = ROOT / "skills" / "newspaper-fetch" / "scripts"
sys.path.insert(0, str(FETCH_SCRIPTS))

import lib  # noqa: E402
from tests.image_fixtures import page_png  # noqa: E402


def jpeg_shaped_garbage(width=1280, height=1823, size=60000):
    """Return a structurally plausible JPEG whose scan is not JPEG data."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    sos = b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    prefix = b"\xff\xd8" + app0 + sof0 + sos
    return prefix + b"x" * max(0, size - len(prefix) - 2) + b"\xff\xd9"


class ImageValidationTests(unittest.TestCase):
    def test_rejects_jpeg_shaped_garbage_that_has_sof_sos_and_eoi(self):
        raw = jpeg_shaped_garbage()

        self.assertEqual(lib.image_dimensions(raw), (1280, 1823))
        self.assertIsNotNone(lib.image_validation_error(raw, min_bytes=50000))

    def test_accepts_a_real_decodable_image(self):
        raw = page_png(width=1280, height=1823, min_bytes=60000)

        self.assertEqual(lib.image_dimensions(raw), (1280, 1823))
        self.assertIsNone(lib.image_validation_error(raw, min_bytes=50000))

    def test_rejects_decodable_oversized_payload_with_thumbnail_dimensions(self):
        raw = page_png(width=32, height=32, min_bytes=60000)

        error = lib.image_validation_error(raw, min_bytes=50000)

        self.assertIn("尺寸过小", error)
        self.assertIn("32x32", error)

    def test_stdlib_png_fallback_inflates_pixels_when_no_decoder_is_installed(self):
        raw = page_png(width=1280, height=1823, min_bytes=60000)

        with mock.patch.object(lib.sys, "platform", "linux"), \
                mock.patch.object(lib.shutil, "which", return_value=None):
            self.assertIsNone(lib.image_validation_error(raw, min_bytes=1))

    def test_non_macos_fallback_fails_closed_for_jpeg_without_a_decoder(self):
        raw = jpeg_shaped_garbage()

        with mock.patch.object(lib.sys, "platform", "linux"), \
                mock.patch.object(lib.shutil, "which", return_value=None):
            error = lib.image_validation_error(raw, min_bytes=50000)

        self.assertIn("缺少可用图片解码器", error)


if __name__ == "__main__":
    unittest.main()
