"""Cutting a document's blocks into passages that keep their place: the section they are in, the pages they span and what kind of text they are.

The headings of the blocks make a tree of sections. Inside a section the blocks are gathered into passages of about `TARGET` tokens (a token is
about 3.5 characters here) and never more than `HARD`: small neighbouring paragraphs are merged, a list stays whole, a table is one passage (or,
when it is too large, is cut by rows with its header row repeated), code is one passage. A paragraph that is longer than the limit is cut at
sentence ends, and only then does the next piece start with the last sentence of the one before. Each passage carries its `path`
("Document > Section > Subsection"), `page_start`, `page_end`, `kind` (text, list, table or code) and `section` (the id of its section); the
text that is searched is the path and the passage. A section keeps its own text (capped), so that a hit can give the section it came from.

`valleys` of `split_prose` is the seam for a splitter that knows where the meaning changes (a later phase); without it the cuts are the
sentence ends nearest to the size."""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

VERSION = 1
CHARS_PER_TOKEN = 3.5
TARGET = 350                       # tokens a passage grows to
HARD = 600                         # tokens a passage never passes (but for one table row or one sentence that cannot be cut)
MIN_MERGE = 120                    # a passage this small takes the next paragraph even when that brings it past TARGET (up to HARD)
SECTION_CHARS = 8000               # the text a section keeps for a hit to expand to
PATH_CHARS = 300
SEP = " > "
_SENTENCE_END = re.compile(r"(?<=[.!?;:。！？])(?:\s+|(?=[一-鿿぀-ヿ]))")
_SEPARATOR_ROW = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
Valleys = Callable[[List[str]], List[int]]


def tokens(text: str) -> int:
    return int(math.ceil(len(text) / CHARS_PER_TOKEN))


def _chars(tokens_: int) -> int:
    return int(tokens_ * CHARS_PER_TOKEN)


def sentences(text: str) -> List[str]:
    """The sentences of a paragraph, in order, each with its end punctuation; a line break is a sentence break too."""
    out: List[str] = []
    for line in text.split("\n"):
        out += [s for s in (p.strip() for p in _SENTENCE_END.split(line)) if s]
    return out


