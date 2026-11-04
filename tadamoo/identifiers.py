"""Identifiers in text (file paths, URLs, function names, ids, numbers) and the audit of a summary against them.

After a stretch of the conversation is replaced by a summary, the messages that stay and the open items may still point at things
only the removed text defined. `audit` lists the identifiers shared by the removed text and what is kept that the summary does not
carry, and says for each how to get it back.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Sequence, Set, Tuple

KINDS = ("url", "path", "id", "function", "number")
MAX_REREAD = 12                    # entries in the re-read list; the rest are only counted
MIN_NUMBER_DIGITS = 3              # shorter numbers (a line, a count of two) are too common to be named

_URL = re.compile(r"https?://[^\s<>\"'`)\]}]+")
_EXT = r"(?:py|js|jsx|ts|tsx|json|jsonl|md|txt|c|cc|cpp|cu|cuh|h|hpp|rs|go|java|kt|toml|yaml|yml|html|css|sh|ps1|cfg|ini|lock|log|csv|sql|xml|dart|cmake)"
_PATH = re.compile(rf"(?<![\w/\\.-])(?:[A-Za-z]:[\\/]|\.{{1,2}}[\\/]|~[\\/]|/)?(?:[\w.-]+[\\/])+[\w.-]+|(?<![\w/\\.-])[\w-]+\.{_EXT}\b")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b")
_CALL_ID = re.compile(r"\bcall_[A-Za-z0-9]{4,}\b")
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b|(?<![\w&])#\d{1,7}\b")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]{2,60})\(")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")
_TRAIL = ".,;:!?'\"`"
# the harness's own markup (data-block tags, the note of a masked result), which names no one's work
_MARKUP = re.compile(r"</?untrusted_data\b[^>\n]*>|\[result of [^\]\n]*\]")

# words that are code-shaped but name nothing in particular
_COMMON = frozenset({"print(", "len(", "str(", "int(", "dict(", "list(", "set(", "range(", "open(", "type(", "isinstance(", "if", "for", "while", "return"})


def norm(ident: str) -> str:
    """The form compared: a path with forward slashes."""
    return ident.replace("\\", "/")


def _trim(text: str) -> str:
    return text.rstrip(_TRAIL)


def identifiers(text: str) -> Dict[str, Set[str]]:
    """The identifiers in `text`, by kind. A piece of text is counted once, as the first kind that claims it (a URL is no path)."""
    found: Dict[str, Set[str]] = {k: set() for k in KINDS}
    rest = _MARKUP.sub(" ", text or "")
    for m in _URL.finditer(rest):
        found["url"].add(_trim(m.group(0)))
    rest = _URL.sub(" ", rest)
    for m in _PATH.finditer(rest):
        item = _trim(m.group(0))
        if len(item) >= 4 and not item.replace("/", "").replace("\\", "").replace(".", "").isdigit():
            found["path"].add(norm(item))
    rest = _PATH.sub(" ", rest)
    for pattern in (_UUID, _CALL_ID, _TICKET, _HEX):
        for m in pattern.finditer(rest):
            found["id"].add(m.group(0))
        rest = pattern.sub(" ", rest)
    for m in _CALL.finditer(rest):
        name = m.group(1).strip(".")
        if len(name) >= 3 and name + "(" not in _COMMON and name not in _COMMON:
            found["function"].add(name)
    for pattern in (_SNAKE, _CAMEL):
        for m in pattern.finditer(rest):
            if len(m.group(0)) >= 5:
                found["function"].add(m.group(0))
    for m in _NUMBER.finditer(rest):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= MIN_NUMBER_DIGITS:
            found["number"].add(m.group(0).rstrip(","))
    return found


def flat(found: Dict[str, Set[str]]) -> List[Tuple[str, str]]:
    """(kind, identifier) pairs in a fixed order: the kinds that are easiest to lose first."""
    return [(k, v) for k in KINDS for v in sorted(found[k])]


def _present(ident: str, kind: str, haystack: str, normalised: str) -> bool:
    return (norm(ident) in normalised) if kind == "path" else (ident in haystack)


def audit(kept: Iterable[str], removed: Iterable[str], summary: str, ignore: Iterable[str] = ()) -> Dict[str, List[Tuple[str, str]]]:
    """Identifiers the summary should carry. `kept` is the text that stays (the messages after the summary, the request), `removed` the text
    the summary replaces. Returns {"missing": [...], "carried": n} where "missing" are the (kind, identifier) pairs in both the kept and the
    removed text and not in the summary. Names in `ignore` (the tools' own) are not identifiers of the work."""
    kept_ids = identifiers("\n".join(kept))
    gone_ids = identifiers("\n".join(removed))
    normalised = norm(summary)
    skip = set(ignore)
    missing: List[Tuple[str, str]] = []
    carried = 0
    for kind in KINDS:
        for ident in sorted((kept_ids[kind] & gone_ids[kind]) - skip):
            if _present(ident, kind, summary, normalised):
                carried += 1
            else:
                missing.append((kind, ident))
    return {"missing": missing, "carried": carried}


def ungrounded(summary_open: str, sources: Sequence[str]) -> List[Tuple[str, str]]:
    """Paths, URLs and ids named in the summary's open items that no source text names: a likely invention."""
    named = identifiers(summary_open)
    have = "\n".join(sources)
    normalised = norm(have)
    out: List[Tuple[str, str]] = []
    for kind in ("url", "path", "id"):
        out += [(kind, i) for i in sorted(named[kind]) if not _present(i, kind, have, normalised)]
    return out
