"""The text side of `edit_file`: find the text an edit names and replace it, or say exactly why that is not possible.

Matching runs on LF text, in passes from strict to tolerant: exact, ignoring trailing whitespace on each line, ignoring each line's
indentation (the replacement is re-indented to the file's), and Unicode-normalised (NFKC, smart quotes, dashes, odd spaces). The
strictest pass that finds anything decides, and a pass other than the exact one is applied only when it finds exactly one place; the
result names the lines. A miss returns the closest text with its line numbers and a whitespace-visible comparison; several matches
return their line numbers. Edits are applied in order on the result of the one before, and either all apply or none does.
"""
from __future__ import annotations

import bisect
import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

EXACT, TRAILING, INDENT, UNICODE = "exact", "trailing-whitespace", "indentation", "unicode"
PASS_TEXT = {TRAILING: "ignoring trailing whitespace", INDENT: "ignoring indentation", UNICODE: "after Unicode normalisation"}
EDIT_KEYS = ("target", "replacement", "replace_all", "expected_count")
SIMILAR_MIN = 0.45                  # below this ratio no text counts as "close" to the target
MAX_WINDOW_LINES = 200
MAX_SHOWN_LINES = 40
LINE_CHARS = 200
TAB_COLUMNS = 4


class EditError(ValueError):
    """An edit that was not applied. The message says why and what to do; `closest` is the first line (1-based) of the nearest text, or 0."""

    def __init__(self, message: str, closest: int = 0, closest_end: int = 0) -> None:
        super().__init__(message)
        self.closest, self.closest_end = closest, closest_end


@dataclass
class EditOutcome:
    text: str                                              # the LF text after all the edits
    notes: List[str] = field(default_factory=list)         # one sentence per edit that needs saying (a tolerant match, a count, a no-op)
    applied: int = 0                                       # edits that changed something

    @property
    def changed(self) -> bool:
        return self.applied > 0


# -- line endings ---------------------------------------------------------------------------------------------------


def to_lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def restore_endings(original: str, old_lf: str, new_lf: str) -> str:
    """`new_lf` with the line endings the file had: all CRLF stays CRLF; a mix keeps each untouched line's own ending, and changed
    lines take the more common one."""
    crlf = original.count("\r\n")
    if not crlf:
        return new_lf
    total = original.count("\n")
    if crlf == total:
        return new_lf.replace("\n", "\r\n")
    old_lines, new_lines = old_lf.split("\n"), new_lf.split("\n")
    ends = [("\r\n" if ln.endswith("\r") else "\n") for ln in original.split("\n")[:-1]]
    common = "\r\n" if crlf * 2 >= total else "\n"
    out: List[str] = []
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        for k in range(b0, b1):
            last = k == len(new_lines) - 1
            end = "" if last else (ends[a0 + (k - b0)] if tag == "equal" and a0 + (k - b0) < len(ends) else common)
            out.append(new_lines[k] + end)
    return "".join(out)


# -- locating text --------------------------------------------------------------------------------------------------


def _starts(text: str) -> List[int]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def _line_of(starts: List[int], pos: int) -> int:
    return bisect.bisect_right(starts, pos)


@dataclass
class Match:
    start: int          # character offsets in the LF text
    end: int
    first: int          # 1-based lines
    last: int


_QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "‵": "'",
           "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "‶": '"'}
_DASHES = dict.fromkeys(map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0xFE58, 0xFE63, 0xFF0D)), "-")
_INVISIBLE = frozenset(map(chr, (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD)))


def _fold_char(ch: str) -> str:
    if ch in _QUOTES:
        return _QUOTES[ch]
    if ch in _DASHES:
        return "-"
    if ch in _INVISIBLE:
        return ""
    if ch != " " and unicodedata.category(ch) == "Zs":
        return " "
    return unicodedata.normalize("NFKC", ch)


def fold(text: str) -> str:
    return "".join(_fold_char(c) for c in text)


def _fold_mapped(text: str) -> Tuple[str, List[int]]:
    out: List[str] = []
    origin: List[int] = []
    for i, ch in enumerate(text):
        f = _fold_char(ch)
        out.append(f)
        origin.extend([i] * len(f))
    return "".join(out), origin


def _find_all(text: str, needle: str) -> List[int]:
    found: List[int] = []
    i = text.find(needle)
    while i != -1:
        found.append(i)
        i = text.find(needle, i + 1)
    return found


