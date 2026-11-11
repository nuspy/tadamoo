"""The document model: a document is a list of blocks, whatever read it.

    {"type": "heading" | "paragraph" | "list" | "table" | "code" | "caption" | "footnote" | "page_break",
     "text": str,            # a table as Markdown, a list as lines starting with "- ", code as it is
     "page": int,            # 1-based; 0 when the reader does not know
     "level": int}           # headings only: 1 is the top

A `page_break` block (no text) marks the start of the page its `page` names. Markdown with `<!-- page -->` markers is read into blocks and
written back from them, so a document that was read once can be given again for any range of pages."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

TYPES = ("heading", "paragraph", "list", "table", "code", "caption", "footnote", "page_break")
_COMMENT = re.compile(r"^\s*<!--\s*(page|image)?\s*(?:\d+)?\s*-->\s*$", re.I)
_PAGE_COMMENT = re.compile(r"^\s*<!--\s*page(?:\s+(\d+))?\s*-->\s*$", re.I)
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+\S")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CAPTION = re.compile(r"^\s*(?:fig(?:ure)?\.?|table|tab\.|tabella|figura|abbildung|tableau|图|表)\s*\d+[.:)]?\s", re.I)


def block(kind: str, text: str, page: int = 0, level: int = 0) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": kind, "text": text, "page": int(page)}
    if kind == "heading":
        out["level"] = max(1, min(6, int(level) or 1))
    return out


def page_break(page: int) -> Dict[str, Any]:
    return {"type": "page_break", "text": "", "page": int(page)}


def markdown_to_blocks(markdown: str, first_page: int = 1) -> List[Dict[str, Any]]:
    """The blocks of Markdown text. `<!-- page -->` starts the next page; `<!-- image -->` and other comments are dropped; a fenced block is code;
    consecutive lines that start with `|` are a table and those that start with a bullet or a number are one list."""
    out: List[Dict[str, Any]] = []
    page = first_page
    para: List[str] = []
    mode, buf = "", []                       # "list", "table" or "code" while one is being read

    def flush_para() -> None:
        if para:
            text = "\n".join(para).strip()
            if text:
                out.append(block("caption" if _CAPTION.match(text) and len(text) < 240 else "paragraph", text, page))
            para.clear()

    def flush_mode() -> None:
        nonlocal mode, buf
        if buf:
            out.append(block(mode, "\n".join(buf).strip("\n") if mode == "code" else "\n".join(l.rstrip() for l in buf), page))
        mode, buf = "", []

    for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if mode == "code":
            if _FENCE.match(line):
                flush_mode()
            else:
                buf.append(line)
            continue
        found = _PAGE_COMMENT.match(line)
        if found:
            flush_para()
            flush_mode()
            page = int(found.group(1)) if found.group(1) else page + 1
            out.append(page_break(page))
            continue
        if _COMMENT.match(line) or (line.strip().startswith("<!--") and line.strip().endswith("-->")):
            flush_para()
            flush_mode()
            continue
        if _FENCE.match(line):
            flush_para()
            flush_mode()
            mode, buf = "code", []
            continue
        stripped = line.strip()
        if not stripped:
            flush_para()
            flush_mode()
            continue
        head = _HEADING.match(line)
        if head:
            flush_para()
            flush_mode()
            out.append(block("heading", head.group(2), page, len(head.group(1))))
            continue
        if stripped.startswith("|"):
            flush_para()
            if mode != "table":
                flush_mode()
                mode = "table"
            buf.append(line)
            continue
        if _LIST_ITEM.match(line):
            flush_para()
            if mode != "list":
                flush_mode()
                mode = "list"
            buf.append(line)
            continue
        if mode == "list" and line.startswith((" ", "\t")):          # the continuation of an item
            buf[-1] = buf[-1].rstrip() + " " + stripped
            continue
        flush_mode()
        para.append(line)
    flush_para()
    flush_mode()
    return out


def has_pages(blocks: Iterable[Dict[str, Any]]) -> bool:
    return any(b.get("page") for b in blocks)


def page_count(blocks: Sequence[Dict[str, Any]]) -> int:
    return max((int(b.get("page") or 0) for b in blocks), default=0)


def _render(b: Dict[str, Any]) -> str:
    kind = b.get("type")
    if kind == "heading":
        return "#" * max(1, min(6, int(b.get("level") or 1))) + " " + b["text"]
    if kind == "code":
        return "```\n" + b["text"] + "\n```"
    return b["text"]


def blocks_to_markdown(blocks: Sequence[Dict[str, Any]], first: int = 1, last: Optional[int] = None, markers: bool = True) -> str:
    """The Markdown of the blocks of pages `first` to `last`, each page after a `<!-- page N -->` line. Blocks without a page are all in page 1."""
    parts: List[str] = []
    shown = 0
    for b in blocks:
        page = int(b.get("page") or 1)
        if page < first or (last is not None and page > last):
            continue
        if markers and page != shown:
            parts.append(f"<!-- page {page} -->")
            shown = page
        if b.get("type") == "page_break":
            continue
        parts.append(_render(b))
    return "\n\n".join(parts)


def parse_pages(spec: Any, total: int) -> tuple:
    """`(first, last)` from `"1-20"`, `"5"`, `"3-"` or `"-4"` (the last four... pages up to 4): clipped to `1..total`. Raises ValueError for text that is not a range."""
    text = str(spec if spec not in (None, "") else "1-20").strip().replace(" ", "")
    found = re.fullmatch(r"(\d*)(?:-(\d*))?", text)
    if not found or not text:
        raise ValueError("pages: a number or a range such as 1-20")
    a, b = found.group(1), found.group(2)
    if b is None:                                       # a single page
        first = last = int(a)
    else:
        first, last = int(a) if a else 1, int(b) if b else total
    if first < 1 or last < first:
        raise ValueError("pages: a range such as 1-20, counted from 1")
    return min(first, max(total, 1)), min(last, total) if total else last
