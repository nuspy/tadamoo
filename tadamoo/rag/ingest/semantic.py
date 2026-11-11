"""Cutting where the meaning changes. The structural cut (`chunker.py`) follows headings, blocks and a size; this one also reads the text: it embeds the units of a
section (a paragraph, a list, a sentence of a very long paragraph) and starts a new passage where the topic shifts (a valley in the similarity of the units before and
after), so a passage holds one thing whatever its length. Headings still end a section, a table is one passage (cut by rows, with its header, only beyond
`table_max`), and a passage is cut by force only at `max_tokens`, which is there for the models' input and not to keep passages small.

Two seams: `sentence_valleys(embed)` is the `valleys` of `chunker.split_prose` (long paragraphs only, everything else as before), and `semantic_chunks` replaces the
cutting of a whole document. `embed` is any function from a list of texts to a matrix of unit-length rows (the archive's embedder)."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from tadamoo.rag.ingest import chunker
from tadamoo.rag.ingest.chunker import SEP, Section, _clip, split_table, tokens

VERSION = 1
Embed = Callable[[List[str]], np.ndarray]
DEFAULTS = {"window": 2, "sensitivity": 0.5, "soft_min": 120, "max_tokens": 2000, "table_max": 2400, "merge_tau": 0.55, "long_unit": 500}


def window_similarity(vectors: np.ndarray, window: int) -> np.ndarray:
    """For each gap i (between unit i-1 and unit i, 1 <= i < n) the cosine of the mean of the `window` units before it and the `window` units after it."""
    n = len(vectors)
    out = np.zeros(max(n - 1, 0), dtype=np.float32)
    for i in range(1, n):
        a = vectors[max(0, i - window):i].mean(axis=0)
        b = vectors[i:i + window].mean(axis=0)
        out[i - 1] = float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))
    return out


def valleys_of(vectors: np.ndarray, window: int = 2, sensitivity: float = 0.5) -> List[int]:
    """Indexes of the units that start a new topic: gaps whose window similarity is more than `sensitivity` standard deviations under the mean of the section and is
    a local minimum."""
    if len(vectors) < 3:
        return []
    sim = window_similarity(vectors, window)
    if len(sim) < 2:
        return []
    cut = float(sim.mean() - sensitivity * sim.std())
    out = []
    for g in range(len(sim)):
        left = sim[g - 1] if g else np.inf
        right = sim[g + 1] if g + 1 < len(sim) else np.inf
        if sim[g] < cut and sim[g] <= left and sim[g] <= right:
            out.append(g + 1)
    return out


def sentence_valleys(embed: Embed, window: int = 2, sensitivity: float = 0.5) -> Callable[[List[str]], List[int]]:
    """The `valleys` of `chunker.split_prose`: the sentences of one long paragraph that start a new topic."""
    def valleys(sentences: List[str]) -> List[int]:
        if len(sentences) < 4:
            return []
        return valleys_of(np.asarray(embed(list(sentences)), dtype=np.float32), window, sensitivity)
    return valleys


class _Unit:
    __slots__ = ("text", "page", "kind", "flag", "para", "size")

    def __init__(self, text: str, page: int, kind: str, flag: str, para: int) -> None:
        self.text, self.page, self.kind, self.flag, self.para, self.size = text, page, kind, flag, para, tokens(text)


def _join(units: Sequence[_Unit]) -> str:
    out = ""
    for i, u in enumerate(units):
        out += (("\n\n" if units[i - 1].para != u.para else " ") if i else "") + u.text
    return out


def _units(blocks_of_section: Sequence[Tuple[str, str, int, str]], cfg: Dict[str, Any]) -> List[_Unit]:
    """The units of a section's blocks: a block as it is, a very long paragraph as its sentences."""
    units: List[_Unit] = []
    for para, (kind, text, page, flag) in enumerate(blocks_of_section):
        if kind == "paragraph" and tokens(text) > cfg["long_unit"]:
            units += [_Unit(s, page, "text", flag, para) for s in chunker.sentences(text)]
        else:
            units.append(_Unit(text, page, kind, flag, para))
    return units


