#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""newspaper-fetch 公共库：HTTP/状态机/校验/日志/路径。"""
import datetime
import contextlib
import errno
import glob
import hashlib
from html.parser import HTMLParser
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
STAGES = ["fetched", "parsed", "summarized", "archived", "tracked"]
SOURCE_ID_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?\Z", re.ASCII
)


class _TagAttributeCollector(HTMLParser):
    def __init__(self, tag_name):
        super().__init__(convert_charrefs=True)
        self.tag_name = str(tag_name).lower()
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == self.tag_name:
            self.rows.append({str(key).lower(): value for key, value in attrs})


def html_tag_attributes(document, tag_name):
    """Return normalized attributes for every matching start tag."""
    if not isinstance(document, str) or not document:
        return []
    collector = _TagAttributeCollector(tag_name)
    try:
        collector.feed(document)
        collector.close()
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001
        return []
    return collector.rows

_HTTP_CONTEXT = threading.local()


def close_http_client():
    """Release the current thread's client at a newspaper boundary.

    A worker thread may execute more than one newspaper sequentially.  The
    connection pool is deliberately reused during one source's fetch/parse
    transaction, then discarded so cookies and authentication state cannot
    leak into the next source scheduled on that worker.
    """
    client = getattr(_HTTP_CONTEXT, "client", None)
    if client is None:
        return
    try:
        delattr(_HTTP_CONTEXT, "client")
    except AttributeError:  # pragma: no cover - defensive thread-local race
        pass
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - cleanup must not mask source result
            pass

try:
    import requests

    def _http_client():
        """Return one connection pool per worker thread.

        ``requests.Session`` owns mutable cookies and connection-pool state.
        Sharing one instance across the bounded source executor made unrelated
        newspaper requests contend on (and mutate) the same client.  A
        thread-local session preserves keep-alive while preventing concurrently
        running sources from sharing mutable transport state.
        """
        client = getattr(_HTTP_CONTEXT, "client", None)
        if client is None:
            client = requests.Session()
            client.headers["User-Agent"] = UA
            _HTTP_CONTEXT.client = client
        return client

    def http_get(url, referer=None, timeout=30, cookies=None):
        h = {"Referer": referer} if referer else {}
        r = _http_client().get(
            url, headers=h, timeout=timeout, cookies=cookies
        )
        return r.status_code, r.url, r.content

    def http_post_json(url, data, headers=None, timeout=30):
        r = _http_client().post(
            url, json=data, headers=dict(headers or {}), timeout=timeout
        )
        return r.status_code, r.url, r.content
