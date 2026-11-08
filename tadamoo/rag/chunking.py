"""Splits a document's text into passages for search: on paragraph boundaries, about `size` characters each, each passage starting with the
tail of the one before it so a sentence that straddles a boundary is found in either."""
from __future__ import annotations

import re
from typing import List

SIZE = 1000
OVERLAP = 150


def chunk_text(text: str, size: int = SIZE, overlap: int = OVERLAP) -> List[str]:
    """The passages of `text`. A paragraph longer than `size` is cut at the last sentence or word end inside the size, else hard."""
    if size < 50 or not 0 <= overlap < size // 2:
        raise ValueError("size must be at least 50 and overlap under half of it")
    units: List[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n", (text or "").strip())):
        while len(para) > size:
            cut = _cut(para, size)
            units.append(para[:cut].strip())
            para = para[cut:].strip()
        if para:
            units.append(para)
    out: List[str] = []
    cur = ""
    for unit in units:
        if cur and len(cur) + len(unit) + 2 > size:
            out.append(cur)
            tail = _tail(cur, overlap)
            cur = (tail + "\n\n" + unit) if tail else unit
        else:
            cur = (cur + "\n\n" + unit) if cur else unit
    if cur:
        out.append(cur)
    return out


def _cut(para: str, size: int) -> int:
    """Where to cut a paragraph longer than `size`: after the last sentence end in its second half, else after the last space, else at `size`."""
    window = para[:size]
    ends = [m.end() for m in re.finditer(r"[.!?;:]\s", window)]
    if ends and ends[-1] > size // 2:
        return ends[-1]
    space = window.rfind(" ")
    return space + 1 if space > size // 2 else size


def _tail(text: str, overlap: int) -> str:
    """The last `overlap` characters of `text`, moved forward to a word start so no word is cut."""
    if overlap <= 0:
        return ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1:] if 0 <= space < len(tail) - 1 else tail
