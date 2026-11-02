"""Repair of a tool call the model wrote slightly wrong: the JSON of its arguments, the types of its values and the name of the tool.

Each repair is a rule that only fires when the result is not in doubt. Text that cannot be recovered is left as it is, so the
model is told what is wrong and writes the call again; nothing here guesses a missing value.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

NAME_CUTOFF = 0.7           # how alike two tool names must be (difflib ratio) for one to stand for the other
MAX_TYPOS = 2               # and at most this many characters apart: a spelling slip, not another word (`read_file` is not `write_file`)
NAME_MARGIN = 0.1           # and how far it must be ahead of the next best
SUGGEST_CUTOFF = 0.6        # and how alike for an unknown name to list the other as a near match
SUGGESTIONS = 3


# -- JSON ----------------------------------------------------------------------------------------------------

_CLOSER = {"{": "}", "[": "]"}


def _escape_controls(text: str) -> str:
    """A raw control character inside a string (a real line break, a tab) written as its escape."""
    out: List[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            elif ord(ch) < 0x20:
                out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, f"\\u{ord(ch):04x}"))
                continue
        elif ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def _drop_trailing_commas(text: str) -> str:
    """A comma that is followed only by whitespace and a closing bracket (or the end of the text), outside strings, removed."""
    out: List[str] = []
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "," and re.match(r"\s*(?:[}\]]|\Z)", text[i + 1:]):
            continue
        out.append(ch)
    return "".join(out)


def _close_brackets(text: str) -> Optional[str]:
    """The text with the brackets that were left open closed in the order they were opened. None when it ends inside a string or after a
    key's colon: what should follow is not known."""
    stack: List[str] = []
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in _CLOSER:
            stack.append(_CLOSER[ch])
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if in_string or re.search(r":\s*\Z", text):
        return None
    return text + "".join(reversed(stack)) if stack else text


def _trim_extra_closers(text: str) -> str:
    """Closing brackets after the end of the value, removed."""
    try:
        _, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return text
    return text[:end] if re.fullmatch(r"[\s}\]]*", text[end:]) else text


def repair_json(text: str) -> Optional[Any]:
    """The value `text` was meant to be, by passes (raw control characters in strings, trailing commas, brackets left open, extra
    closing brackets); None when no pass makes it valid JSON."""
    text = text.strip()
    for stage in (_escape_controls, _drop_trailing_commas, _close_brackets, _trim_extra_closers):
        fixed = stage(text)
        if fixed is None:
            return None
        text = fixed
        try:
            return json.loads(text)
        except ValueError:
            continue
    return None


# -- values -------------------------------------------------------------------------------------------------

_INT = re.compile(r"-?(?:0|[1-9]\d*)")
_FLOAT = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _decoded(text: str, kind: type) -> Any:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, kind) else None


def _is_type(kind: str, value: Any) -> bool:
    """Whether `value` has the JSON type `kind` (a boolean is not a number)."""
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, {"string": str, "boolean": bool, "array": list, "object": dict}.get(kind, object))


def coerce_value(spec: Dict[str, Any], value: Any) -> Tuple[Any, bool]:
    """(value, changed): `value` made the type the schema `spec` asks for when that is not in doubt, else the value as it was.
    A property with an enum is never changed, and a string that is a number with a leading zero ("00123") stays a string."""
    want = spec.get("type")
    if "enum" in spec or want is None or isinstance(want, list):
        return value, False
    if want == "integer":
        if isinstance(value, str) and _INT.fullmatch(value.strip()):
            return int(value.strip()), True
        if isinstance(value, float) and not isinstance(value, bool) and value.is_integer():
            return int(value), True
    elif want == "number":
        if isinstance(value, str) and _FLOAT.fullmatch(value.strip()):
            number = float(value.strip())
            return (int(number) if _INT.fullmatch(value.strip()) else number), True
    elif want == "boolean":
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true", True
    elif want == "array":
        item = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        changed = False
        if isinstance(value, str):
            listed = _decoded(value.strip(), list)
            if listed is None:
                if item.get("type") in (None, "string"):
                    return [value], True
                one, did = coerce_value(item, value)
                return ([one], True) if did else (value, False)
            value, changed = listed, True
        elif value is None:
            return value, False
        elif not isinstance(value, list):
            fixed, _ = coerce_value(item, value) if item else (value, False)
            if item.get("type") and not _is_type(item["type"], fixed):
                return value, False
            value, changed = [value], True
        if item:
            fixed = [coerce_value(item, v) for v in value]
            if any(c for _, c in fixed):
                return [v for v, _ in fixed], True
        return value, changed
    elif want == "object":
        if isinstance(value, str):
            obj = _decoded(value.strip(), dict)
            if obj is not None:
                return obj, True
    return value, False