def _hard_cut(text: str, limit: int) -> List[str]:
    """`text` in pieces of at most `limit` characters, cut at the last space inside the size, else at the size."""
    pieces: List[str] = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind(" ", limit // 2, limit)
        cut = cut + 1 if cut > 0 else limit
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def split_prose(text: str, target: int = TARGET, hard: int = HARD, valleys: Optional[Valleys] = None) -> List[str]:
    """A long paragraph cut into pieces of about `target` tokens at sentence ends; each piece after the first begins with the last sentence of the
    one before. `valleys(sentences)` may return the indexes of the sentences that start a new topic: the cuts are then made there (when no valley fits
    in the size, at the nearest sentence end)."""
    sents = sentences(text)
    if not sents:
        return []
    limit = _chars(hard)
    sents = [piece for s in sents for piece in (_hard_cut(s, limit) if len(s) > limit else [s])]
    starts = set(valleys(sents)) if valleys is not None else set()
    pieces: List[List[str]] = []
    cur: List[str] = []
    used = 0
    for i, s in enumerate(sents):
        size = len(s) + 1
        full = used + size > _chars(target) and cur
        valley = i in starts and cur and used >= _chars(target) // 2
        if full or valley:
            pieces.append(cur)
            keep = cur[-1] if len(cur[-1]) <= _chars(target) // 3 else ""    # the overlap: one sentence, when it is a short one
            cur, used = ([keep], len(keep) + 1) if keep and len(keep) + 1 + size <= _chars(hard) else ([], 0)
        cur.append(s)
        used += size
    if cur:
        pieces.append(cur)
    return [" ".join(p) for p in pieces]


def _is_separator(line: str) -> bool:
    return bool(_SEPARATOR_ROW.match(line)) and "-" in line


def split_table(text: str, target: int = TARGET, hard: int = HARD) -> List[str]:
    """A table as one piece when it fits in `hard` tokens; else pieces of rows of about `target` tokens, each starting with the header row and its
    separator. A single row that is larger than `hard` is cut in pieces of its own."""
    if tokens(text) <= hard:
        return [text]
    lines = [l for l in text.split("\n") if l.strip()]
    head = lines[:2] if len(lines) > 1 and _is_separator(lines[1]) else []
    rows = lines[len(head):]
    header = "\n".join(head)
    pieces: List[str] = []
    cur: List[str] = []
    for row in rows:
        room = _chars(target) - len(header)
        if len(row) > _chars(hard) - len(header):
            if cur:
                pieces.append("\n".join(([header] if header else []) + cur))
                cur = []
            pieces += ["\n".join(([header] if header else []) + [part]) for part in _hard_cut(row, max(200, _chars(hard) - len(header) - 2))]
            continue
        if cur and sum(len(r) + 1 for r in cur) + len(row) > room:
            pieces.append("\n".join(([header] if header else []) + cur))
            cur = []
        cur.append(row)
    if cur:
        pieces.append("\n".join(([header] if header else []) + cur))
    return pieces


def _split_lines(text: str, target: int, hard: int) -> List[str]:
    """Code (or any text) cut at line ends into pieces of about `target` tokens."""
    pieces, cur = [], []
    for line in text.split("\n"):
        if cur and tokens("\n".join(cur + [line])) > target:
            pieces.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        pieces.append("\n".join(cur))
    return [p for piece in pieces for p in (_hard_cut(piece, _chars(hard)) if tokens(piece) > hard else [piece])]


def _split_list(text: str, target: int, hard: int) -> List[str]:
    items: List[str] = []
    for line in text.split("\n"):
        if re.match(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s", line) or not items:
            items.append(line)
        else:
            items[-1] += " " + line.strip()
    pieces, cur = [], []
    for item in items:
        if cur and tokens("\n".join(cur + [item])) > target:
            pieces.append("\n".join(cur))
            cur = []
        cur.append(item)
    if cur:
        pieces.append("\n".join(cur))
    return [p for piece in pieces for p in (_hard_cut(piece, _chars(hard)) if tokens(piece) > hard else [piece])]


class Section:
    def __init__(self, sid: str, parent: str, path: str, level: int, page: int) -> None:
        self.id, self.parent, self.path, self.level = sid, parent, path, level
        self.page_start = self.page_end = page
        self.texts: List[str] = []
        self.size = 0
        self.flag = ""

    def add(self, text: str, page: int, flag: str = "") -> None:
        self.flag = self.flag or flag
        if page:
            self.page_start = min(self.page_start, page) if self.page_start else page
            self.page_end = max(self.page_end, page)
        if self.size < SECTION_CHARS:
            self.texts.append(text)
            self.size += len(text) + 2

    def record(self) -> Dict[str, Any]:
        return {"id": self.id, "parent": self.parent, "path": self.path, "level": self.level, "page_start": self.page_start, "page_end": self.page_end,
                "text": "\n\n".join(self.texts)[:SECTION_CHARS], **({"flag": self.flag} if self.flag else {})}


def _clip(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= PATH_CHARS else text[:PATH_CHARS - 1] + "…"


def chunk_blocks(blocks: Sequence[Dict[str, Any]], title: str, valleys: Optional[Valleys] = None, target: int = TARGET, hard: int = HARD) -> Dict[str, Any]:
    """`{"chunks": [...], "sections": [...]}` for the blocks of one document called `title`. A chunk is `{text, path, page_start, page_end, kind,
    section}`; a section is `{id, parent, path, level, page_start, page_end, text}` (ids are local to the document: `s0` is the document itself). `target` and `hard`
    are the sizes in tokens (the settings `chunk_target` and `chunk_hard` of tadamoo/rag/settings.py)."""
    min_merge = min(MIN_MERGE, target // 2)
    title = _clip(title) or "document"
    root = Section("s0", "", title, 0, 0)
    sections: List[Section] = [root]
    stack: List[Tuple[int, Section]] = [(0, root)]
    chunks: List[Dict[str, Any]] = []
    buf: List[Tuple[str, int, str, str]] = []              # (text, page, kind, flag) waiting to be merged
    state = {"section": root}

    def emit(text: str, kind: str, pages: Tuple[int, int], section: Section, flag: str = "") -> None:
        if text.strip():
            chunks.append({"text": text.strip(), "path": section.path, "page_start": pages[0], "page_end": pages[1], "kind": kind, "section": section.id,
                           **({"flag": flag} if flag else {})})

    def flush() -> None:
        if not buf:
            return
        pages = [p for _, p, _, _ in buf if p]
        kinds = {k for _, _, k, _ in buf}
        emit("\n\n".join(t for t, _, _, _ in buf), "list" if kinds == {"list"} else "text", (min(pages, default=0), max(pages, default=0)), state["section"], buf[0][3])
        buf.clear()

    def unit(kind: str, text: str, page: int, flag: str = "") -> None:
        section = state["section"]
        section.add(text, page, flag)
        if kind == "table":
            flush()
            for piece in split_table(text, target, hard):
                emit(piece, "table", (page, page), section, flag)
            return
        if kind == "code":
            flush()
            for piece in ([text] if tokens(text) <= hard else _split_lines(text, target, hard)):
                emit(piece, "code", (page, page), section, flag)
            return
        size = tokens(text)
        if size > hard:
            flush()
            pieces = _split_list(text, target, hard) if kind == "list" else split_prose(text, target, hard, valleys)
            for piece in pieces:
                emit(piece, "list" if kind == "list" else "text", (page, page), section, flag)
            return
        have = sum(tokens(t) for t, _, _, _ in buf)
        if buf and (buf[0][3] != flag or (have + size > target and not (have < min_merge and have + size <= hard))):
            flush()                                                     # a flagged passage is never merged with one that is not
        buf.append((text, page, "list" if kind == "list" else "text", flag))

    for b in blocks:
        kind = b.get("type", "paragraph")
        text = str(b.get("text") or "").strip()
        page = int(b.get("page") or 0)
        if kind == "page_break" or not text:
            continue
        if kind == "heading":
            flush()
            level = max(1, int(b.get("level") or 1))
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1]
            section = Section(f"s{len(sections)}", parent.id, _clip(parent.path + SEP + _clip(text)), level, page)
            sections.append(section)
            stack.append((level, section))
            state["section"] = section
            continue
        unit(kind if kind in ("table", "code", "list") else "paragraph", text, page, str(b.get("flag") or ""))
    flush()
    return {"chunks": chunks, "sections": [s.record() for s in sections if s.texts]}
