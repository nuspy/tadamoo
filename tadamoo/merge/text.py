"""Three-way merge of text, line by line: what the person changed in the installed copy and what the new version changed, against the version both started from.

A region only one side changed is taken from that side; a region both changed the same way is taken once; a region both changed differently is a conflict, kept apart
so that the caller decides. Nothing here writes a file or calls a model."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

UNCHANGED, SAME, OURS, THEIRS, CONFLICT = "unchanged", "same", "ours", "theirs", "conflict"


@dataclass
class Chunk:
    """A run of lines and who it comes from. A conflict carries the three versions of the region."""
    kind: str
    lines: List[str] = field(default_factory=list)
    base: List[str] = field(default_factory=list)
    ours: List[str] = field(default_factory=list)
    theirs: List[str] = field(default_factory=list)


@dataclass
class TextMerge:
    chunks: List[Chunk]

    @property
    def conflicts(self) -> int:
        return sum(1 for c in self.chunks if c.kind == CONFLICT)

    @property
    def clean(self) -> bool:
        return self.conflicts == 0

    def render(self, conflict: str = "ours") -> str:
        """The merged text. A conflict is written as the installed side (`ours`), the new side (`theirs`), or both between markers (`markers`)."""
        out: List[str] = []
        for c in self.chunks:
            if c.kind != CONFLICT:
                out += c.lines
            elif conflict == "theirs":
                out += c.theirs
            elif conflict == "markers":
                out += ["<<<<<<< installed\n", *_ended(c.ours), "=======\n", *_ended(c.theirs), ">>>>>>> new\n"]
            else:
                out += c.ours
        return "".join(out)


def _ended(lines: Sequence[str]) -> List[str]:
    """The lines of a conflict side, the last one ending in a newline so that a marker line never joins it."""
    out = list(lines)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    return out


def _sync_regions(base: Sequence[str], a: Sequence[str], b: Sequence[str]) -> List[Tuple[int, int, int, int, int, int]]:
    """The runs of lines that both sides kept from the base: (base start, end, a start, end, b start, end), closed by an empty run at the very end."""
    amatches = difflib.SequenceMatcher(None, base, a, autojunk=False).get_matching_blocks()
    bmatches = difflib.SequenceMatcher(None, base, b, autojunk=False).get_matching_blocks()
    out: List[Tuple[int, int, int, int, int, int]] = []
    ia = ib = 0
    while ia < len(amatches) and ib < len(bmatches):
        abase, amatch, alen = amatches[ia]
        bbase, bmatch, blen = bmatches[ib]
        start, end = max(abase, bbase), min(abase + alen, bbase + blen)
        if start < end:
            asub, bsub = amatch + (start - abase), bmatch + (start - bbase)
            out.append((start, end, asub, asub + (end - start), bsub, bsub + (end - start)))
        if abase + alen < bbase + blen:
            ia += 1
        else:
            ib += 1
    out.append((len(base), len(base), len(a), len(a), len(b), len(b)))
    return out


def merge_lines(base: Sequence[str], ours: Sequence[str], theirs: Sequence[str]) -> TextMerge:
    chunks: List[Chunk] = []
    iz = ia = ib = 0
    for zmatch, zend, amatch, aend, bmatch, bend in _sync_regions(base, ours, theirs):
        if amatch - ia or bmatch - ib:
            old, mine, new = list(base[iz:zmatch]), list(ours[ia:amatch]), list(theirs[ib:bmatch])
            if mine == new:
                chunks.append(Chunk(SAME, mine))
            elif mine == old:
                chunks.append(Chunk(THEIRS, new))
            elif new == old:
                chunks.append(Chunk(OURS, mine))
            else:
                chunks.append(Chunk(CONFLICT, base=old, ours=mine, theirs=new))
        if zend > zmatch:
            chunks.append(Chunk(UNCHANGED, list(base[zmatch:zend])))
        ia, ib, iz = aend, bend, zend
    return TextMerge(chunks)


def split(text: str) -> List[str]:
    return text.splitlines(keepends=True)


def merge_text(base: str, ours: str, theirs: str) -> TextMerge:
    return merge_lines(split(base), split(ours), split(theirs))