def _non_overlapping(matches: List[Match]) -> List[Match]:
    out: List[Match] = []
    for m in matches:
        if not out or m.start >= out[-1].end:
            out.append(m)
    return out


def _line_matches(text: str, target: str, key: Any) -> List[Match]:
    """Places where the lines of `target` equal consecutive lines of `text` once `key` is applied to each line."""
    lines = text.split("\n")
    wanted = target.split("\n")
    tail_newline = len(wanted) > 1 and wanted[-1] == ""
    if tail_newline:
        wanted.pop()
    if not wanted or all(not w.strip() for w in wanted):
        return []
    keys = [key(ln) for ln in lines]
    goal = [key(w) for w in wanted]
    starts = _starts(text)
    found: List[Match] = []
    for i in range(len(lines) - len(wanted) + 1):
        if keys[i] != goal[0] or keys[i:i + len(wanted)] != goal:
            continue
        j = i + len(wanted) - 1
        end = starts[j] + len(lines[j])
        if tail_newline and end < len(text):
            end += 1
        found.append(Match(starts[i], end, i + 1, j + 1))
    return found


def locate(text: str, target: str, exact_only: bool = False) -> Tuple[str, List[Match]]:
    """The strictest pass that finds `target` in `text`, and its matches (empty list, with the exact pass named, when none does)."""
    starts = _starts(text)
    exact = [Match(i, i + len(target), _line_of(starts, i), _line_of(starts, max(i, i + len(target) - 1))) for i in _find_all(text, target)]
    if exact or exact_only:
        return EXACT, exact
    found = _line_matches(text, target, str.rstrip)
    if found:
        return TRAILING, found
    found = _line_matches(text, target, str.strip)
    if found:
        return INDENT, found
    folded, origin = _fold_mapped(text)
    goal = fold(target)
    found = []
    if goal:
        for i in _find_all(folded, goal):
            j = i + len(goal)
            if (i and origin[i] == origin[i - 1]) or (j < len(origin) and origin[j] == origin[j - 1]):
                continue                                    # begins or ends inside the expansion of one character
            start, end = origin[i], origin[j - 1] + 1
            found.append(Match(start, end, _line_of(starts, start), _line_of(starts, max(start, end - 1))))
    if found:
        return UNICODE, found
    found = _line_matches(text, target, lambda s: fold(s).strip())
    return (UNICODE, found) if found else (EXACT, [])


# -- re-indenting a replacement -------------------------------------------------------------------------------------


