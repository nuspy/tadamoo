"""The pieces of a text file: the map of its sections, a section or a range of words as a block of whole lines, and the edits that act on a block
by its place. A block is a run of whole lines (a paragraph of prose is a line). Nothing here reads or writes a file."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
FENCE = re.compile(r"^\s*(```|~~~)")
WORD = re.compile(r"\S+")
MAP_ROWS = 200                      # most sections one map lists; a longer file lists its top levels only
ACTIONS = ("replace", "delete", "insert_after", "insert_before", "move_after")


class PieceError(ValueError):
    """A request for a block that cannot be met; the message says what is there instead."""


@dataclass(frozen=True)
class Section:
    level: int
    title: str
    first: int                      # 1-based, inclusive: the heading line
    last: int                       # inclusive: the line before the next heading of the same or a higher level, or the last line
    words: int


def lines_of(text: str) -> List[str]:
    """The lines of a text with LF endings; a final newline does not make an empty last line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def text_of(lines: List[str]) -> str:
    return "\n".join(lines) + ("\n" if lines else "")


def version(text: str) -> str:
    """Short name of a file's current content: a line number is only good for the version it was read from."""
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=4).hexdigest()


def count_words(text: str) -> int:
    return len(WORD.findall(text))


def sections_of(lines: List[str]) -> List[Section]:
    """The headed sections of a Markdown text: a heading inside a code fence is not one."""
    found: List[Tuple[int, str, int]] = []
    fenced = False
    for n, line in enumerate(lines, 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else HEADING.match(line)
        if m:
            found.append((len(m.group(1)), m.group(2).strip(), n))
    words = [count_words(line) for line in lines]
    out: List[Section] = []
    for i, (level, title, first) in enumerate(found):
        last = len(lines)
        for level2, _t, first2 in found[i + 1:]:
            if level2 <= level:
                last = first2 - 1
                break
        out.append(Section(level, title, first, last, sum(words[first - 1:last])))
    return out


def render_map(lines: List[str], sections: List[Section], ver: str) -> str:
    """The map of a file: its size and version, then each section with its lines and words."""
    total = sum(count_words(line) for line in lines)
    head = f"{len(lines)} lines, {total} words, version {ver}"
    if not sections:
        blank = sum(1 for line in lines if not line.strip())
        return (head + f"\nNo headings; {len(lines) - blank} non-empty lines. Read a range with words=[from, to] or offset and limit."
                + f"\n[version {ver}]")
    shown, note = sections, ""
    if len(sections) > MAP_ROWS:
        top = min(s.level for s in sections)
        shown = [s for s in sections if s.level <= top + 1][:MAP_ROWS]
        note = f"\n[{len(sections)} sections: the top levels are listed; read a section by its title to see what is inside it]"
    rows = [f"{s.first}-{s.last}\t{s.words}w\t{'  ' * (s.level - 1)}{'#' * s.level} {s.title}" for s in shown]
    return head + "\n" + "\n".join(rows) + note + f"\n[version {ver}]"


def find_section(sections: List[Section], name: str) -> Section:
    """The section a title names: the whole title ignoring case, else the start of one, else a part of one; it must be a single one."""
    want = re.sub(r"^#+\s*", "", str(name or "")).strip().casefold()
    if not want:
        raise PieceError("A section is named by its title (the map lists them).")
    for test in (lambda t: t == want, lambda t: t.startswith(want), lambda t: want in t):
        hits = [s for s in sections if test(s.title.casefold())]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            shown = "; ".join(f"{s.first}-{s.last} {s.title}" for s in hits[:8])
            raise PieceError(f"'{name}' fits {len(hits)} sections ({shown}). Give more of the title or use lines=[first, last].")
    if not sections:
        raise PieceError("This file has no headings; use lines=[first, last] or words=[from, to].")
    raise PieceError(f"No section is called '{name}'. The map lists them: read_file with outline=true.")


def block_for_words(lines: List[str], start: int, end: int) -> Tuple[int, int, int, int]:
    """The whole lines that hold words `start` to `end` (counted from 0): (first line, last line, first word of the block, words in the file).
    The block begins at the line holding word `start` and ends at the line holding the word before `end`."""
    if start < 0 or end <= start:
        raise PieceError("words=[from, to] counts from 0 and `to` is above `from`.")
    first = last = 0
    begin = at = 0
    for n, line in enumerate(lines, 1):
        w = count_words(line)
        if w and not first and at + w > start:
            first, begin = n, at
        if w and first and at < end:
            last = n
        at += w
    if not first:
        raise PieceError(f"The file has {at} words; words=[{start}, {end}] is past the end.")
    return first, last, begin, at


def unit(lines: List[str], sections: List[Section], lines_arg: Any = None, section: Any = None) -> Tuple[int, int]:
    """The block a request names, as (first, last) lines: `lines` ([first, last], 1-based) or `section` (a title); exactly one of them."""
    if (lines_arg is None) == (not section):
        raise PieceError("Name the block with lines=[first, last] or with section (a title), one of them.")
    if section:
        s = find_section(sections, section)
        return s.first, s.last
    if (not isinstance(lines_arg, (list, tuple)) or len(lines_arg) != 2 or any(isinstance(x, bool) or not isinstance(x, int) for x in lines_arg)):
        raise PieceError("lines is two whole numbers: the first and the last line (1-based, both included).")
    first, last = lines_arg
    if first < 1 or last < first or last > len(lines):
        raise PieceError(f"lines=[{first}, {last}] is outside the file: it has {len(lines)} lines.")
    return first, last


def apply(lines: List[str], action: str, first: int, last: int, new: Optional[List[str]] = None, after: Optional[int] = None) -> Tuple[List[str], int, int]:
    """`lines` after the action on lines `first` to `last`, and the lines the changed place has now (first, last; 0, 0 for nothing left there).
    `after` is the line a moved block goes behind (0: the start of the file)."""
    new = new or []
    if action == "replace":
        out = lines[:first - 1] + new + lines[last:]
        return out, first, first + len(new) - 1 if new else 0
    if action == "delete":
        return lines[:first - 1] + lines[last:], 0, 0
    if action == "insert_after":
        out = lines[:last] + new + lines[last:]
        return out, last + 1, last + len(new)
    if action == "insert_before":
        out = lines[:first - 1] + new + lines[first - 1:]
        return out, first, first + len(new) - 1
    if action == "move_after":
        if after is None:
            raise PieceError("move_after needs `after`: the line, or the section, the block goes behind.")
        if first - 1 <= after <= last:
            raise PieceError(f"The block (lines {first}-{last}) cannot be moved into itself.")
        block = lines[first - 1:last]
        rest = lines[:first - 1] + lines[last:]
        at = after if after < first else after - len(block)
        out = rest[:at] + block + rest[at:]
        return out, at + 1, at + len(block)
    raise PieceError(f"action is one of: {', '.join(ACTIONS)}.")


def block_lines(text: str) -> List[str]:
    """The lines a text given for an edit adds; no text is no lines."""
    return lines_of(str(text).replace("\r\n", "\n")) if text else []


def numbered(lines: List[str], first: int) -> str:
    return "\n".join(f"{first + i:>6}\t{line}" for i, line in enumerate(lines))


def words_block(lines: List[str], start: int, end: int) -> Dict[str, Any]:
    """What a read of words `start` to `end` returns: the block's lines and where they are."""
    first, last, begin, total = block_for_words(lines, start, min(end, math.inf))
    return {"first": first, "last": last, "begin": begin, "total_words": total, "lines": lines[first - 1:last]}