except ImportError:  # pragma: no cover
    import http.cookiejar
    import urllib.request

    def _http_client():
        """Return one cookie-aware stdlib opener per worker thread."""
        client = getattr(_HTTP_CONTEXT, "client", None)
        if client is None:
            cookie_jar = http.cookiejar.CookieJar()
            client = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cookie_jar)
            )
            client.addheaders = [("User-Agent", UA)]
            _HTTP_CONTEXT.client = client
        return client

    def http_get(url, referer=None, timeout=30, cookies=None):
        h = {"Referer": referer} if referer else {}
        req = urllib.request.Request(url, headers=h)
        with _http_client().open(req, timeout=timeout) as resp:
            return resp.status, resp.geturl(), resp.read()

    def http_post_json(url, data, headers=None, timeout=30):
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(dict(headers or {}))
        req = urllib.request.Request(
            url,
            data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        with _http_client().open(req, timeout=timeout) as resp:
            return resp.status, resp.geturl(), resp.read()


def html_text(raw, enc_candidates=("utf-8", "gb18030", "gbk")):
    """按候选编码解码 HTML。"""
    for enc in enc_candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def detect_image_format(raw):
    """Return a trusted image format from file magic, or ``None``.

    Newspaper endpoints sometimes answer HTTP 200 with a large HTML/WAF page.
    File extensions and byte length therefore cannot prove that a downloaded
    page is an image.  Keep this dependency-free so the packaged app can run
    with the macOS system Python.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None
    data = bytes(raw)
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n") or data.startswith(
        b"\xff\x4f\xff\x51"
    ):
        return "jpeg2000"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {
        b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx", b"mif1"
    }:
        return "heif"
    return None


def image_dimensions(raw):
    """Read dimensions from a structurally complete common web image."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None
    data = bytes(raw)
    image_format = detect_image_format(data)
    if image_format == "jpeg":
        if not data.endswith(b"\xff\xd9"):
            return None
        sof_markers = {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }
        offset = 2
        dimensions = None
        saw_scan = False
        while offset < len(data):
            if data[offset] != 0xFF:
                return None
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                return None
            marker = data[offset]
            offset += 1
            if marker == 0xD9:
                break
            if marker == 0xDA:
                saw_scan = True
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[offset:offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(data):
                return None
            if marker in sof_markers:
                if segment_length < 8:
                    return None
                height = int.from_bytes(data[offset + 3:offset + 5], "big")
                width = int.from_bytes(data[offset + 5:offset + 7], "big")
                if width <= 0 or height <= 0:
                    return None
                dimensions = (width, height)
            offset += segment_length
        return dimensions if dimensions and saw_scan else None
    if image_format == "png":
        iend = b"\x00\x00\x00\x00IEND\xaeB`\x82"
        if (len(data) < 33 or data[8:12] != b"\x00\x00\x00\r"
                or data[12:16] != b"IHDR" or not data.endswith(iend)):
            return None
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return (width, height) if width > 0 and height > 0 else None
    if image_format == "gif":
        if len(data) < 14 or not data.endswith(b";"):
            return None
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return (width, height) if width > 0 and height > 0 else None
    if image_format == "webp":
        if int.from_bytes(data[4:8], "little") + 8 != len(data):
            return None
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return (width, height) if width > 0 and height > 0 else None
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


_IMAGE_SUFFIXES = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "tiff": ".tiff",
    "bmp": ".bmp",
    "jpeg2000": ".jp2",
    "heif": ".heic",
}
_IMAGE_DECODE_TIMEOUT_SECONDS = 20
_MAX_FALLBACK_RASTER_BYTES = 256 * 1024 * 1024
MIN_PAGE_SHORT_EDGE = 1000
MIN_PAGE_LONG_EDGE = 1400


def _png_raster_error(raw):
    """Fully inflate and validate a PNG raster using only the stdlib.

    This is the fail-safe fallback for hosts without a native image decoder.
    It intentionally supports the standard PNG color/bit-depth combinations
    and both non-interlaced and Adam7 images, while rejecting malformed chunk
    CRCs, compressed-data bombs, invalid scan filters, and trailing data.
    """
    data = bytes(raw)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG 签名无效"

    offset = 8
    ihdr = None
    idat = []
    saw_idat = False
    idat_ended = False
    saw_iend = False
    saw_plte = False
    while offset < len(data):
        if offset + 12 > len(data):
            return "PNG 数据块被截断"
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return "PNG 数据块长度越界"
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = int.from_bytes(
            data[offset + 8 + length:chunk_end], "big"
        )
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(payload, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return "PNG 数据块校验和无效"

        if ihdr is None and chunk_type != b"IHDR":
            return "PNG 首个数据块不是 IHDR"
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                return "PNG IHDR 无效或重复"
            ihdr = payload
        elif chunk_type == b"PLTE":
            if saw_idat or length == 0 or length % 3 or length > 768:
                return "PNG 调色板无效"
            saw_plte = True
        elif chunk_type == b"IDAT":
            if idat_ended:
                return "PNG IDAT 数据块不连续"
            saw_idat = True
            idat.append(payload)
        elif chunk_type == b"IEND":
            if length != 0 or not saw_idat or chunk_end != len(data):
                return "PNG IEND 或结束边界无效"
            saw_iend = True
            offset = chunk_end
            break
        else:
            if saw_idat:
                idat_ended = True
            # Unknown critical chunks cannot be decoded safely.  Ancillary
            # chunks (lower-case first letter) may be ignored by PNG readers.
            if chunk_type[:1].isupper():
                return "PNG 包含不支持的关键数据块"
        offset = chunk_end

    if ihdr is None or not saw_iend or offset != len(data):
        return "PNG 缺少完整 IHDR、IDAT 或 IEND"

    width = int.from_bytes(ihdr[0:4], "big")
    height = int.from_bytes(ihdr[4:8], "big")
    bit_depth = ihdr[8]
    color_type = ihdr[9]
    compression, filtering, interlace = ihdr[10], ihdr[11], ihdr[12]
    valid_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if (width <= 0 or height <= 0 or color_type not in valid_depths
            or bit_depth not in valid_depths[color_type]
            or compression != 0 or filtering != 0 or interlace not in (0, 1)):
        return "PNG IHDR 参数不受支持"
    if color_type == 3 and not saw_plte:
        return "索引色 PNG 缺少调色板"

    bits_per_pixel = channels[color_type] * bit_depth

    def pass_shape(x_start, y_start, x_step, y_step):
        pass_width = 0 if width <= x_start else (
            (width - x_start + x_step - 1) // x_step
        )
        pass_height = 0 if height <= y_start else (
            (height - y_start + y_step - 1) // y_step
        )
        return pass_width, pass_height

    if interlace == 0:
        passes = [(width, height)]
    else:
        passes = [
            pass_shape(0, 0, 8, 8),
            pass_shape(4, 0, 8, 8),
            pass_shape(0, 4, 4, 8),
            pass_shape(2, 0, 4, 4),
            pass_shape(0, 2, 2, 4),
            pass_shape(1, 0, 2, 2),
            pass_shape(0, 1, 1, 2),
        ]
    expected_size = sum(
        pass_height * (1 + ((pass_width * bits_per_pixel + 7) // 8))
        for pass_width, pass_height in passes
        if pass_width and pass_height
    )
    if expected_size <= 0 or expected_size > _MAX_FALLBACK_RASTER_BYTES:
        return "PNG 解码后尺寸过大，拒绝安全验收"

    inflater = zlib.decompressobj()
    try:
        decoded = inflater.decompress(b"".join(idat), expected_size + 1)
        if inflater.unconsumed_tail or len(decoded) > expected_size:
            return "PNG 解码数据超过声明尺寸"
        decoded += inflater.flush()
    except zlib.error:
        return "PNG 像素数据无法解压"
    if (not inflater.eof or inflater.unused_data or len(decoded) != expected_size):
        return "PNG 解码数据与声明尺寸不一致"

    cursor = 0
    for pass_width, pass_height in passes:
        if not pass_width or not pass_height:
            continue
        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        for _row in range(pass_height):
            if decoded[cursor] > 4:
                return "PNG 扫描行滤镜无效"
            cursor += 1 + row_bytes
    if cursor != len(decoded):
        return "PNG 扫描行边界无效"
    return None


def _decode_with_sips(raw, image_format):
    """Force ImageIO to decode an image into a one-pixel PNG on macOS."""
    with tempfile.TemporaryDirectory(prefix="readdaily-image-decode-") as work:
        source = os.path.join(work, "source" + _IMAGE_SUFFIXES[image_format])
        output = os.path.join(work, "decoded.png")
        with open(source, "wb") as stream:
            stream.write(bytes(raw))
        try:
            result = subprocess.run(
                [
                    "/usr/bin/sips", "--resampleHeightWidth", "1", "1",
                    "-s", "format", "png", source, "--out", output,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_IMAGE_DECODE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "macOS ImageIO 解码器执行失败"
        if result.returncode != 0 or not os.path.isfile(output):
            return "macOS ImageIO 无法解码"
        try:
            with open(output, "rb") as stream:
                decoded = stream.read()
        except OSError:
            return "macOS ImageIO 未生成可读结果"
        if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
            return "macOS ImageIO 未生成有效预览"
    return None


def _decode_with_ffmpeg(raw, image_format, executable):
    """Decode one frame with ffmpeg when no platform image API is available."""
    with tempfile.TemporaryDirectory(prefix="readdaily-image-decode-") as work:
        source = os.path.join(work, "source" + _IMAGE_SUFFIXES[image_format])
        with open(source, "wb") as stream:
            stream.write(bytes(raw))
        try:
            result = subprocess.run(
                [
                    executable, "-nostdin", "-v", "error", "-xerror",
                    "-err_detect", "explode", "-i", source,
                    "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_IMAGE_DECODE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "ffmpeg 图片解码器执行失败"
        if result.returncode != 0:
            return "ffmpeg 无法完整解码"
    return None


def _image_decode_error(raw, image_format):
    """Return an error unless pixels can be decoded by a trusted local path."""
    if sys.platform == "darwin" and os.path.isfile("/usr/bin/sips"):
        return _decode_with_sips(raw, image_format)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return _decode_with_ffmpeg(raw, image_format, ffmpeg)
    if image_format == "png":
        return _png_raster_error(raw)
    return "当前平台缺少可用图片解码器，无法安全验收"


def validate_page_image(
        raw,
        min_bytes=1,
        min_short_edge=MIN_PAGE_SHORT_EDGE,
        min_long_edge=MIN_PAGE_LONG_EDGE):
    """Validate and describe a previewable, full-size newspaper page image.

    Edge checks are orientation-independent: portrait and landscape pages both
    need a short edge of at least 1000 px and a long edge of at least 1400 px.
    Returning the accepted metadata from the same check keeps adapters from
    recording dimensions or hashes for bytes that were never actually decoded.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None, "响应不是二进制图片"
    data = bytes(raw)
    if len(data) < int(min_bytes):
        return None, "响应过小，可能不完整"
    image_format = detect_image_format(data)
    if image_format is None:
        return None, "响应不是可识别的图片，可能是拦截或错误页面"
    dimensions = image_dimensions(data)
    if dimensions is None:
        return None, "%s 结构、尺寸或结束边界无效，图片可能被截断" % image_format
    width, height = dimensions
    short_edge, long_edge = sorted((width, height))
    if short_edge < int(min_short_edge) or long_edge < int(min_long_edge):
        return None, (
            "尺寸过小：%sx%s；版面原图最短边须至少 %s px、最长边须至少 %s px"
            % (width, height, min_short_edge, min_long_edge)
        )
    decode_error = _image_decode_error(data, image_format)
    if decode_error:
        return None, "%s 无法实际解码：%s" % (image_format, decode_error)
    return {
        "format": image_format,
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(data).hexdigest(),
    }, None


def image_validation_error(
        raw,
        min_bytes=1,
        min_short_edge=MIN_PAGE_SHORT_EDGE,
        min_long_edge=MIN_PAGE_LONG_EDGE):
    """Explain why a response cannot be accepted as a complete page image."""
    _metadata, error = validate_page_image(
        raw,
        min_bytes=min_bytes,
        min_short_edge=min_short_edge,
        min_long_edge=min_long_edge,
    )
    return error


def norm_day(d):
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    return datetime.datetime.strptime(str(d), "%Y-%m-%d").date()


def validate_source_id(source_id):
    """Return a filesystem-safe registry id or reject it before path joining."""
    if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError(
            "来源 id 必须是 1–64 位小写 ASCII slug（字母、数字、_、-），"
            "且首尾必须为字母或数字"
        )
    return source_id


def archive_paths(archive_root, source_id, d):
    d = norm_day(d)
    source_id = validate_source_id(source_id)
    root = os.path.expanduser(archive_root)
    issue_dir = os.path.join(root, source_id, d.isoformat())
    return {
        "root": root,
        "dir": issue_dir,
        "pages": os.path.join(issue_dir, "pages"),
        "text": os.path.join(issue_dir, "text"),
        "issue_json": os.path.join(issue_dir, "issue.json"),
        "state": os.path.join(root, "_state", source_id, f"{d.isoformat()}.json"),
        "summaries": os.path.join(root, "_summaries", source_id, f"{d.isoformat()}.json"),
    }


class ArchivePathSafetyError(RuntimeError):
    """A configured archive path can no longer be resolved without links."""


class ArchiveConflictError(RuntimeError):
    """An opened archive or one of its ancestors changed during an operation."""


class ArchiveTransactionError(RuntimeError):
    """An archive commit/rollback/cleanup could not finish durably."""


ARCHIVE_FATAL_EXCEPTIONS = (
    ArchivePathSafetyError,
    ArchiveConflictError,
    ArchiveTransactionError,
)
PIPELINE_FATAL_EXCEPTIONS = ARCHIVE_FATAL_EXCEPTIONS + (MemoryError,)


def open_lock_file_at(directory_fd, name, mode=0o600):
    """Create or open one lock file without following links.

    macOS can return ``ENOENT`` when two processes concurrently call
    ``open(O_CREAT | O_NOFOLLOW)`` for the same previously-missing entry.
    An exclusive create followed by a no-follow open avoids that kernel race
    while retaining the fail-closed symlink boundary.
    """
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _attempt in range(8):
        try:
            return os.open(
                name, flags | os.O_CREAT | os.O_EXCL, mode,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            try:
                return os.open(name, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                # The entry changed between the create and open attempts.
                # Retry a bounded number of times, then let the caller map the
                # unstable path to its typed fatal lock error.
                continue
    raise FileNotFoundError(
        errno.ENOENT, "锁文件在创建期间反复变化", name
    )


def _combined_archive_cleanup_error(message, primary_error, cleanup_error):
    """Keep archive cleanup failures fatal without losing the first failure."""
    error = ArchiveTransactionError(message)
    error.primary_error = primary_error
    error.cleanup_error = cleanup_error
    return error


def _cleanup_archive_temporary(
        parent_fd, parent_display, name, *, descriptor=-1,
        directory=False, description="归档暂存项"):
    """Remove one temporary entry and durably record its removal.

    Callers invoke this from ``finally`` blocks.  Every ordinary cleanup I/O
    error is converted to the archive-fatal transaction type so it cannot
    replace an earlier safety conflict and later be treated as a source miss.
    """
    first_error = None
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except Exception as exc:  # noqa: BLE001 - normalized below
            first_error = exc
    removed = False
    try:
        if directory:
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
        removed = True
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - normalized below
        if first_error is None:
            first_error = exc
    if removed:
        try:
            with _pinned_directory(parent_display, parent_fd):
                fsync_directory(parent_display)
        except Exception as exc:  # noqa: BLE001 - normalized below
            if first_error is None:
                first_error = exc
    if first_error is not None:
        if isinstance(first_error, ARCHIVE_FATAL_EXCEPTIONS):
            raise first_error
        raise ArchiveTransactionError(
            "%s未完成耐久清理" % description
        ) from first_error


def _close_archive_descriptor(
        descriptor, description, primary_error=None):
    """Close an archive fd without allowing raw cleanup I/O to mask safety."""
    try:
        os.close(descriptor)
    except Exception as cleanup_error:  # noqa: BLE001 - normalized below
        if primary_error is not None:
            raise _combined_archive_cleanup_error(
                "%s失败，且文件描述符清理未完成" % description,
                primary_error,
                cleanup_error,
            ) from primary_error
        if isinstance(cleanup_error, ARCHIVE_FATAL_EXCEPTIONS):
            raise
        raise ArchiveTransactionError(
            "%s文件描述符清理未完成" % description
        ) from cleanup_error


_ARCHIVE_CONTEXT = threading.local()
_PINNED_DIRECTORY_CONTEXT = threading.local()
_LOG_THREAD_LOCKS = {}
_LOG_THREAD_LOCKS_GUARD = threading.Lock()
_CONSOLE_PRINT_LOCK = threading.RLock()
_SYSTEM_TEMP_ROOT = "/private/tmp" if sys.platform == "darwin" else "/tmp"
READDAILY_USER_LOCK_ROOT = os.path.join(
    _SYSTEM_TEMP_ROOT, "readdaily-%s" % os.geteuid()
)
READDAILY_LOCK_ROOT = os.path.join(
    READDAILY_USER_LOCK_ROOT, "locks"
)
SOURCE_EVIDENCE_LOCK_ROOT = os.path.join(
    READDAILY_LOCK_ROOT, "source-evidence"
)
FETCH_BATCH_LOCK_ROOT = os.path.join(READDAILY_LOCK_ROOT, "fetch-batches")


def console_print(*values, **kwargs):
    """Emit one complete console record across concurrent source workers."""
    with _CONSOLE_PRINT_LOCK:
        print(*values, **kwargs)


def _absolute_path(path):
    absolute = os.path.abspath(os.path.expanduser(os.fspath(path) or "."))
    # macOS exposes these stable system aliases as symlinks. Normalize only
    # those platform-owned prefixes; never realpath a user-controlled archive
    # path, because doing so would trust a link swapped in after validation.
    if sys.platform == "darwin":
        for alias, canonical in (
                ("/var", "/private/var"),
                ("/tmp", "/private/tmp"),
                ("/etc", "/private/etc"),
        ):
            if absolute == alias or absolute.startswith(alias + os.sep):
                return canonical + absolute[len(alias):]
    return absolute


def _directory_open_flags():
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _same_inode(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_verified_at(parent_fd, name, flags, before, description):
    """Open a previously-lstat'd entry and prove its identity did not change."""
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ArchiveConflictError(
            "%s在安全打开时被移走或替换" % description
        ) from exc
    try:
        opened = os.fstat(descriptor)
    except OSError as exc:
        primary_error = ArchiveConflictError(
            "%s在校验打开身份时失效" % description
        )
        _close_archive_descriptor(
            descriptor, description, primary_error=primary_error
        )
        raise primary_error from exc
    if not _same_inode(before, opened):
        primary_error = ArchiveConflictError(
            "%s在打开时发生变化" % description
        )
        _close_archive_descriptor(
            descriptor, description, primary_error=primary_error
        )
        raise primary_error
    return descriptor


@contextlib.contextmanager
def _pinned_directory(path, descriptor):
    """Let observable fsync helpers use an already-open directory safely."""
    stack = getattr(_PINNED_DIRECTORY_CONTEXT, "stack", None)
    if stack is None:
        stack = []
        _PINNED_DIRECTORY_CONTEXT.stack = stack
    stack.append((_absolute_path(path), descriptor))
    try:
        yield
    finally:
        stack.pop()


def _pinned_directory_fd(path):
    wanted = _absolute_path(path)
    for candidate, descriptor in reversed(
            getattr(_PINNED_DIRECTORY_CONTEXT, "stack", ())):
        if candidate == wanted:
            return descriptor
    return None


class ArchiveSession:
    """Pin an archive root and mutate it only through ``*at`` syscalls.

    The session retains descriptors for the canonical root and every existing
    ancestor.  All descendants are opened with ``O_NOFOLLOW`` relative to the
    retained descriptor.  Renaming any ancestor (including replacing the root
    with a symlink) therefore cannot redirect writes; identity checks turn the
    operation into an explicit conflict instead.
    """

    def __init__(self, root, create=False, mode=0o777):
        self.configured_root = _absolute_path(root)
        self.canonical_root = None
        self._root_fd = None
        self._root_chain = []
        self.created_paths = []
        self._closed = False
        self._open_root(create=create, mode=mode)

    def _open_root(self, create, mode):
        configured = self.configured_root
        flags = _directory_open_flags()
        opened = []
        try:
            descriptor = os.open(os.sep, flags)
            opened.append(descriptor)
            current = os.sep
            for name in [part for part in configured.split(os.sep) if part]:
                parent_fd = descriptor
                parent_display = current
                try:
                    before = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    created_display = os.path.join(current, name)
                    try:
                        os.mkdir(name, mode, dir_fd=parent_fd)
                    except FileExistsError:
                        # Another ArchiveSession may have created the shared
                        # component after our lstat.  Treat that only as a
                        # creation race: the lstat/open/inode checks below must
                        # still prove it is the same real directory.
                        pass
                    else:
                        self.created_paths.append(created_display)
                        with _pinned_directory(parent_display, parent_fd):
                            fsync_directory(parent_display)
                    try:
                        before = os.stat(
                            name, dir_fd=parent_fd, follow_symlinks=False
                        )
                    except FileNotFoundError as exc:
                        raise ArchiveConflictError(
                            "归档目录在并发创建时被移走"
                        ) from exc
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                    raise ArchivePathSafetyError(
                        "归档目录链包含符号链接或非目录项：%s" %
                        os.path.join(current, name)
                    )
                child = _open_verified_at(
                    parent_fd, name, flags, before, "归档目录"
                )
                self._root_chain.append((parent_fd, name, child))
                descriptor = child
                opened.append(descriptor)
                current = os.path.join(current, name)

            self.canonical_root = configured
            self._root_fd = descriptor
            self.assert_stable()
        except BaseException:
            for descriptor in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._root_chain = []
            self._root_fd = None
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        descriptors = []
        if self._root_chain:
            descriptors.append(self._root_chain[0][0])
            descriptors.extend(row[2] for row in self._root_chain)
        elif self._root_fd is not None:
            descriptors.append(self._root_fd)
        seen = set()
        for descriptor in reversed(descriptors):
            if descriptor in seen:
                continue
            seen.add(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._root_fd = None
        self._root_chain = []

    def fork(self):
        """Duplicate this pinned session for use in another thread.

        File-descriptor duplication preserves the exact directory inode chosen
        by the coordinator.  Reopening ``configured_root`` in a worker would
        instead allow a rename-and-replace race to redirect that worker into a
        different, but otherwise valid, directory.
        """
        self.assert_stable()
        duplicated = {}
        try:
            originals = []
            for parent_fd, _name, child_fd in self._root_chain:
                originals.extend((parent_fd, child_fd))
            originals.append(self._root_fd)
            for descriptor in originals:
                if descriptor not in duplicated:
                    duplicated[descriptor] = os.dup(descriptor)

            clone = object.__new__(ArchiveSession)
            clone.configured_root = self.configured_root
            clone.canonical_root = self.canonical_root
            clone._root_fd = duplicated[self._root_fd]
            clone._root_chain = [
                (duplicated[parent_fd], name, duplicated[child_fd])
                for parent_fd, name, child_fd in self._root_chain
            ]
            clone.created_paths = []
            clone._closed = False
            self.assert_stable()
            clone.assert_stable()
            return clone
        except BaseException:
            for descriptor in duplicated.values():
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def assert_stable(self):
        if self._closed or self._root_fd is None:
            raise ArchiveConflictError("归档会话已经关闭")
        for parent_fd, name, child_fd in self._root_chain:
            try:
                current = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise ArchiveConflictError("归档根或祖先已被移走") from exc
            if stat.S_ISLNK(current.st_mode) or not _same_inode(
                    current, os.fstat(child_fd)):
                raise ArchiveConflictError("归档根或祖先身份发生变化")

    def relative_path(self, path):
        absolute = _absolute_path(path)
        for root in (self.configured_root, self.canonical_root):
            if not root:
                continue
            try:
                common = os.path.commonpath([root, absolute])
            except ValueError:
                continue
            if common == root:
                return os.path.relpath(absolute, root)
        return None

    @staticmethod
    def _components(relative):
        if relative in (None, "", "."):
            return []
        if os.path.isabs(relative):
            raise ArchivePathSafetyError("归档内部路径必须是相对路径")
        normalized = os.path.normpath(relative)
        parts = normalized.split(os.sep)
        if normalized in ("..",) or any(
                part in ("", ".", "..") for part in parts):
            raise ArchivePathSafetyError("归档内部路径越界：%r" % relative)
        return parts

    def display_path(self, relative):
        parts = self._components(relative)
        return os.path.join(self.configured_root, *parts)

    @contextlib.contextmanager
    def opened_dir(self, relative=".", create=False, mode=0o777):
        self.assert_stable()
        components = self._components(relative)
        descriptor = os.dup(self._root_fd)
        initial_descriptor = descriptor
        chain = []
        display = self.configured_root
        try:
            for name in components:
                parent_fd = descriptor
                parent_display = display
                try:
                    before = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    child_display = os.path.join(display, name)
                    try:
                        os.mkdir(name, mode, dir_fd=parent_fd)
                    except FileExistsError:
                        # A peer may win the missing -> mkdir race for shared
                        # parents such as ``_state``.  Never trust the errno:
                        # lstat without following links, open with O_NOFOLLOW,
                        # then compare the two inode identities below.
                        pass
                    else:
                        self.created_paths.append(child_display)
                        with _pinned_directory(parent_display, parent_fd):
                            fsync_directory(parent_display)
                    try:
                        before = os.stat(
                            name, dir_fd=parent_fd, follow_symlinks=False
                        )
                    except FileNotFoundError as exc:
                        raise ArchiveConflictError(
                            "归档子目录在并发创建时被移走"
                        ) from exc
                    display = child_display
                else:
                    display = os.path.join(display, name)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                    raise ArchivePathSafetyError(
                        "归档目录链包含符号链接或非目录项：%s" % display
                    )
                child = _open_verified_at(
                    parent_fd, name, _directory_open_flags(), before,
                    "归档子目录",
                )
                chain.append((parent_fd, name, child))
                descriptor = child
            self._assert_local_chain(chain)
            yield descriptor, display, chain
            self._assert_local_chain(chain)
            self.assert_stable()
        finally:
            descriptors = [initial_descriptor]
            descriptors.extend(row[2] for row in chain)
            for child in reversed(descriptors):
                try:
                    os.close(child)
                except OSError:
                    pass

    @staticmethod
    def _assert_local_chain(chain):
        for parent_fd, name, child_fd in chain:
            try:
                current = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise ArchiveConflictError("归档子目录已被移走") from exc
            if stat.S_ISLNK(current.st_mode) or not _same_inode(
                    current, os.fstat(child_fd)):
                raise ArchiveConflictError("归档子目录身份发生变化")

    @contextlib.contextmanager
    def opened_parent(self, relative, create=False):
        parts = self._components(relative)
        if not parts:
            raise ArchivePathSafetyError("操作目标不能是归档根")
        parent = os.path.join(*parts[:-1]) if len(parts) > 1 else "."
        with self.opened_dir(parent, create=create) as opened:
            yield opened[0], opened[1], parts[-1], opened[2]

    def makedirs(self, relative, mode=0o777, exist_ok=True):
        before = len(self.created_paths)
        try:
            with self.opened_dir(relative, create=True, mode=mode):
                pass
        except FileExistsError:
            if not exist_ok:
                raise
        created = self.created_paths[before:]
        if not created and not exist_ok:
            raise FileExistsError(self.display_path(relative))
        return created

    def lstat(self, relative):
        if relative in ("", "."):
            self.assert_stable()
            return os.fstat(self._root_fd)
        with self.opened_parent(relative) as (parent_fd, _display, name, _chain):
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)

    def lexists(self, relative):
        try:
            self.lstat(relative)
            return True
        except FileNotFoundError:
            return False

    def is_dir(self, relative):
        try:
            return stat.S_ISDIR(self.lstat(relative).st_mode)
        except FileNotFoundError:
            return False

    def is_file(self, relative):
        try:
            return stat.S_ISREG(self.lstat(relative).st_mode)
        except FileNotFoundError:
            return False

    def read_bytes(self, relative):
        with self.opened_parent(relative) as (parent_fd, _display, name, chain):
            try:
                linked = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise ArchivePathSafetyError(
                    "归档读取目标无法安全检查"
                ) from exc
            if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
                raise ArchivePathSafetyError("归档读取目标必须是真实普通文件")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise ArchiveConflictError("归档读取目标在打开前被移走") from exc
            except OSError as exc:
                raise ArchivePathSafetyError(
                    "归档读取目标无法安全打开"
                ) from exc
            try:
                before = os.fstat(descriptor)
                if (not stat.S_ISREG(before.st_mode)
                        or not _same_inode(linked, before)):
                    raise ArchiveConflictError("归档读取目标身份发生变化")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    payload = stream.read()
                after = os.fstat(descriptor)
                if (not _same_inode(before, after)
                        or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns):
                    raise ArchiveConflictError("归档文件在读取期间发生变化")
                self._assert_local_chain(chain)
                self.assert_stable()
                return payload
            finally:
                _close_archive_descriptor(
                    descriptor,
                    "归档读取",
                    primary_error=sys.exc_info()[1],
                )

    def atomic_write(self, relative, payload, mode=0o600):
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("原子文件内容必须是二进制数据")
        with self.opened_parent(relative, create=True) as (
                parent_fd, parent_display, name, chain):
            temporary = ".%s.%s.tmp" % (name, uuid.uuid4().hex)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(
                    temporary, flags, mode, dir_fd=parent_fd
                )
            except OSError as exc:
                raise ArchiveTransactionError(
                    "无法安全创建归档原子写入暂存文件"
                ) from exc
            temporary_exists = True
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(bytes(payload))
                    stream.flush()
                    os.fsync(stream.fileno())
                self._assert_local_chain(chain)
                self.assert_stable()
                os.replace(
                    temporary, name,
                    src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                )
                temporary_exists = False
                with _pinned_directory(parent_display, parent_fd):
                    fsync_directory(parent_display)
                self._assert_local_chain(chain)
                self.assert_stable()
            except ARCHIVE_FATAL_EXCEPTIONS:
                raise
            except OSError as exc:
                raise ArchiveTransactionError(
                    "归档原子写入未完成耐久提交"
                ) from exc
            finally:
                primary_error = sys.exc_info()[1]
                if descriptor >= 0 or temporary_exists:
                    try:
                        _cleanup_archive_temporary(
                            parent_fd,
                            parent_display,
                            temporary,
                            descriptor=descriptor,
                            description="归档原子写入暂存文件",
                        )
                    except Exception as cleanup_error:  # noqa: BLE001
                        if primary_error is None:
                            raise
                        raise _combined_archive_cleanup_error(
                            "归档原子写入失败，且暂存文件清理未完成",
                            primary_error,
                            cleanup_error,
                        ) from primary_error

    def append_bytes(self, relative, payload):
        with self.opened_parent(relative, create=True) as (
                parent_fd, parent_display, name, chain):
            flags = os.O_WRONLY | os.O_APPEND
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                linked = os.stat(
                    name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                linked = None
            except OSError as exc:
                raise ArchivePathSafetyError(
                    "追加日志目标无法安全检查"
                ) from exc
            if linked is not None:
                if (stat.S_ISLNK(linked.st_mode)
                        or not stat.S_ISREG(linked.st_mode)):
                    raise ArchivePathSafetyError(
                        "追加日志目标必须是真实普通文件"
                    )
                descriptor = _open_verified_at(
                    parent_fd, name, flags, linked, "追加日志目标"
                )
            else:
                try:
                    descriptor = os.open(
                        name, flags | os.O_CREAT | os.O_EXCL, 0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    try:
                        linked = os.stat(
                            name, dir_fd=parent_fd, follow_symlinks=False
                        )
                    except OSError as exc:
                        raise ArchiveConflictError(
                            "追加日志目标在并发创建时失效"
                        ) from exc
                    if (stat.S_ISLNK(linked.st_mode)
                            or not stat.S_ISREG(linked.st_mode)):
                        raise ArchivePathSafetyError(
                            "追加日志目标必须是真实普通文件"
                        )
                    descriptor = _open_verified_at(
                        parent_fd, name, flags, linked, "追加日志目标"
                    )
                except OSError as exc:
                    raise ArchivePathSafetyError(
                        "追加日志目标无法安全创建"
                    ) from exc
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise ArchivePathSafetyError("追加日志目标必须是普通文件")
                with os.fdopen(descriptor, "ab", closefd=False) as stream:
                    stream.write(bytes(payload))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    linked_after = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except OSError as exc:
                    raise ArchiveConflictError(
                        "追加日志目标在写入期间被移走"
                    ) from exc
                if not _same_inode(opened, linked_after):
                    raise ArchiveConflictError(
                        "追加日志目标在写入期间发生变化"
                    )
                with _pinned_directory(parent_display, parent_fd):
                    fsync_directory(parent_display)
                self._assert_local_chain(chain)
                self.assert_stable()
            except ARCHIVE_FATAL_EXCEPTIONS:
                raise
            except OSError as exc:
                raise ArchiveTransactionError(
                    "追加日志未完成耐久写入"
                ) from exc
            finally:
                _close_archive_descriptor(
                    descriptor,
                    "归档日志追加",
                    primary_error=sys.exc_info()[1],
                )

    def make_temp_dir(self, parent_relative=".", prefix=".staging."):
        with self.opened_dir(parent_relative, create=True) as (
                parent_fd, parent_display, chain):
            for _attempt in range(128):
                name = prefix + uuid.uuid4().hex
                try:
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise ArchiveTransactionError(
                        "无法安全创建归档暂存目录"
                    ) from exc
                try:
                    with _pinned_directory(parent_display, parent_fd):
                        fsync_directory(parent_display)
                except BaseException as primary_error:
                    try:
                        _cleanup_archive_temporary(
                            parent_fd,
                            parent_display,
                            name,
                            directory=True,
                            description="归档暂存目录",
                        )
                    except Exception as cleanup_error:  # noqa: BLE001
                        raise _combined_archive_cleanup_error(
                            "归档暂存目录创建失败，且耐久清理未完成",
                            primary_error,
                            cleanup_error,
                        ) from primary_error
                    if isinstance(primary_error, OSError):
                        raise ArchiveTransactionError(
                            "归档暂存目录创建未完成耐久提交"
                        ) from primary_error
                    raise
                self._assert_local_chain(chain)
                self.assert_stable()
                relative = os.path.join(parent_relative, name)
                return self.display_path(relative)
        raise FileExistsError("无法创建唯一归档暂存目录")

    def rename(self, source_relative, target_relative):
        source_parts = self._components(source_relative)
        target_parts = self._components(target_relative)
        if not source_parts or not target_parts:
            raise ArchivePathSafetyError("不能重命名归档根")
        source_parent = os.path.join(*source_parts[:-1]) if len(source_parts) > 1 else "."
        target_parent = os.path.join(*target_parts[:-1]) if len(target_parts) > 1 else "."
        with self.opened_dir(source_parent) as source_opened, \
                self.opened_dir(target_parent, create=True) as target_opened:
            source_fd, source_display, source_chain = source_opened
            target_fd, target_display, target_chain = target_opened
            self._assert_local_chain(source_chain)
            self._assert_local_chain(target_chain)
            self.assert_stable()
            os.replace(
                source_parts[-1], target_parts[-1],
                src_dir_fd=source_fd, dst_dir_fd=target_fd,
            )
            with _pinned_directory(target_display, target_fd):
                fsync_directory(target_display)
            if not _same_inode(os.fstat(source_fd), os.fstat(target_fd)):
                with _pinned_directory(source_display, source_fd):
                    fsync_directory(source_display)
            self._assert_local_chain(source_chain)
            self._assert_local_chain(target_chain)
            self.assert_stable()

    def unlink(self, relative, missing_ok=False):
        with self.opened_parent(relative) as (
                parent_fd, parent_display, name, chain):
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    raise IsADirectoryError(self.display_path(relative))
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise
            with _pinned_directory(parent_display, parent_fd):
                fsync_directory(parent_display)
            self._assert_local_chain(chain)
            self.assert_stable()
            return True

    @staticmethod
    def _remove_tree_fd(parent_fd, name):
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise ArchivePathSafetyError("递归删除目标必须是真实目录")
        descriptor = _open_verified_at(
            parent_fd, name, _directory_open_flags(), before,
            "待删除目录",
        )
        try:
            for child in os.listdir(descriptor):
                info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    ArchiveSession._remove_tree_fd(descriptor, child)
                else:
                    os.unlink(child, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            _close_archive_descriptor(
                descriptor,
                "递归删除归档目录",
                primary_error=sys.exc_info()[1],
            )
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_inode(before, current):
            raise ArchiveConflictError("待删除目录身份发生变化")
        os.rmdir(name, dir_fd=parent_fd)

    def rmtree(self, relative, missing_ok=False):
        with self.opened_parent(relative) as (
                parent_fd, parent_display, name, chain):
            try:
                self._remove_tree_fd(parent_fd, name)
            except FileNotFoundError:
                if missing_ok:
                    return False
                raise
            with _pinned_directory(parent_display, parent_fd):
                fsync_directory(parent_display)
            self._assert_local_chain(chain)
            self.assert_stable()
            return True

    @classmethod
    def _fsync_tree_fd(cls, descriptor):
        for name in sorted(os.listdir(descriptor)):
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise ArchivePathSafetyError("待提交目录树不能包含符号链接")
            if stat.S_ISDIR(before.st_mode):
                child = _open_verified_at(
                    descriptor, name, _directory_open_flags(), before,
                    "待提交目录",
                )
                try:
                    cls._fsync_tree_fd(child)
                finally:
                    _close_archive_descriptor(
                        child,
                        "归档目录树耐久提交",
                        primary_error=sys.exc_info()[1],
                    )
            elif stat.S_ISREG(before.st_mode):
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                child = _open_verified_at(
                    descriptor, name, flags, before, "待提交文件"
                )
                try:
                    os.fsync(child)
                finally:
                    _close_archive_descriptor(
                        child,
                        "归档文件耐久提交",
                        primary_error=sys.exc_info()[1],
                    )
            else:
                raise ArchivePathSafetyError("目录树只能包含真实普通文件")
        os.fsync(descriptor)

    def fsync_tree(self, relative):
        with self.opened_dir(relative) as (descriptor, _display, chain):
            self._fsync_tree_fd(descriptor)
            self._assert_local_chain(chain)
            self.assert_stable()

    @classmethod
    def _snapshot_tree_fd(cls, descriptor, prefix, directories, files):
        for name in sorted(os.listdir(descriptor)):
            relative = os.path.join(prefix, name) if prefix else name
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise ArchivePathSafetyError("归档目录树不能包含符号链接")
            if stat.S_ISDIR(before.st_mode):
                directories.append(relative)
                child = _open_verified_at(
                    descriptor, name, _directory_open_flags(), before,
                    "归档目录",
                )
                try:
                    cls._snapshot_tree_fd(child, relative, directories, files)
                finally:
                    _close_archive_descriptor(
                        child,
                        "归档目录快照",
                        primary_error=sys.exc_info()[1],
                    )
            elif stat.S_ISREG(before.st_mode):
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                child = _open_verified_at(
                    descriptor, name, flags, before, "归档文件"
                )
                try:
                    opened = os.fstat(child)
                    with os.fdopen(child, "rb", closefd=False) as stream:
                        payload = stream.read()
                    after = os.fstat(child)
                    if (not _same_inode(opened, after)
                            or opened.st_size != after.st_size
                            or opened.st_mtime_ns != after.st_mtime_ns):
                        raise ArchiveConflictError("归档文件在读取期间发生变化")
                    files.append((relative, payload))
                finally:
                    _close_archive_descriptor(
                        child,
                        "归档文件快照",
                        primary_error=sys.exc_info()[1],
                    )
            else:
                raise ArchivePathSafetyError("归档目录树包含特殊文件")

    def snapshot_tree(self, relative):
        directories, files = [], []
        with self.opened_dir(relative) as (descriptor, _display, chain):
            self._snapshot_tree_fd(descriptor, "", directories, files)
            self._assert_local_chain(chain)
            self.assert_stable()
        return directories, files

    def copy_file_from_path(self, source, target_relative, expected_sha256=None):
        with self.opened_parent(target_relative, create=True) as (
                parent_fd, parent_display, name, chain):
            temporary = ".%s.%s.tmp" % (name, uuid.uuid4().hex)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(
                    temporary, flags, 0o600, dir_fd=parent_fd
                )
            except OSError as exc:
                raise ArchiveTransactionError(
                    "无法安全创建归档复制暂存文件"
                ) from exc
            temporary_exists = True
            digest = hashlib.sha256()
            try:
                with open(source, "rb") as source_stream, os.fdopen(
                        descriptor, "wb") as target_stream:
                    descriptor = -1
                    while True:
                        block = source_stream.read(1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        target_stream.write(block)
                    target_stream.flush()
                    os.fsync(target_stream.fileno())
                actual = digest.hexdigest()
                if expected_sha256 is not None and actual != expected_sha256:
                    raise RuntimeError("复制文件的 SHA-256 与预期不一致")
                self._assert_local_chain(chain)
                self.assert_stable()
                os.replace(
                    temporary, name,
                    src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                )
                temporary_exists = False
                with _pinned_directory(parent_display, parent_fd):
                    fsync_directory(parent_display)
                self._assert_local_chain(chain)
                self.assert_stable()
                return actual
            except ARCHIVE_FATAL_EXCEPTIONS:
                raise
            except OSError as exc:
                raise ArchiveTransactionError(
                    "归档文件复制未完成耐久提交"
                ) from exc
            finally:
                primary_error = sys.exc_info()[1]
                if descriptor >= 0 or temporary_exists:
                    try:
                        _cleanup_archive_temporary(
                            parent_fd,
                            parent_display,
                            temporary,
                            descriptor=descriptor,
                            description="归档复制暂存文件",
                        )
                    except Exception as cleanup_error:  # noqa: BLE001
                        if primary_error is None:
                            raise
                        raise _combined_archive_cleanup_error(
                            "归档文件复制失败，且暂存文件清理未完成",
                            primary_error,
                            cleanup_error,
                        ) from primary_error


def _archive_stack():
    stack = getattr(_ARCHIVE_CONTEXT, "stack", None)
    if stack is None:
        stack = []
        _ARCHIVE_CONTEXT.stack = stack
    return stack


def current_archive_session(path=None):
    for session in reversed(getattr(_ARCHIVE_CONTEXT, "stack", ())):
        if path is None or session.relative_path(path) is not None:
            return session
    return None


def archive_lock_identity(root):
    """Return a source-lock identity tied to the opened archive inode.

    Path strings are not identities on a case-insensitive filesystem.  If the
    archive exists, pin its full component chain and derive the key from the
    root descriptor.  A path identity is used only for a genuinely not-yet-
    created archive.  Callers must create and pin it before acquiring a source
    lock so a missing-path key can never transition to a different inode key.
    """
    configured = _absolute_path(root)
    active = current_archive_session()
    owns_session = False
    if (active is None
            or configured not in (
                active.configured_root, active.canonical_root
            )):
        active = ArchiveSession(configured, create=False)
        owns_session = True
    try:
        active.assert_stable()
        info = os.fstat(active._root_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ArchivePathSafetyError("归档锁根路径必须是真实目录")
        return "inode:%s:%s" % (info.st_dev, info.st_ino)
    finally:
        if owns_session:
            active.close()


@contextlib.contextmanager
def fork_archive_session(parent):
    """Bind an fd-cloned parent ArchiveSession to the current worker thread."""
    if not isinstance(parent, ArchiveSession):
        raise TypeError("工作线程归档会话必须来自 ArchiveSession")
    if current_archive_session() is not None:
        raise ArchiveConflictError("工作线程不能嵌套另一归档会话")
    session = parent.fork()
    stack = _archive_stack()
    stack.append(session)
    try:
        yield session
        session.assert_stable()
    finally:
        stack.pop()
        session.close()


@contextlib.contextmanager
def pinned_user_lock_directory(path, label):
    """Yield a securely pinned, TMPDIR-independent per-user lock directory."""
    root = _absolute_path(path)
    user_root = _absolute_path(READDAILY_USER_LOCK_ROOT)
    try:
        if os.path.commonpath([user_root, root]) != user_root:
            raise ArchivePathSafetyError(
                "%s必须位于固定的当前用户锁根目录" % label
            )
    except ValueError as exc:
        raise ArchivePathSafetyError(
            "%s无法绑定到固定的当前用户锁根目录" % label
        ) from exc
    with archive_session(root, create=True, mode=0o700) as session:
        session.assert_stable()
        configured_parts = [
            part for part in session.configured_root.split(os.sep) if part
        ]
        user_parts = [part for part in user_root.split(os.sep) if part]
        if configured_parts[:len(user_parts)] != user_parts:
            raise ArchivePathSafetyError("%s固定根目录身份不一致" % label)
        # /private/tmp is sticky and system-owned.  Starting with the
        # predictable readdaily-<uid> component, every ancestor that could
        # rename a descendant must itself belong exclusively to this user.
        for _parent_fd, _name, child_fd in session._root_chain[
                len(user_parts) - 1:]:
            info = os.fstat(child_fd)
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ArchivePathSafetyError(
                    "%s及其用户级祖先必须由当前用户持有且不可被他人写入"
                    % label
                )
        yield session._root_fd, root
        session.assert_stable()


@contextlib.contextmanager
def source_evidence_lock_directory():
    """Yield the stable source/date evidence lock directory."""
    with pinned_user_lock_directory(
            SOURCE_EVIDENCE_LOCK_ROOT, "报纸来源证据锁目录") as opened:
        yield opened


def assert_configured_roots_separate(first, second, label="Vault"):
    """Reject an obvious lexical overlap before either root may be created."""
    first = _absolute_path(first)
    second = _absolute_path(second)
    try:
        common = os.path.commonpath([first, second])
    except ValueError as exc:
        raise ArchivePathSafetyError("归档与 %s 无法安全比较" % label) from exc
    if common in (first, second):
        raise ArchivePathSafetyError(
            "归档目录必须与 %s 完全分离，不能互为父子目录" % label
        )


def assert_session_isolated(session, forbidden_root, label="Vault"):
    """Compare a pinned archive identity with a separately pinned root.

    This check deliberately runs *after* the archive descriptor is opened. A
    validation-then-open pathname swap therefore either encounters O_NOFOLLOW
    or compares the attacker's actual opened inode/path against the forbidden
    root before any archive mutation is allowed.
    """
    if not isinstance(session, ArchiveSession):
        raise TypeError("隔离校验需要已打开的归档会话")
    session.assert_stable()
    forbidden = _absolute_path(forbidden_root)
    try:
        other = ArchiveSession(forbidden, create=False)
    except FileNotFoundError:
        other = None
        other_root = forbidden
    except (ArchivePathSafetyError, ArchiveConflictError) as exc:
        raise ArchivePathSafetyError(
            "%s 路径无法安全固定" % label
        ) from exc
    else:
        other_root = other.canonical_root
    try:
        try:
            common = os.path.commonpath([
                session.canonical_root, other_root
            ])
        except ValueError as exc:
            raise ArchivePathSafetyError("归档与 %s 无法安全比较" % label) from exc
        same_root_inode = (
            other is not None
            and _same_inode(os.fstat(session._root_fd), os.fstat(other._root_fd))
        )
        if same_root_inode or common in (session.canonical_root, other_root):
            raise ArchivePathSafetyError(
                "归档目录必须与 %s 完全分离，不能互为父子目录或路径别名"
                % label
            )
        session.assert_stable()
        if other is not None:
            other.assert_stable()
    finally:
        if other is not None:
            other.close()
    return session.canonical_root


@contextlib.contextmanager
def archive_session(root, create=False, mode=0o777):
    existing = current_archive_session(root)
    if existing is not None and existing.relative_path(root) == ".":
        stack = _archive_stack()
        stack.append(existing)
        try:
            yield existing
            existing.assert_stable()
        finally:
            stack.pop()
        return
    session = ArchiveSession(root, create=create, mode=mode)
    stack = _archive_stack()
    stack.append(session)
    try:
        yield session
        session.assert_stable()
    finally:
        stack.pop()
        session.close()


@contextlib.contextmanager
def _session_for_single_path(path, create_parent=False):
    absolute = _absolute_path(path)
    session = current_archive_session(absolute)
    if session is not None:
        yield session, session.relative_path(absolute)
        return
    parent = os.path.dirname(absolute)
    with archive_session(parent, create=create_parent) as session:
        yield session, os.path.basename(absolute)


def load_json(path, default=None):
    try:
        return json.loads(read_bytes(path).decode("utf-8"))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return default


def read_bytes(path):
    with _session_for_single_path(path) as (session, relative):
        return session.read_bytes(relative)


def path_exists(path):
    try:
        with _session_for_single_path(path) as (session, relative):
            return session.lexists(relative)
    except FileNotFoundError:
        return False


def path_is_file(path):
    try:
        with _session_for_single_path(path) as (session, relative):
            return session.is_file(relative)
    except FileNotFoundError:
        return False


def path_is_dir(path):
    try:
        with _session_for_single_path(path) as (session, relative):
            return session.is_dir(relative)
    except FileNotFoundError:
        return False


def fsync_directory(path):
    """Flush a directory through an already-pinned descriptor when possible."""
    directory = _absolute_path(path)
    pinned = _pinned_directory_fd(directory)
    if pinned is not None:
        if not stat.S_ISDIR(os.fstat(pinned).st_mode):
            raise ValueError("目录 fsync 目标必须是真实目录：%s" % directory)
        os.fsync(pinned)
        return
    session = current_archive_session(directory)
    if session is not None:
        relative = session.relative_path(directory)
        with session.opened_dir(relative) as (descriptor, _display, _chain):
            os.fsync(descriptor)
        return
    with archive_session(directory, create=False) as session:
        os.fsync(session._root_fd)


def fsync_file(path):
    """Flush one ordinary file without following a replacement symlink."""
    with _session_for_single_path(path) as (session, relative):
        with session.opened_parent(relative) as (parent_fd, _display, name, _chain):
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("目录树只能包含真实普通文件：%s" % path)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def fsync_tree(root):
    """Flush every staged file and directory, children before parents."""
    with _session_for_single_path(root) as (session, relative):
        if not session.is_dir(relative):
            raise ValueError("待提交目录树必须是真实目录：%s" % root)
        session.fsync_tree(relative)


def durable_makedirs(path, mode=0o777, exist_ok=True):
    """Create and fsync each path component without pathname re-resolution."""
    directory = _absolute_path(path)
    session = current_archive_session(directory)
    if session is not None:
        return session.makedirs(
            session.relative_path(directory), mode=mode, exist_ok=exist_ok
        )
    with archive_session(directory, create=True, mode=mode) as session:
        if not exist_ok and not session.created_paths:
            raise FileExistsError(directory)
        return list(session.created_paths)


def _common_session_for_paths(source, target):
    source = _absolute_path(source)
    target = _absolute_path(target)
    for session in reversed(getattr(_ARCHIVE_CONTEXT, "stack", ())):
        if (session.relative_path(source) is not None
                and session.relative_path(target) is not None):
            return contextlib.nullcontext(session)
    common = os.path.commonpath([os.path.dirname(source), os.path.dirname(target)])
    return archive_session(common, create=False)


def durable_replace(source, target):
    """Atomically replace through parent dirfds and durably flush namespaces."""
    source = _absolute_path(source)
    target = _absolute_path(target)
    with _common_session_for_paths(source, target) as session:
        session.rename(session.relative_path(source), session.relative_path(target))


def durable_unlink(path, missing_ok=False):
    """Unlink one non-directory entry and durably flush its pinned parent."""
    with _session_for_single_path(path) as (session, relative):
        return session.unlink(relative, missing_ok=missing_ok)


def durable_rmtree(path, missing_ok=False):
    """Remove one real tree by dirfd and durably flush its pinned parent."""
    try:
        with _session_for_single_path(path) as (session, relative):
            return session.rmtree(relative, missing_ok=missing_ok)
    except FileNotFoundError:
        if missing_ok:
            return False
        raise


def durable_atomic_write_bytes(path, payload):
    """Write bytes via file fsync, sibling renameat, then parent fsync."""
    with _session_for_single_path(path, create_parent=True) as (
            session, relative):
        session.atomic_write(relative, payload)


def durable_copy_file(source, target, expected_sha256=None):
    """Stream an external file into a pinned archive sibling transaction."""
    with _session_for_single_path(target, create_parent=True) as (
            session, relative):
        return session.copy_file_from_path(
            source, relative, expected_sha256=expected_sha256
        )


def durable_chmod(path, mode):
    """Change one pinned regular file's mode and flush the inode."""
    with _session_for_single_path(path) as (session, relative):
        with session.opened_parent(relative) as (
                parent_fd, _parent_display, name, chain):
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ArchivePathSafetyError("chmod 目标必须是普通文件")
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
                session._assert_local_chain(chain)
                session.assert_stable()
            finally:
                os.close(descriptor)


def durable_mkdtemp(parent, prefix=".staging."):
    parent = _absolute_path(parent)
    session = current_archive_session(parent)
    if session is not None:
        return session.make_temp_dir(session.relative_path(parent), prefix=prefix)
    with archive_session(parent, create=True) as session:
        return session.make_temp_dir(".", prefix=prefix)


def read_tree_files(root):
    with _session_for_single_path(root) as (session, relative):
        _directories, files = session.snapshot_tree(relative)
        return files


def copy_directory_tree(source, target):
    """Copy a validated real tree inside one pinned archive session."""
    source = _absolute_path(source)
    target = _absolute_path(target)
    with _common_session_for_paths(source, target) as session:
        source_relative = session.relative_path(source)
        target_relative = session.relative_path(target)
        directories, files = session.snapshot_tree(source_relative)
        session.makedirs(target_relative, exist_ok=True)
        for directory in directories:
            session.makedirs(os.path.join(target_relative, directory), exist_ok=True)
        for relative, payload in files:
            session.atomic_write(os.path.join(target_relative, relative), payload)


def save_json(path, obj):
    payload = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
    durable_atomic_write_bytes(path, payload)


def _write_bytes_fsync(path, payload):
    """Write one staged artifact completely before it can become visible."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("归档文件内容必须是二进制数据")
    durable_atomic_write_bytes(path, payload)


def _transaction_relative_path(value):
    """Accept only a normalized path contained by a fresh staging tree."""
    if not isinstance(value, str) or not value:
        raise ValueError("归档文件相对路径无效")
    normalized = os.path.normpath(value)
    if (os.path.isabs(normalized) or normalized in ("", ".", "..")
            or normalized.startswith(".." + os.sep)):
        raise ValueError("归档文件路径必须位于期次目录内：%r" % value)
    if normalized == "issue.json":
        raise ValueError("issue.json 由事务提交器生成，不能作为普通文件覆盖")
    return normalized


def replace_issue_directory(staging, target):
    """Atomically replace a complete issue directory with rollback.

    Both directories must live on the same filesystem.  If committing the new
    tree fails after the old tree was moved aside, the old tree is restored
    before the original error is re-raised.
    """
    staging = _absolute_path(staging)
    target = _absolute_path(target)
    with _common_session_for_paths(staging, target) as session:
        staging_relative = session.relative_path(staging)
        target_relative = session.relative_path(target)
        if os.path.dirname(staging_relative) != os.path.dirname(target_relative):
            raise ValueError("暂存目录必须与目标期次目录位于同一父目录")
        if not session.is_dir(staging_relative):
            raise ValueError("暂存期次目录无效")
        if session.lexists(target_relative) and not session.is_dir(target_relative):
            raise ValueError("目标期次路径必须是真实目录")

        # OCR helpers and copy callers may not fsync their outputs. Flush the
        # complete private tree through the same pinned directory hierarchy.
        fsync_tree(staging)

        backup = None
        old_moved = False
        new_moved = False
        if session.lexists(target_relative):
            transaction_id = os.path.basename(staging).rsplit(".", 1)[-1]
            backup = os.path.join(
                os.path.dirname(target),
                ".%s.previous.%s" % (os.path.basename(target), transaction_id),
            )
            backup_relative = session.relative_path(backup)
            if session.lexists(backup_relative):
                raise RuntimeError("期次备份路径意外存在：%s" % backup)
        else:
            backup_relative = None
        try:
            if backup_relative:
                try:
                    session.rename(target_relative, backup_relative)
                except BaseException:
                    old_moved = (
                        not session.lexists(target_relative)
                        and session.lexists(backup_relative)
                    )
                    raise
                else:
                    old_moved = True
            try:
                session.rename(staging_relative, target_relative)
            except BaseException:
                new_moved = (
                    not session.lexists(staging_relative)
                    and session.lexists(target_relative)
                )
                raise
            else:
                new_moved = True
        except BaseException as commit_error:
            recovery_errors = []
            if new_moved:
                try:
                    if (not session.lexists(target_relative)
                            or session.lexists(staging_relative)):
                        raise RuntimeError("新期次位置异常，无法移回暂存目录")
                    session.rename(target_relative, staging_relative)
                    new_moved = False
                except BaseException as exc:  # noqa: BLE001
                    recovery_errors.append(exc)
            if old_moved:
                try:
                    if (session.lexists(target_relative)
                            or not session.lexists(backup_relative)):
                        raise RuntimeError("旧期次备份位置异常，无法自动恢复")
                    session.rename(backup_relative, target_relative)
                    old_moved = False
                except BaseException as exc:  # noqa: BLE001
                    recovery_errors.append(exc)
            if recovery_errors:
                raise ArchiveTransactionError(
                    "期次提交失败且耐久回滚未完成；请保留并检查 target=%s、"
                    "staging=%s、backup=%s" % (target, staging, backup or "无")
                ) from recovery_errors[0]
            raise commit_error

        if backup_relative and session.lexists(backup_relative):
            try:
                durable_rmtree(backup)
            except BaseException as cleanup_error:
                raise ArchiveTransactionError(
                    "新期次已提交，但旧期次备份未完成耐久删除；"
                    "target=%s、backup=%s" % (target, backup)
                ) from cleanup_error


def commit_issue_tree(issue_dir, files, issue):
    """Stage and atomically commit one complete fetched issue tree.

    ``files`` is an iterable of ``(relative_path, bytes)`` pairs.  The live
    issue directory is untouched until every file and ``issue.json`` has been
    flushed successfully.  A successful fetch deliberately starts with empty
    ``pages`` and ``text`` directories and replaces, rather than merges with,
    any prior same-day issue.
    """
    issue_dir = _absolute_path(issue_dir)
    parent = os.path.dirname(issue_dir)
    if not isinstance(issue, dict):
        raise TypeError("issue.json 内容必须是对象")

    normalized_files = []
    seen = set()
    for relative_path, payload in files:
        relative_path = _transaction_relative_path(relative_path)
        if relative_path in seen:
            raise ValueError("归档事务包含重复文件：%s" % relative_path)
        seen.add(relative_path)
        normalized_files.append((relative_path, payload))

    active = current_archive_session(issue_dir)
    session_context = (
        contextlib.nullcontext(active)
        if active is not None else archive_session(parent, create=True)
    )
    with session_context as session:
        durable_makedirs(parent, exist_ok=True)
        target_relative = session.relative_path(issue_dir)
        if session.lexists(target_relative) and not session.is_dir(target_relative):
            raise ValueError("目标期次路径必须是真实目录")
        staging = session.make_temp_dir(
            session.relative_path(parent),
            prefix=".%s.staging." % os.path.basename(issue_dir),
        )
        try:
            durable_makedirs(os.path.join(staging, "pages"), exist_ok=False)
            durable_makedirs(os.path.join(staging, "text"), exist_ok=False)
            for relative_path, payload in normalized_files:
                _write_bytes_fsync(os.path.join(staging, relative_path), payload)
            save_json(os.path.join(staging, "issue.json"), issue)
            replace_issue_directory(staging, issue_dir)
            staging = None
        except BaseException:
            if staging and session.lexists(session.relative_path(staging)):
                try:
                    durable_rmtree(staging)
                except BaseException as cleanup_error:
                    raise ArchiveTransactionError(
                        "期次事务未完成，且暂存目录未完成耐久清理：%s"
                        % staging
                    ) from cleanup_error
            raise


def state_has(state, stage):
    return bool(state and state.get("stages", {}).get(stage))


def state_mark(state_path, stage, **extra):
    st = load_json(state_path, {}) or {}
    st.setdefault("stages", {})[stage] = datetime.datetime.now().isoformat(timespec="seconds")
    st.update(extra)
    save_json(state_path, st)
    return st


def chain_check(archive_root, source_id, d, issue_no):
    """期号连续性校验：与「上一期」比较（差 1 或空）。返回 (ok, 提示)。"""
    d = norm_day(d)
    prev = d - datetime.timedelta(days=1)
    pj = os.path.join(os.path.expanduser(archive_root), source_id,
                      prev.isoformat(), "issue.json")
    meta = load_json(pj)
    if not meta or not meta.get("issue_no") or not issue_no:
        return True, "无上一期或期号缺失，跳过连续性校验"
    try:
        diff = int(issue_no) - int(meta["issue_no"])
    except (TypeError, ValueError):
        return True, "期号非数字，跳过"
    if diff == 1:
        return True, f"期号连续（{meta['issue_no']}→{issue_no}）"
    return False, f"期号不连续：上一期 {meta['issue_no']}，本期 {issue_no}（差 {diff}，可能缺刊）"


def log_line(path, entry):
    entry.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
    payload = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    lock_key = _absolute_path(path)
    with _LOG_THREAD_LOCKS_GUARD:
        thread_lock = _LOG_THREAD_LOCKS.setdefault(
            lock_key, threading.Lock()
        )
    # A fetch batch now runs sources concurrently.  Serialize each complete
    # JSON line so two worker threads can never splice their UTF-8 payloads.
    # The date coordinator lock separately excludes another fetch process.
    with thread_lock:
        with _session_for_single_path(path, create_parent=True) as (
                session, relative):
            session.append_bytes(relative, payload)
    console_print("[log]", json.dumps(entry, ensure_ascii=False)[:300])


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(s)).strip("_")[:80]


def unique_issue_no(issue):
    return issue.get("issue_no")