def coerce_arguments(schema: Dict[str, Any], arguments: Any) -> Tuple[Any, bool]:
    """(arguments, changed): each value of an arguments object made the type its property asks for (see `coerce_value`)."""
    if not isinstance(arguments, dict):
        return arguments, False
    props = schema.get("properties") or {}
    out, changed = dict(arguments), False
    for key, value in arguments.items():
        spec = props.get(key)
        if isinstance(spec, dict):
            out[key], did = coerce_value(spec, value)
            changed = changed or did
    return (out if changed else arguments), changed


# -- names --------------------------------------------------------------------------------------------------

_JUNK = re.compile(r"""["'<>(){}\[\]`].*\Z""", re.DOTALL)
_NAMESPACE = re.compile(r"\A(?:functions?|tools?|default_api|api)[.:/]", re.IGNORECASE)
_SUFFIX = re.compile(r"[_\s-]?tool\Z", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s_.\-:/]+")


def _flat(name: str) -> str:
    """A name without case or separators: `Read-File`, `readFile` and `read_file` are the same."""
    return _SEPARATORS.sub("", name).lower()


def _camel_flat(name: str) -> str:
    return _flat(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name))


def _distance(a: str, b: str) -> int:
    """The number of single-character insertions, deletions and replacements that turn `a` into `b`."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def resolve_tool_name(name: str, names: Sequence[str]) -> Tuple[Optional[str], List[str]]:
    """(the tool the name stands for, near matches). A name the model got slightly wrong is resolved when exactly one tool fits: case,
    separators, CamelCase, a `_tool` suffix, a namespace prefix (`functions.read_file`), text after a quote or bracket, or a spelling at
    least 0.7 alike and at most two characters apart. Otherwise None and up to three tools with a similar name."""
    if name in names:
        return name, []
    candidates: List[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    cleaned = _JUNK.sub("", name.strip().lstrip("\"'<`")).strip()
    add(cleaned)
    add(_NAMESPACE.sub("", cleaned))
    for c in list(candidates):
        add(_SUFFIX.sub("", c))
    flats: Dict[str, List[str]] = {}
    for tool in names:
        flats.setdefault(_flat(tool), []).append(tool)
    for c in candidates:
        if c in names:
            return c, []
        for key in (_flat(c), _camel_flat(c)):
            if len(flats.get(key, [])) == 1:
                return flats[key][0], []
    lowered = {t.lower(): t for t in names}
    for c in candidates:
        ranked = sorted(((difflib.SequenceMatcher(None, c.lower(), k).ratio(), k) for k in lowered), reverse=True)
        if (ranked and ranked[0][0] >= NAME_CUTOFF and (len(ranked) == 1 or ranked[1][0] < ranked[0][0] - NAME_MARGIN)
                and _distance(c.lower(), ranked[0][1]) <= MAX_TYPOS):
            return lowered[ranked[0][1]], []
    pool = candidates or [name.lower()]
    near: List[str] = []
    for c in pool:
        for tool in sorted(names):                                                 # a name that holds the other, or is held by it
            if min(len(_flat(c)), len(_flat(tool))) >= 4 and (_flat(c) in _flat(tool) or _flat(tool) in _flat(c)) and tool not in near:
                near.append(tool)
        for m in difflib.get_close_matches(c.lower(), list(lowered), n=SUGGESTIONS, cutoff=SUGGEST_CUTOFF):
            if lowered[m] not in near:
                near.append(lowered[m])
    return None, near[:SUGGESTIONS]
