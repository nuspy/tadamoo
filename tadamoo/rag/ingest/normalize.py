"""Cleaning the text of a document before it is cut into passages: pure functions on strings and on blocks.

A block is a dict `{type, text, page, level?}` (tadamoo/rag/ingest/blocks.py). Text goes through the same steps, whatever read it (Docling, the
basic readers): control characters out, a few compatibility characters made plain, words broken at line ends joined, lines wrapped by the page
made one, white space collapsed. Page numbers and the headers and footers that repeat on the pages are dropped, and lines that are mostly noise from
text recognition. Tables and code are left as they are. Nothing here reads a file or calls a model."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Sequence, Tuple

VERSION = 1

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_SPACES = dict.fromkeys(map(ord, "               　"), " ")
_LINE_SEPARATORS = {0x2028: "\n", 0x2029: "\n\n"}
_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st", "‑": "-", "‒": "-", "−": "-"}
_BULLET_START = re.compile(r"^\s*(?:[-*+•·▪◦‣]|\d{1,3}[.)]|[a-z][.)]|\(\w{1,3}\))\s+")
_PAGE_NUMBER = re.compile(r"^\s*(?:(?:page|pag\.?|p\.|seite|pagina|página|第)\s*)?(?:\d{1,5}|[ivxlcdm]{1,7})(?:\s*(?:/|of|di|von|de|sur)\s*\d{1,5})?\s*(?:页)?\s*$", re.I)
_DIGITS = re.compile(r"\d+")
_HYPHEN_BREAK = re.compile(r"([^\W\d_]{2})-[ \t]*\n[ \t]*([^\W\d_])")
KEEP_TYPES = ("table", "code")
EDGE = 2                           # blocks at the top and at the bottom of a page that may be a header or a footer
MIN_PAGE_BLOCKS = 4                # a page with fewer blocks than this has no header or footer to tell from its text
MIN_PAGES_REPEAT = 3               # a header or footer repeats on at least this many pages
REPEAT_SHARE = 0.25                # ... and on at least this share of the pages that have text


def _plain_char(ch: str) -> str:
    """One character as it is searched: a ligature or a full-width Latin letter or digit becomes the plain one; everything else (CJK, kana,
    accents, punctuation of any script) is left as written."""
    if ch in _LIGATURES:
        return _LIGATURES[ch]
    code = ord(ch)
    if 0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A or 0xFF41 <= code <= 0xFF5A:
        return chr(code - 0xFF00 + 0x20)
    return ch


def clean_chars(text: str) -> str:
    """Control and zero-width characters out, odd spaces and line separators made plain, ligatures and full-width Latin undone, accents composed (NFC)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_ZERO_WIDTH).translate(_SPACES).translate(_LINE_SEPARATORS)
    text = _CONTROL.sub("", text)
    if any(ord(c) > 0x2010 for c in text):
        text = "".join(_plain_char(c) for c in text)
    return unicodedata.normalize("NFC", text)


def collapse_space(text: str) -> str:
    """Runs of spaces and tabs inside a line become one space, lines lose the spaces at their ends, three blank lines become one."""
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def dehyphenate(text: str) -> str:
    """A word broken with a hyphen at the end of a line is made whole when the next line goes on in lower case: `reprogram-` / `ming` is
    `reprogramming`. A hyphen before a capital, a digit or a space stays (a name, a range, a dash)."""
    return _HYPHEN_BREAK.sub(lambda m: m.group(1) + m.group(2) if m.group(2).islower() else m.group(0), text)


def _is_structure(line: str) -> bool:
    s = line.lstrip()
    return bool(_BULLET_START.match(line) or s.startswith(("|", "#", ">", "```")) or line.startswith(("    ", "\t")))


def join_wrapped(text: str) -> str:
    """The lines of a paragraph that the page wrapped become one line. A line that starts a list item, a table row, a quote or an indented block
    starts a new line; a paragraph break (a blank line) stays."""
    out: List[str] = []
    for para in re.split(r"\n\s*\n", text):
        built: List[str] = []
        for line in para.split("\n"):
            if built and not _is_structure(line) and not _is_structure(built[-1]) and built[-1].strip():
                built[-1] = built[-1].rstrip() + " " + line.lstrip()
            else:
                built.append(line)
        out.append("\n".join(built))
    return "\n\n".join(out)


def letter_ratio(line: str) -> float:
    """Letters and digits as a share of the characters that are not white space (0 for an empty line)."""
    chars = [c for c in line if not c.isspace()]
    return sum(c.isalnum() for c in chars) / len(chars) if chars else 0.0


