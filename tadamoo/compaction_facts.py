"""The Facts section of a compaction record: per-item values written as `id: value` lines.

The program keeps the lines, not the model. The lines of the previous record are copied unchanged into the next one, the lines the agent
wrote itself (a ledger entry in an edit or a write) are read from its calls, and the summary call may only add ids that are not there yet.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Tuple

SECTIONS = ("Facts", "Goal", "Decisions", "Files touched", "Commands run", "Open items")
_HEADING = re.compile(r"^\W*(" + "|".join(re.escape(s) for s in SECTIONS) + r")\b\W*(.*)$", re.IGNORECASE)
# a line the agent wrote as "id: value": the id has a digit, underscore, dot or slash (so prose labels are not taken), the value is plain text
_ENTRY = re.compile(r"^[ \t]*(?:[-*][ \t]+)?([A-Za-z0-9_./\\-]{2,48}):[ \t]+([^\s{\[\"'(#/<].{0,79}?)[ \t]*$")
_ID_MARK = re.compile(r"[0-9_./\\]")
LEFT_OUT = "({n} older fact lines left out; they are in the originals)"
_LEFT_OUT = re.compile(r"^\(\d+ older fact lines left out")


def key_of(line: str) -> str:
    """The id of a fact line: the text before its first colon, else the whole line."""
    head, sep, _ = line.partition(":")
    return (head if sep else line).strip()


def split(text: str) -> Tuple[List[str], str]:
    """(the lines of the Facts section, the text without that section). The section runs from its heading to the next heading."""
    facts: List[str] = []
    rest: List[str] = []
    inside = False
    for line in text.splitlines():
        found = _HEADING.match(line)
        if found:
            inside = found.group(1).lower() == "facts"
            if inside:
                if found.group(2).strip():
                    facts.append(found.group(2).strip())
                continue
        if inside:
            if line.strip() and not _LEFT_OUT.match(line.strip()):
                facts.append(line.strip().lstrip("-* ").strip())
        else:
            rest.append(line)
    return facts, "\n".join(rest).strip()


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def from_calls(arguments: Iterable[Dict[str, Any]]) -> List[str]:
    """The `id: value` lines in the text arguments of the given calls (the agent's own writes), in the order written; a later line for an id wins."""
    last: Dict[str, str] = {}
    for args in arguments:
        for text in _strings({k: v for k, v in args.items() if k not in ("path", "file_path", "command")}):
            for line in text.splitlines():
                found = _ENTRY.match(line)
                if found and _ID_MARK.search(found.group(1)):
                    ident = found.group(1)
                    last.pop(ident, None)
                    last[ident] = f"{ident}: {found.group(2)}"
    return list(last.values())


def merge(previous: List[str], written: List[str], proposed: List[str]) -> List[str]:
    """The previous lines in their place and unchanged; an id the agent wrote again takes its newest value; an id only the summary call
    named is added, and the call never changes a line that exists."""
    out: Dict[str, str] = {}
    for line in previous:
        out.setdefault(key_of(line), line)
    for line in written:
        out[key_of(line)] = line
    for line in proposed:
        out.setdefault(key_of(line), line)
    return list(out.values())


def render(lines: List[str], max_chars: int) -> str:
    """The section as text, within `max_chars`: when it is too long the oldest lines are left out and counted."""
    if not lines:
        return ""
    kept = list(lines)
    size = sum(len(x) + 1 for x in kept)
    dropped = 0
    while kept and size > max_chars:
        size -= len(kept.pop(0)) + 1
        dropped += 1
    body = ([LEFT_OUT.format(n=dropped)] if dropped else []) + kept
    return "Facts:\n" + "\n".join(body)


def with_facts(facts_text: str, rest: str) -> str:
    return facts_text + ("\n\n" if facts_text and rest else "") + rest
