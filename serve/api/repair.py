"""The message that asks for a structured answer again after a rejected try.

The rejected text is never sent back: a model that wrote the same list entry hundreds of times reads it again and
repeats it. The message gives the validator's error first, then only what locates the fault: the JSON path where the
text broke, any entry that was repeated (once, with its count and positions), and a short summary of the keys that
were produced. `build_repair(error, bad)` returns it with the facts it was built from.

Pure standard library; a broken text is read with a tolerant scanner, so the location is found even when the text is
not valid JSON.
"""
from __future__ import annotations

import collections
import json
import re
from dataclasses import dataclass, field

REPEAT_MIN = 3                  # an entry written this often (or more) counts as a repetition
ENTRY_CHARS = 240               # the most of a repeated entry that is quoted
ERROR_CHARS = 500
SUMMARY_KEYS = 12
SUMMARY_CHARS = 700
MAX_DEPTH = 64
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.S)
_LITERAL = re.compile(r"true|false|null")
_SPACE = re.compile(r"\s*")
_SEGMENT = re.compile(r"\\n|\n|(?<=[.!?])\s+")
_KEY_PREFIX = re.compile(r'^[\s,\[{]*(?:"(?:[^"\\]|\\.)*"\s*:\s*)?"?')     # the `{"key": "` in front of a first line


class _Stop(Exception):
    """The scan ended inside a value: at the end of the text (`eof`) or at a character that is not JSON."""

    def __init__(self, pos: int, eof: bool):
        super().__init__(pos)
        self.pos, self.eof = pos, eof


class _Scan:
    """Reads the longest valid JSON prefix of a text, recording where each value of every list starts and ends."""

    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.path: list = []                            # keys and indexes of the value being read
        self.items: dict[tuple, list] = {}              # list path -> (start, end) of each finished entry
        self.top: list = []                             # top-level keys: [key, kind, start, end or None]
        self.stop: _Stop | None = None
        self.no_json = False                            # the text has no object or list at all

    def run(self) -> "_Scan":
        m = re.search(r"[{\[]", self.s)
        if m is None:
            self.stop, self.no_json = _Stop(len(self.s), True), True
            return self
        self.i = m.start()
        try:
            self.value()
        except _Stop as stop:
            self.stop = stop
        except RecursionError:
            self.stop = _Stop(self.i, False)
        return self

    def ws(self):
        self.i = _SPACE.match(self.s, self.i).end()

    def value(self):
        self.ws()
        if self.i >= len(self.s):
            raise _Stop(self.i, True)
        c = self.s[self.i]
        if len(self.path) > MAX_DEPTH:
            raise _Stop(self.i, False)
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c == '"':
            return self.string()
        m = _NUMBER.match(self.s, self.i) or _LITERAL.match(self.s, self.i)
        if m is None:
            # a partial literal or number at the very end is a cut, anything else is not JSON
            rest = self.s[self.i:]
            raise _Stop(self.i, bool(rest) and len(rest) < 6 and any(w.startswith(rest) for w in ("true", "false", "null")))
        self.i = m.end()
        if self.i >= len(self.s):                       # "12" may be the start of "123": a cut
            raise _Stop(self.i, True)

    def string(self) -> str:
        m = _STRING.match(self.s, self.i)
        if m is None:
            raise _Stop(self.i, True)
        self.i = m.end()
        return m.group()

    def obj(self):
        depth = len(self.path)
        self.i += 1
        first = True
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise _Stop(self.i, True)
            if self.s[self.i] == "}" and first:
                self.i += 1
                return
            if self.s[self.i] != '"':
                raise _Stop(self.i, False)
            start = self.i
            raw = self.string()
            try:
                key = json.loads(raw)
            except ValueError:
                raise _Stop(start, False) from None
            self.ws()
            if self.i >= len(self.s):
                raise _Stop(self.i, True)
            if self.s[self.i] != ":":
                raise _Stop(self.i, False)
            self.i += 1
            self.path.append(key)
            self.ws()
            begin = self.i
            row = None
            if depth == 0:
                kind = {"{": "object", "[": "list", '"': "string"}.get(self.s[begin] if begin < len(self.s) else "", "value")
                row = [key, kind, begin, None]
                self.top.append(row)
            self.value()
            if row is not None:
                row[3] = self.i
            self.path.pop()
            first = False
            self.ws()
            if self.i >= len(self.s):
                raise _Stop(self.i, True)
            c = self.s[self.i]
            self.i += 1
            if c == "}":
                return
            if c != ",":
                raise _Stop(self.i - 1, False)

    def arr(self):
        self.i += 1
        here = tuple(self.path)
        items = self.items.setdefault(here, [])
        first = True
        while True:
            self.ws()
            if self.i >= len(self.s):
                if not first:
                    self.path.append(len(items))        # the entry that was about to start
                raise _Stop(self.i, True)
            if self.s[self.i] == "]" and first:
                self.i += 1
                return
            index = len(items)
            self.path.append(index)
            start = self.i
            self.value()
            items.append((start, self.i))
            self.path.pop()
            first = False
            self.ws()
            if self.i >= len(self.s):
                raise _Stop(self.i, True)
            c = self.s[self.i]
            self.i += 1
            if c == "]":
                return
            if c != ",":
                raise _Stop(self.i - 1, False)


def pointer(path) -> str:
    """A JSON pointer such as /implicit_syllabus/3 ("/" is the whole answer)."""
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path)


def _norm(raw: str) -> str:
    """An entry's text with its layout and letter case removed, so the same entry written twice compares equal."""
    try:
        return json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False).casefold()
    except ValueError:
        return " ".join(raw.split()).casefold()