def is_garbage(line: str) -> bool:
    """A line that is mostly noise from text recognition: very few letters, or a run of single characters."""
    s = line.strip()
    if len(s) < 3:
        return False
    if letter_ratio(s) < 0.45:
        return True
    tokens = s.split()
    if len(tokens) < 5:
        return False
    if sum(len(t) == 1 for t in tokens) / len(tokens) > 0.6:
        return True
    odd = sum(any(c.isupper() for c in t) or not any(c.isalpha() for c in t) for t in tokens)
    return sum(map(len, tokens)) / len(tokens) < 2.2 and odd / len(tokens) >= 0.5       # many tiny fragments in capitals or marks


def drop_garbage(text: str) -> str:
    """The lines of running text that are noise, out; tables and code are not passed here."""
    return "\n".join(line for line in text.split("\n") if not is_garbage(line))


def normalize_text(text: str, wrapped: bool = True) -> str:
    """The whole chain for a piece of running text. With `wrapped` False the line breaks are kept (lists)."""
    text = dehyphenate(clean_chars(text))
    text = drop_garbage(text)
    if wrapped:
        text = join_wrapped(text)
    return collapse_space(text)


def clean_code(text: str) -> str:
    """Code: only control characters and line ends."""
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip("\n")


def clean_table(text: str) -> str:
    """A Markdown table with the padding its cells were written with taken out (a cell keeps its words; the separator row becomes `---`)."""
    rows: List[str] = []
    for line in clean_code(text).split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and any(cells) and all(re.fullmatch(r":?-{2,}:?", c) or not c for c in cells):
            rows.append("| " + " | ".join("---" for _ in cells) + " |")
        else:
            rows.append("| " + " | ".join(re.sub(r"\s+", " ", c) for c in cells) + " |")
    return "\n".join(rows)


def is_page_number(text: str) -> bool:
    return bool(text) and len(text) <= 24 and "\n" not in text.strip() and bool(_PAGE_NUMBER.match(text))


def _shape(text: str) -> str:
    """A line with its numbers and its case taken out: `Page 12 of 300` and `Page 13 of 300` are the same header."""
    return _DIGITS.sub("#", re.sub(r"\s+", " ", text.strip().lower()))


def repeating_edges(blocks: Sequence[Dict[str, Any]]) -> set:
    """The shapes of the blocks that sit at the top or the bottom of many pages: running headers and footers."""
    pages: Dict[int, List[Dict[str, Any]]] = {}
    for b in blocks:
        if b.get("type") != "page_break" and b.get("page"):
            pages.setdefault(int(b["page"]), []).append(b)
    if len(pages) < MIN_PAGES_REPEAT:
        return set()
    seen: Dict[str, set] = {}
    for number, items in pages.items():
        items = [b for b in items if str(b.get("text", "")).strip()]
        if len(items) < MIN_PAGE_BLOCKS:
            continue
        for b in items[:EDGE] + items[-EDGE:]:
            if b.get("type") in KEEP_TYPES or len(b["text"]) > 160:
                continue
            seen.setdefault(_shape(b["text"]), set()).add(number)
    need = max(MIN_PAGES_REPEAT, int(REPEAT_SHARE * len(pages) + 0.999))
    return {shape for shape, where in seen.items() if len(where) >= need and shape not in ("", "#")}


def normalize_blocks(blocks: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """The blocks cleaned, and what was done: `(blocks, {dropped_repeats, dropped_numbers, dropped_garbage})`. Page numbers and the running headers
    and footers are dropped, tables and code are kept as they are, a block left empty is dropped."""
    repeats = repeating_edges(blocks)
    stats = {"dropped_repeats": 0, "dropped_numbers": 0, "dropped_garbage": 0}
    by_page: Dict[int, List[int]] = {}
    for i, b in enumerate(blocks):
        if b.get("page") and b.get("type") != "page_break":
            by_page.setdefault(int(b["page"]), []).append(i)
    edge_index = {i for idx in by_page.values() for i in idx[:EDGE] + idx[-EDGE:]}
    short = {i for idx in by_page.values() if len(idx) < MIN_PAGE_BLOCKS for i in idx}
    out: List[Dict[str, Any]] = []
    for i, b in enumerate(blocks):
        kind = b.get("type", "paragraph")
        if kind == "page_break":
            out.append(dict(b))
            continue
        raw = str(b.get("text") or "")
        if kind in KEEP_TYPES:
            text = clean_table(raw) if kind == "table" else clean_code(raw)
        else:
            text = normalize_text(raw, wrapped=kind in ("paragraph", "caption", "footnote", "heading"))
            if not text and raw.strip():
                stats["dropped_garbage"] += 1
        if not text.strip():
            continue
        if kind not in KEEP_TYPES and i in edge_index:
            if is_page_number(text):
                stats["dropped_numbers"] += 1
                continue
            if i not in short and _shape(text) in repeats:
                stats["dropped_repeats"] += 1
                continue
        out.append({**b, "text": text})
    return out, stats
