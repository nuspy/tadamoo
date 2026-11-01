"""Splits a long Markdown text into pieces of at most `limit` characters.

A piece never ends inside a fenced code block, a table or a list item while a boundary outside them is available. Boundaries are preferred in this
order: between blocks (paragraphs, headings, list items, code blocks, tables), between lines, between sentences, anywhere. A code block or table that
alone is longer than the limit is cut at line boundaries: each piece of a code block closes the fence and the next one opens it again with the same
marker and language; each piece of a table after the first repeats the header and delimiter rows.

Apart from those added fence and header lines, the pieces joined give back the text exactly. A code line or table row longer than the room a piece
has is cut: in a code block the cut ends the line in that piece (the fence closes on the next line), in a table the row is cut as plain text."""
from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

Part = Tuple[str, str]                      # (kind, text): kind is "fence", "table" or "prose"

_FENCE_OPEN = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")
_TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_LIST_ITEM = re.compile(r"^([ \t]*)(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])(\s+)")


def split_markdown(text: str, limit: int) -> List[str]:
    """The pieces of `text`, each at most `limit` characters. Raises ValueError when `limit` is below 1 or too small to hold a fence's or table header's own lines."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if len(text) <= limit:
        return [text] if text else []
    return _pack(_blocks(text), limit, lambda kind, core: _split_block(kind, core, limit))


# -- reading the blocks -----------------------------------------------------------------------------------------------

def _indent(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())


def _opener(line: str) -> Optional[Tuple[str, str]]:
    """(indent, marker) when the line opens a fenced code block."""
    m = _FENCE_OPEN.match(line.rstrip("\r\n"))
    if not m or (m.group(2)[0] == "`" and "`" in m.group(3)):
        return None
    return m.group(1), m.group(2)


def _closes(line: str, marker: str) -> bool:
    s = line.strip()
    return len(s) >= len(marker) and set(s) == {marker[0]}


def _fence_end(lines: List[str], i: int, marker: str) -> int:
    """The index after the line that closes the fence opened at lines[i]; the end of the text when it never closes."""
    for j in range(i + 1, len(lines)):
        if _closes(lines[j], marker):
            return j + 1
    return len(lines)


def _table_at(lines: List[str], i: int) -> bool:
    return i + 1 < len(lines) and "|" in lines[i] and "|" in lines[i + 1] and bool(_TABLE_DELIMITER.match(lines[i + 1].rstrip("\r\n")))


def _starts_block(lines: List[str], i: int) -> bool:
    """A line that cannot continue a paragraph or a list item's lazy text."""
    line = lines[i]
    o = _opener(line)
    return bool((o and len(o[0]) <= 3) or _LIST_ITEM.match(line) or _HEADING.match(line) or _table_at(lines, i))


def _blocks(text: str) -> List[Part]:
    """The text as blocks in order, each with the blank lines after it."""
    lines = text.splitlines(keepends=True)
    out: List[Part] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        o = _opener(line)
        if not line.strip():
            j, kind = i + 1, "prose"
        elif o and len(o[0]) <= 3:
            j, kind = _fence_end(lines, i, o[1]), "fence"
        elif _table_at(lines, i):
            j, kind = i + 2, "table"
            while j < len(lines) and lines[j].strip() and "|" in lines[j] and not (_opener(lines[j]) or _HEADING.match(lines[j])):
                j += 1
        elif _LIST_ITEM.match(line):
            j, kind = _item_end(lines, i), "prose"
        elif _HEADING.match(line):
            j, kind = i + 1, "prose"
        else:
            j, kind = i + 1, "prose"
            while j < len(lines) and lines[j].strip() and not _starts_block(lines, j):
                j += 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        out.append((kind, "".join(lines[i:j])))
        i = j
    return out


def _item_end(lines: List[str], i: int) -> int:
    """The index after the list item that starts at lines[i]: its indented lines, nested items and fences, and its lazy continuation lines."""
    n = _indent(lines[i])
    j = i + 1
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k == len(lines) or _indent(lines[k]) <= n:
                break
            j = k
            continue
        if _indent(line) > n:
            o = _opener(line)
            j = _fence_end(lines, j, o[1]) if o else j + 1
            continue
        if _starts_block(lines, j) or not lines[j - 1].strip():
            break
        j += 1
    return j


# -- packing ------------------------------------------------------------------------------------------------------------

def _pack(parts: List[Part], limit: int, finer: Callable[[str, str], List[str]]) -> List[str]:
    """Parts put one after the other into pieces; a part longer than the limit goes to `finer`. The whitespace after a part is carried like text, so it may
    start the next piece when the room is gone."""
    out: List[str] = []
    cur = ""

    def carry(white: str) -> None:
        nonlocal cur
        while white:
            if len(cur) >= limit:
                out.append(cur)
                cur = ""
            room = limit - len(cur)
            cur += white[:room]
            white = white[room:]

    for kind, text in parts:
        core = text.rstrip()
        white = text[len(core):]
        if len(core) <= limit:
            if cur and len(cur) + len(core) > limit:
                out.append(cur)
                cur = ""
            cur += core
        else:
            if cur:
                out.append(cur)
            pieces = finer(kind, core)
            out.extend(pieces[:-1])
            cur = pieces[-1]
        carry(white)
    if cur:
        out.append(cur)
    return out