def _columns(ws: str) -> int:
    return sum(TAB_COLUMNS if c == "\t" else 1 for c in ws)


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _render(columns: int, tabs: bool) -> str:
    return "\t" * (columns // TAB_COLUMNS) + " " * (columns % TAB_COLUMNS) if tabs else " " * columns


def reindent(replacement: str, sent: Sequence[str], real: Sequence[str]) -> str:
    """The replacement's lines moved to the indentation of the real lines the target matched (`sent` and `real` pair line for line).
    Relative indentation is kept and scaled when the target used a different step (2 spaces against 4); tabs and spaces follow the file."""
    pairs = [(s, r) for s, r in zip(sent, real) if s.strip()]
    if not pairs:
        return replacement
    base_sent, base_real = _indent(pairs[0][0]), _indent(pairs[0][1])
    scale = 1.0
    for s, r in pairs[1:]:
        ds, dr = _columns(_indent(s)) - _columns(base_sent), _columns(_indent(r)) - _columns(base_real)
        if ds > 0 and dr > 0:
            scale = dr / ds
            break
    if base_sent == base_real and scale == 1.0:
        return replacement
    tabs = "\t" in base_real or any(_indent(r).startswith("\t") for _, r in pairs)
    base_cols_sent, base_cols_real = _columns(base_sent), _columns(base_real)
    out: List[str] = []
    for line in replacement.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        delta = round((_columns(_indent(line)) - base_cols_sent) * scale)
        cols = max(0, base_cols_real + delta)
        out.append((base_real if delta == 0 else _render(cols, tabs)) + line.lstrip())
    return "\n".join(out)


# -- failure messages -----------------------------------------------------------------------------------------------


def visible(line: str) -> str:
    """A line with its whitespace made visible: a tab is →, a space ·, any other space character ⍽."""
    return "".join("→" if c == "\t" else "·" if c == " " else "⍽" if unicodedata.category(c) == "Zs" else c for c in line)


def _clip(line: str, limit: int = LINE_CHARS) -> str:
    return line if len(line) <= limit else line[:limit] + "..."


def numbered_lines(lines: Sequence[str], first: int, last: int) -> str:
    return "\n".join(f"{n:>5} | {_clip(lines[n - 1])}" for n in range(first, last + 1))


def closest_window(lines: Sequence[str], target: str) -> Tuple[float, int, int]:
    """(similarity 0..1, first line, last line; both 1-based) of the stretch of `lines` most like `target`; similarity 0 when nothing is close."""
    wanted = target.split("\n")
    if len(wanted) > 1 and wanted[-1] == "":
        wanted.pop()
    wanted = wanted[:MAX_WINDOW_LINES]
    size = len(wanted)
    if not lines or all(not w.strip() for w in wanted):
        return 0.0, 0, 0
    goal = "\n".join(wanted)
    last_start = max(0, len(lines) - size)
    starts: Sequence[int] = range(last_start + 1)
    if len(lines) > 20_000:                                 # on a huge file only windows that share a line with the target are compared
        shared = {w.strip() for w in wanted if len(w.strip()) >= 4}
        starts = [i for i in starts if any(lines[k].strip() in shared for k in range(i, min(i + size, len(lines))))]
    matcher = difflib.SequenceMatcher(None, "", goal, autojunk=False)
    best, at = 0.0, 0
    for i in starts:
        matcher.set_seq1("\n".join(lines[i:i + size]))
        if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
            continue
        ratio = matcher.ratio()
        if ratio > best:
            best, at = ratio, i
    if best < SIMILAR_MIN:
        return 0.0, 0, 0
    return best, at + 1, min(len(lines), at + size)


def whitespace_diff(sent: Sequence[str], real: Sequence[str]) -> str:
    rows = [ln for ln in difflib.unified_diff([visible(s) for s in sent], [visible(r) for r in real], "sent", "file", lineterm="", n=0)
            if not ln.startswith(("---", "+++", "@@"))]
    if len(rows) > MAX_SHOWN_LINES:
        rows = rows[:MAX_SHOWN_LINES] + ["..."]
    return "\n".join(_clip(r) for r in rows)


def excerpt(text: str, around: int = 0, until: int = 0, radius: int = 12, cap: int = 60) -> str:
    """Numbered lines of `text`: around the lines `around`..`until` when given, else from the top."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return "(the file is empty)"
    if around:
        first, last = max(1, around - radius), min(len(lines), (until or around) + radius)
    else:
        first, last = 1, min(len(lines), cap // 2 + 10)
    last = min(last, first + cap - 1)
    shown = numbered_lines(lines, first, last)
    more = f"\n(lines {first}-{last} of {len(lines)})" if (first, last) != (1, len(lines)) else ""
    return shown + more


def _where(edit_no: int, total: int) -> str:
    return f"Edit {edit_no} of {total}" if total > 1 else "The edit"


def _suggestions(items: Sequence[str]) -> str:
    return "Next:\n" + "\n".join(f" {i}. {t}" for i, t in enumerate(items, 1))


def _missing_message(text: str, target: str, edit_no: int, total: int, exact_only: bool) -> EditError:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    ratio, first, last = closest_window(lines, target)
    head = f"{_where(edit_no, total)}: the target text was not found in the file, so nothing was changed" + (" (no edit of this call was applied)." if total > 1 else ".")
    if exact_only:
        head += " replace_all and expected_count match exact text only."
    if not first:
        return EditError(head + " No text in the file is close to it.\n" + _suggestions([
            "Read the part of the file you mean again (read_file with offset and limit) and copy the lines exactly.",
            "Use a shorter target: one or two distinctive lines, not a whole block.",
            "If the text may be in another file, check the path.",
        ]))
    shown_first, shown_last = max(1, first - 3), min(len(lines), last + 3)
    if shown_last - shown_first + 1 > MAX_SHOWN_LINES:
        shown_last = shown_first + MAX_SHOWN_LINES - 1
    wanted = target.split("\n")
    if len(wanted) > 1 and wanted[-1] == "":
        wanted.pop()
    body = [head, f"The closest text is at lines {first}-{last} ({round(ratio * 100)}% similar). The file has:", numbered_lines(lines, shown_first, shown_last),
            "What you sent (-) against what the file has (+); → is a tab, · a space, ⍽ another space character:",
            whitespace_diff(wanted, lines[first - 1:last]) or "(the visible characters are the same: look for an invisible difference)"]
    body.append(_suggestions([
        f"Copy the lines {first}-{last} above exactly (without the line numbers) as the target.",
        "Use a shorter target: one or two distinctive lines.",
        f"Read the area again with read_file offset={max(1, first - 5)} before the next try.",
        "For a large change, rewrite the file with write_file after reading it whole.",
    ]))
    return EditError("\n".join(body), first, last)


def _ambiguous_message(matches: Sequence[Match], edit_no: int, total: int, pass_name: str, path: str) -> EditError:
    where = ", ".join(str(m.first) if m.first == m.last else f"{m.first}-{m.last}" for m in matches[:12]) + (", ..." if len(matches) > 12 else "")
    how = "" if pass_name == EXACT else f" ({PASS_TEXT[pass_name]})"
    return EditError(f"{_where(edit_no, total)}: the target appears {len(matches)} times{how} in {path or 'the file'}, at lines {where}. Nothing was changed.\n"
                     + _suggestions(["Add the line before or after the target so that only one place matches.",
                                     f"Or, to change every one of the {len(matches)}, send replace_all=true (or expected_count={len(matches)})."]),
                     matches[0].first, matches[0].last)


# -- applying -------------------------------------------------------------------------------------------------------


def _check_edit(raw: Any, edit_no: int, total: int) -> Tuple[str, str, bool, Optional[int]]:
    if not isinstance(raw, Mapping):
        raise EditError(f"{_where(edit_no, total)} must be an object with 'target' and 'replacement'.")
    unknown = [k for k in raw if k not in EDIT_KEYS]
    if unknown:
        raise EditError(f"{_where(edit_no, total)} has no field(s) {', '.join(map(str, unknown))}; the fields are {', '.join(EDIT_KEYS)}.")
    target, replacement = raw.get("target"), raw.get("replacement")
    if not isinstance(target, str) or not isinstance(replacement, str):
        raise EditError(f"{_where(edit_no, total)} needs 'target' and 'replacement' as text (use an empty replacement to delete the target).")
    target, replacement = to_lf(target), to_lf(replacement)
    if target == "":
        raise EditError(f"{_where(edit_no, total)}: the target text must not be empty, so nothing was changed. Do not re-send this call unchanged.\n"
                        + _suggestions(["To insert text, use the line before or after the place as the target and put it back in the replacement with the new text.",
                                        "To create a file or replace all of one, use write_file."]))
    if not target.strip():
        raise EditError(f"{_where(edit_no, total)}: the target is only whitespace, which matches many places. Do not re-send this call unchanged.\n"
                        + _suggestions(["Use a neighbouring line of code as the target and include the whitespace you want in the replacement."]))
    if target == replacement:
        raise EditError(f"{_where(edit_no, total)}: the target and the replacement are identical, so nothing would change. Do not re-send this call unchanged.\n"
                        + _suggestions(["Put the new text in the replacement.", "If the file is already as you want it, go on with the task."]))
    count = raw.get("expected_count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
        raise EditError(f"{_where(edit_no, total)}: expected_count must be a whole number of 1 or more.")
    return target, replacement, bool(raw.get("replace_all")), count


def _splice(text: str, matches: Sequence[Match], pieces: Sequence[str]) -> str:
    out: List[str] = []
    at = 0
    for m, piece in zip(matches, pieces):
        out.append(text[at:m.start])
        out.append(piece)
        at = m.end
    out.append(text[at:])
    return "".join(out)


def _lines_text(first: int, last: int) -> str:
    return f"line {first}" if first == last else f"lines {first}-{last}"


def apply_edits(text: str, edits: Sequence[Mapping[str, Any]], path: str = "") -> EditOutcome:
    """Applies `edits` in order to the LF text `text`. Raises EditError, with nothing applied, when any edit cannot be."""
    total = len(edits)
    if not total:
        raise EditError("There is no edit to apply: send 'target' and 'replacement', or a non-empty 'edits' list.")
    current = text
    outcome = EditOutcome(current)
    for number, raw in enumerate(edits, 1):
        target, replacement, every, expected = _check_edit(raw, number, total)
        exact_only = every or expected is not None
        pass_name, matches = locate(current, target, exact_only)
        if not matches:
            done = locate(current, replacement)[1] if replacement.strip() else []
            if done:
                first = done[0]
                outcome.notes.append(f"{_where(number, total)} was already applied (the replacement is at {_lines_text(first.first, first.last)}); nothing changed.")
                continue
            raise _missing_message(current, target, number, total, exact_only)
        if exact_only:
            matches = _non_overlapping(matches)
            if expected is not None and len(matches) != expected:
                raise EditError(f"{_where(number, total)}: the target appears {len(matches)} times, not the {expected} expected, at lines "
                                + ", ".join(str(m.first) for m in matches[:12]) + ". Nothing was changed.\n"
                                + _suggestions(["Check the count, or add surrounding lines so the target is the place you mean."]), matches[0].first, matches[0].last)
            pieces = [replacement] * len(matches)
            current = _splice(current, matches, pieces)
            outcome.applied += 1
            if len(matches) > 1:
                outcome.notes.append(f"{_where(number, total)} changed {len(matches)} places (lines " + ", ".join(str(m.first) for m in matches[:12]) + ").")
            continue
        if len(matches) > 1:
            raise _ambiguous_message(matches, number, total, pass_name, path)
        match = matches[0]
        piece = replacement
        if pass_name in (INDENT, UNICODE):
            sent, real = target.split("\n"), current[match.start:match.end].split("\n")
            for rows in (sent, real):
                if len(rows) > 1 and rows[-1] == "":
                    rows.pop()
            if len(sent) == len(real):
                piece = reindent(replacement, sent, real)
        current = _splice(current, [match], [piece])
        outcome.applied += 1
        if pass_name != EXACT:
            outcome.notes.append(f"{_where(number, total)} matched {_lines_text(match.first, match.last)} {PASS_TEXT[pass_name]}; the replacement was put there"
                                 + (" with the file's indentation." if pass_name == INDENT else "."))
    outcome.text = current
    return outcome


# -- a block found by indentation -----------------------------------------------------------------------------------

_LEAD_MARKS = ("@", "#", "//", "/*", "*", "--")
_CLOSERS = (")", "]", "}")


def indent_block(lines: Sequence[str], index: int, max_levels: int = 1, siblings: bool = False) -> Tuple[int, int]:
    """(first, last) 0-based line indexes of the block around `lines[index]`: the block the line opens, or else the one it is in, and
    `max_levels - 1` blocks above that. A comment or decorator right above the block's first line, and a closing bracket line at its end,
    belong to it. With `siblings`, the blocks next to the outermost one are included."""
    count = len(lines)
    depth: List[Optional[int]] = [None if not ln.strip() else _columns(_indent(ln)) for ln in lines]
    i = index
    while i < count and depth[i] is None:                   # a blank line stands for the next line with text
        i += 1
    if i >= count:
        i = index
        while i > 0 and depth[i] is None:
            i -= 1
    nxt = i + 1
    while nxt < count and depth[nxt] is None:
        nxt += 1
    opens = nxt < count and depth[i] is not None and (depth[nxt] or 0) > (depth[i] or 0)
    head = i
    for _ in range(max_levels - 1 if opens else max_levels):
        j = head - 1
        while j >= 0 and (depth[j] is None or (depth[j] or 0) >= (depth[head] or 0)):
            j -= 1
        if j < 0:
            break
        head = j
    base = depth[head] or 0
    end = head
    k = head + 1
    while k < count and (depth[k] is None or (depth[k] or 0) > base):
        if depth[k] is not None:
            end = k
        k += 1
    if k < count and depth[k] == base and lines[k].lstrip().startswith(_CLOSERS):
        end = k
    end = max(end, i)
    start = head
    while start > 0 and depth[start - 1] == base and lines[start - 1].lstrip().startswith(_LEAD_MARKS):
        start -= 1
    if siblings:
        while start > 0 and (depth[start - 1] is None or (depth[start - 1] or 0) >= base):
            start -= 1
        while end + 1 < count and (depth[end + 1] is None or (depth[end + 1] or 0) >= base):
            end += 1
        while start < end and depth[start] is None:
            start += 1
        while end > start and depth[end] is None:
            end -= 1
    return start, end