def _group(units: List[_Unit], vectors: np.ndarray, cfg: Dict[str, Any]) -> List[List[int]]:
    """Indexes of the units of each passage: solid units (tables, code) alone; the rest cut at valleys once `soft_min` is reached, by force at `max_tokens`,
    then small neighbours that are close in meaning merged."""
    cuts = set(valleys_of(vectors, cfg["window"], cfg["sensitivity"])) if len(units) > 2 else set()
    groups: List[List[int]] = []
    cur: List[int] = []
    size = 0
    for i, u in enumerate(units):
        solid = u.kind in ("table", "code")
        if cur and (solid or units[cur[-1]].kind in ("table", "code") or units[cur[-1]].flag != u.flag or size + u.size > cfg["max_tokens"]
                    or (i in cuts and size >= cfg["soft_min"])):
            groups.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += u.size
    if cur:
        groups.append(cur)
    def solid(g: List[int]) -> bool:
        return any(units[i].kind in ("table", "code") for i in g)

    def size_of(g: List[int]) -> int:
        return sum(units[i].size for i in g)

    def close(g: List[int], h: List[int]) -> bool:
        if solid(g) or solid(h) or units[g[0]].flag != units[h[0]].flag or size_of(g) + size_of(h) > cfg["max_tokens"]:
            return False
        a, b = vectors[g].mean(axis=0), vectors[h].mean(axis=0)
        return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0)) >= cfg["merge_tau"]

    out: List[List[int]] = []
    for g in groups:                                     # a small passage joins the one before it when they are close in meaning
        if out and (size_of(g) < cfg["soft_min"] or size_of(out[-1]) < cfg["soft_min"]) and close(out[-1], g):
            out[-1] = out[-1] + g
        else:
            out.append(g)
    groups = out
    return groups


def semantic_chunks(blocks: Sequence[Dict[str, Any]], title: str, embed: Embed, **options: Any) -> Dict[str, Any]:
    """`{"chunks", "sections"}` as `chunker.chunk_blocks` gives them, cut at semantic valleys instead of by size (see the module text). Options: `window`, `sensitivity`,
    `soft_min`, `max_tokens`, `table_max`, `merge_tau`, `long_unit`."""
    cfg = {**DEFAULTS, **options}
    title = _clip(title) or "document"
    root = Section("s0", "", title, 0, 0)
    sections: List[Section] = [root]
    stack: List[Tuple[int, Section]] = [(0, root)]
    pending: List[Tuple[Section, List[Tuple[str, str, int, str]]]] = [(root, [])]
    for b in blocks:
        kind = b.get("type", "paragraph")
        text = str(b.get("text") or "").strip()
        page = int(b.get("page") or 0)
        if kind == "page_break" or not text:
            continue
        if kind == "heading":
            level = max(1, int(b.get("level") or 1))
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            section = Section(f"s{len(sections)}", stack[-1][1].id, _clip(stack[-1][1].path + SEP + _clip(text)), level, page)
            sections.append(section)
            stack.append((level, section))
            pending.append((section, []))
            continue
        kind = kind if kind in ("table", "code", "list") else "paragraph"
        pending[-1][1].append((kind, text, page, str(b.get("flag") or "")))
        pending[-1][0].add(text, page, str(b.get("flag") or ""))
    chunks: List[Dict[str, Any]] = []

    def emit(text: str, kind: str, pages: Tuple[int, int], section: Section, flag: str) -> None:
        if text.strip():
            chunks.append({"text": text.strip(), "path": section.path, "page_start": pages[0], "page_end": pages[1], "kind": kind, "section": section.id,
                            **({"flag": flag} if flag else {})})

    todo = [(section, _units(items, cfg)) for section, items in pending if items]
    everything = np.asarray(embed([u.text for _, units in todo for u in units]), dtype=np.float32) if todo else np.zeros((0, 1), dtype=np.float32)     # one call for the whole document
    at = 0
    for section, units in todo:
        vectors = everything[at:at + len(units)]
        at += len(units)
        for group in _group(units, vectors, cfg):
            members = [units[i] for i in group]
            pages = [u.page for u in members if u.page]
            span = (min(pages, default=0), max(pages, default=0))
            first = members[0]
            if first.kind == "table":
                for piece in split_table(first.text, cfg["table_max"], cfg["table_max"]):
                    emit(piece, "table", span, section, first.flag)
            elif first.kind == "code":
                emit(first.text, "code", span, section, first.flag)
            else:
                emit(_join(members), "list" if all(u.kind == "list" for u in members) else "text", span, section, first.flag)
    return {"chunks": chunks, "sections": [s.record() for s in sections if s.texts]}
