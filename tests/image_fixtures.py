"""Small dependency-free, genuinely decodable image fixtures for tests."""
import functools
import struct
import zlib


def _png_chunk(chunk_type, payload):
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return (
        len(payload).to_bytes(4, "big")
        + chunk_type
        + payload
        + checksum.to_bytes(4, "big")
    )


@functools.lru_cache(maxsize=32)
def page_png(width=1280, height=1823, min_bytes=60000, fill=b"A"):
    """Build a valid grayscale PNG with newspaper-like dimensions.

    A valid ancillary text chunk supplies the byte floor used by adapters to
    reject thumbnails.  The raster itself stays highly compressible so tests
    remain fast and do not need Pillow or platform image-writing APIs.
    """
    if width <= 0 or height <= 0:
        raise ValueError("dimensions must be positive")
    if not isinstance(fill, bytes) or not fill:
        raise ValueError("fill must be non-empty bytes")
    shade = fill[0:1]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    scanlines = (b"\x00" + shade * width) * height
    chunks = [
        _png_chunk(b"IHDR", ihdr),
        _png_chunk(b"IDAT", zlib.compress(scanlines, 9)),
    ]
    iend = _png_chunk(b"IEND", b"")
    current_size = 8 + sum(len(chunk) for chunk in chunks) + len(iend)
    if current_size < min_bytes:
        # 12 bytes are the chunk framing; the keyword and NUL are valid tEXt.
        payload_size = max(8, min_bytes - current_size - 12)
        payload = b"fixture\x00" + shade * max(0, payload_size - 8)
        chunks.append(_png_chunk(b"tEXt", payload))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks) + iend