def _cut(text: str, limit: int) -> List[str]:
    """Pieces of at most `limit` characters, cut after whitespace when there is some in the second half of the room, else anywhere."""
    out = []
    while len(text) > limit:
        at = max(text.rfind(" ", 0, limit), text.rfind("\n", 0, limit), text.rfind("\t", 0, limit)) + 1
        at = at if at > limit // 2 else limit
        out.append(text[:at])
        text = text[at:]
    return [*out, text]


def _greedy(units: List[str], room: int) -> List[str]:
    out: List[str] = []
    cur = ""
    for u in units:
        if cur and len(cur) + len(u) > room:
            out.append(cur)
            cur = ""
        cur += u
    return [*out, cur] if cur else out


def _split_block(kind: str, core: str, limit: int) -> List[str]:
    if kind == "fence":
        return _split_fence(core, limit)
    if kind == "table":
        return _split_table(core, limit)
    return _split_prose(core, limit, 0)


def _split_fence(core: str, limit: int) -> List[str]:
    lines = core.splitlines(keepends=True)
    o = _opener(lines[0])
    if o is None or len(lines) < 2:
        return _cut(core, limit)
    indent, marker = o
    closed = len(lines) > 1 and _closes(lines[-1], marker)
    opener = lines[0] if lines[0].endswith("\n") else lines[0] + "\n"
    body = lines[1:-1] if closed else lines[1:]
    closer = lines[-1] if closed else ""
    shut = indent + marker
    room = limit - len(opener) - max(len(shut), len(closer))
    if room < 2:
        raise ValueError(f"limit {limit} is too small for the lines that close and reopen a code block")
    units: List[str] = []
    for line in body:
        if len(line) <= room:
            units.append(line)
            continue
        text, end = (line[:-1], "\n") if line.endswith("\n") else (line, "")
        parts = _cut(text, room - 1)
        units += [p + "\n" for p in parts[:-1]] + [parts[-1] + end]
    bodies = _greedy(units, room) or [""]
    pieces = []
    for n, b in enumerate(bodies):
        last = n == len(bodies) - 1
        tail = closer if last else shut
        pieces.append(opener + b + tail)
    return pieces


def _split_table(core: str, limit: int) -> List[str]:
    lines = core.splitlines(keepends=True)
    if len(lines) < 3:
        return _cut(core, limit)
    header = lines[0] + lines[1]
    room = limit - len(header)
    if room < 1:
        raise ValueError(f"limit {limit} is too small for the header of a table")
    units: List[str] = []
    for row in lines[2:]:
        units += _cut(row, room) if len(row) > room else [row]
    bodies = _greedy(units, room)
    return [header + b for b in bodies]


def _paragraphs(core: str) -> List[Part]:
    """Groups of lines ending at a blank line outside a fence."""
    atoms = _atoms(core)
    groups: List[List[Part]] = []
    new = True
    for kind, text in atoms:
        if new:
            groups.append([])
        groups[-1].append((kind, text))
        new = kind == "prose" and not text.strip()
    return [("fence" if g[0][0] == "fence" and sum(bool(t.strip()) for _, t in g) == 1 else "prose", "".join(t for _, t in g)) for g in groups]


def _atoms(core: str) -> List[Part]:
    """Single lines, and a whole fenced block (at any indent) as one atom."""
    lines = core.splitlines(keepends=True)
    out: List[Part] = []
    i = 0
    while i < len(lines):
        o = _opener(lines[i])
        j = _fence_end(lines, i, o[1]) if o else i + 1
        out.append(("fence" if o else "prose", "".join(lines[i:j])))
        i = j
    return out


def _sentences(core: str) -> List[Part]:
    bits = _SENTENCE_END.split(core)
    return [("prose", bits[k] + (bits[k + 1] if k + 1 < len(bits) else "")) for k in range(0, len(bits), 2)]


def _split_prose(core: str, limit: int, level: int) -> List[str]:
    """Text with no special block, by paragraph (level 0), line (1), sentence (2), then anywhere."""
    if level > 2:
        return _cut(core, limit)
    parts = (_paragraphs, _atoms, _sentences)[level](core)
    if len(parts) == 1 and parts[0][0] != "fence":
        return _split_prose(core, limit, level + 1)
    return _pack(parts, limit, lambda kind, text: _split_fence(text, limit) if kind == "fence" else _split_prose(text, limit, level + 1))