def _ranges(positions: list[int]) -> str:
    """1-based positions as "6-48" or "6-9, 12-48" (at most six runs)."""
    runs, start, prev = [], None, None
    for p in positions:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            runs.append((start, prev))
            start = prev = p
    if start is not None:
        runs.append((start, prev))
    shown = [f"{a}" if a == b else f"{a}-{b}" for a, b in runs[:6]]
    return ", ".join(shown) + (", ..." if len(runs) > 6 else "")


def _shorten(text: str, limit: int = ENTRY_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _entry_text(raw: str) -> str:
    try:
        value = json.loads(raw)
    except ValueError:
        return raw
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


@dataclass
class Repair:
    """The repair message and the facts it was built from (for logs and tests)."""
    message: str
    path: str = "/"                         # JSON pointer where the text broke or ended (the whole answer if valid)
    cut: bool = False                       # the text ended inside a value
    valid_json: bool = False                # the text parsed completely (the fault is the schema, not the syntax)
    repeat: dict | None = None              # {"text", "count", "positions", "path"} of the worst repetition
    keys: list = field(default_factory=list)


def find_repeat(text: str, scan: _Scan | None = None) -> dict | None:
    """The most repeated entry of any list in `text` (or, without lists, the most repeated line or sentence)."""
    scan = scan or _Scan(text).run()
    best = None
    for path, spans in scan.items.items():
        counts = collections.defaultdict(list)
        for n, (a, b) in enumerate(spans, 1):
            counts[_norm(text[a:b])].append(n)
        for norm, positions in counts.items():
            if len(positions) >= REPEAT_MIN and (best is None or len(positions) > len(best["positions"])):
                a, b = spans[positions[0] - 1]
                best = {"text": _shorten(_entry_text(text[a:b])), "count": len(positions), "positions": positions,
                        "path": pointer(path), "kind": "entry", "total": len(spans)}
    if best is not None:
        return best
    seen = collections.defaultdict(list)
    for n, segment in enumerate(_SEGMENT.split(text), 1):
        segment = _KEY_PREFIX.sub("", " ".join(segment.split())).strip(' ",[]{}')
        if len(segment) >= 12:
            seen[segment.casefold()].append((n, segment))
    for hits in seen.values():
        if len(hits) >= REPEAT_MIN and (best is None or len(hits) > best["count"]):
            best = {"text": _shorten(hits[0][1]), "count": len(hits), "positions": [], "path": None,
                    "kind": "line", "total": None}
    return best


def _summary(text: str, scan: _Scan) -> tuple[str, list]:
    """One short line on the top-level keys produced, without the content of any list."""
    parts, names = [], []
    for key, kind, a, b in scan.top[:SUMMARY_KEYS]:
        names.append(key)
        if b is None:
            parts.append(f"{key} (cut off here)")
        elif kind == "list":
            spans = scan.items.get((key,), [])
            distinct = len({_norm(text[x:y]) for x, y in spans})
            parts.append(f"{key} (list of {len(spans)}, {distinct} different)")
        elif kind == "string":
            value = _entry_text(text[a:b])
            parts.append(f"{key} (text, {len(value)} chars)")
        elif kind == "object":
            parts.append(f"{key} (object)")
        else:
            parts.append(f"{key}: {_shorten(text[a:b], 40)}")
    if len(scan.top) > SUMMARY_KEYS:
        parts.append(f"... and {len(scan.top) - SUMMARY_KEYS} more")
    line = ", ".join(parts)
    return (line[:SUMMARY_CHARS].rstrip() + "...") if len(line) > SUMMARY_CHARS else line, names


def build_repair(error: str, bad: str) -> Repair:
    """The message for the next try: the error first, where the fault is, what was repeated, what was kept."""
    bad = bad or ""
    error = " ".join(str(error).split())
    if len(error) > ERROR_CHARS:
        error = error[:ERROR_CHARS].rstrip() + "..."
    scan = _Scan(bad).run()
    stop = scan.stop
    out = [f"Your previous answer was rejected: {error.rstrip('.')}."]
    result = Repair(message="", valid_json=stop is None)
    if stop is not None:
        path = list(scan.path)
        result.path, result.cut = pointer(path), stop.eof
        if path and isinstance(path[-1], int):
            place = f"entry {path[-1] + 1} of the list {pointer(path[:-1])}"
        else:
            place = pointer(path) if path else "the start of the answer"
        if scan.no_json:
            out.append("The answer was empty." if not bad.strip() else "The text contained no JSON object.")
        else:
            out.append(f"The text {'ended unfinished' if stop.eof else 'stopped being valid JSON'} at {place}.")
    repeat = find_repeat(bad, scan)
    result.repeat = repeat
    if repeat is not None and repeat["kind"] == "entry":
        out.append(f'The entry "{repeat["text"]}" was written {repeat["count"]} times in the list {repeat["path"]} '
                   f'(entries {_ranges(repeat["positions"])} of {repeat["total"]}).')
    elif repeat is not None:
        out.append(f'The text "{repeat["text"]}" was written {repeat["count"]} times.')
    if scan.top:
        line, result.keys = _summary(bad, scan)
        out.append(f"Already produced, at the top level: {line}.")
    ask = ("Answer again with only the corrected JSON object that matches the required JSON Schema: no Markdown, "
           "no code fences, no text outside the JSON.")
    if repeat is not None:
        ask += " Do not repeat anything: write each list entry and each sentence at most once, and keep the lists short."
    out.append(ask)
    result.message = " ".join(out)
    return result
