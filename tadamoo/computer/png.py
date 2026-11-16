"""A PNG made from raw pixels with the standard library alone (no Pillow): what the Windows backend keeps of a screen capture."""
from __future__ import annotations

import struct
import zlib


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def bgra_to_rgb(bgra: bytes) -> bytes:
    """Drops the alpha byte and puts the colours in the order PNG wants."""
    count = len(bgra) // 4
    out = bytearray(count * 3)
    out[0::3] = bgra[2::4]
    out[1::3] = bgra[1::4]
    out[2::3] = bgra[0::4]
    return bytes(out)


def encode_rgb(rgb: bytes, width: int, height: int, level: int = 4) -> bytes:
    """An 8-bit RGB PNG of width x height pixels (`rgb` is width*height*3 bytes, rows top to bottom)."""
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError("the pixels do not fill the picture")
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, level)) + _chunk(b"IEND", b"")
