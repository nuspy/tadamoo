"""Where code is in a conversation: fenced blocks, source files and results that are source.

One detector for both uses. An agent session (file and shell tools) keeps code whole in its context; a Chat session replaces old code with a
description. Each rule is deterministic: a fence, the file extension of the tool's path, or how much of a text looks like source.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

CODE_EXTENSIONS = frozenset((
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cu", ".cuh", ".cs", ".java", ".kt", ".kts",
    ".go", ".rs", ".rb", ".php", ".swift", ".scala", ".dart", ".lua", ".r", ".pl", ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".cmake", ".gradle", ".m", ".mm", ".hs", ".ex", ".exs", ".clj", ".jl"))
CODE_TOOLS = ("search_code", "find_references", "go_to_definition")         # results made of source lines, `path:line: text`
READ_TOOLS = ("read_file", "read_attachment")
WRITE_TOOLS = re.compile(r"write|edit|patch|replace|append|create|save", re.IGNORECASE)
_PATH_KEYS = ("path", "file_path", "filepath", "file", "filename", "target_file")

MIN_CODE_CHARS = 60                  # a shorter block is a command or a name, not code worth keeping
MIN_SOURCE_LINES = 4                 # fewer lines are not judged by how they look
SOURCE_SHARE = 0.45                  # share of the lines that must look like source
SCAN_LINES = 400                     # lines looked at when judging a text by its look

_FENCE = re.compile(r"(?ms)^[ \t]*(```|~~~)[ \t]*([\w+#.-]*)[^\n]*\n(.*?)(?:^[ \t]*\1[ \t]*$|\Z)")
_HIT = re.compile(r"(?m)^(\S[^\n:]*?\.\w{1,8}):\d+[:-]")
_LOOKS = re.compile(
    r"^\s*(?:def |class |import |from \S+ import |async def |function[ (]|const |let |var |export |return\b|if\b.*[:{]\s*$|elif |else\s*[:{]|for\b.*[:{]\s*$|while\b.*[:{]\s*$|"
    r"try\s*[:{]|except\b|catch\b|#include|#define|using |namespace |public |private |protected |static |fn |impl |struct |enum |interface |package |func |"
    r"@\w+|\}\s*$|\{\s*$|.*(?:=>|;\s*$|\)\s*\{\s*$)|[\w.\[\]]+\s*(?:=|\+=|-=)\s*\S)")
_COMMENT = re.compile(r"^\s*(?:#(?!include|define|!)|//|/\*|\*|--|;)\s*(.+)$")
_FUNCTION = re.compile(r"^\s*(?:async\s+)?(?:def|function|fn|func)\s+(\w+)|^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(?[\w, ]*\)?\s*=>|"
                       r"^\s*(?:public|private|protected|static)[\w<>\[\] ]*\s+(\w+)\s*\(")
_CLASS = re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?(?:class|struct|interface|enum|trait|impl)\s+(\w+)")
_LANGUAGES = (("python", re.compile(r"^\s*(?:def |class |import \w|from \w+ import )", re.M)), ("javascript", re.compile(r"\b(?:function\s*\w*\(|const \w+ =|=>|console\.)")),
              ("C or C++", re.compile(r"#include|\bstd::|\bint main\b")), ("shell", re.compile(r"^\s*(?:#!/bin/(?:ba)?sh|echo |fi$|esac$)", re.M)),
              ("SQL", re.compile(r"(?i)\bselect\b.+\bfrom\b|\bcreate table\b")), ("HTML", re.compile(r"(?i)<(?:html|div|body|script)\b")),
              ("Rust", re.compile(r"\bfn \w+\(|\blet mut\b")), ("Go", re.compile(r"^\s*func \w+|^package \w+", re.M)), ("Java", re.compile(r"\bpublic (?:static )?(?:class|void)\b")))


def has_code_extension(path: str) -> bool:
    """Whether a file name ends in a source extension."""
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return "." in name and "." + name.rsplit(".", 1)[-1] in CODE_EXTENSIONS or name in ("makefile", "cmakelists.txt", "dockerfile")


def fences(text: str) -> List[Tuple[int, int, str, str]]:
    """The fenced blocks of a text: (start, end, language tag, body), an unclosed one running to the end; blocks too short to be code are left out."""
    return [(m.start(), m.end(), m.group(2), m.group(3)) for m in _FENCE.finditer(text or "") if len(m.group(3).strip()) >= MIN_CODE_CHARS]


def without_code(text: str, mark: str = "[code left out here]") -> str:
    """The text with each fenced block replaced by `mark`."""
    out, last = [], 0
    for start, end, _, _ in fences(text):
        out.append(text[last:start])
        out.append(mark)
        last = end
    out.append(text[last:])
    return "".join(out)


def looks_like_source(text: str) -> bool:
    """Whether a text is mostly source lines (a file, a function, a listing), judged by the lines themselves."""
    lines = [ln for ln in (text or "").splitlines()[:SCAN_LINES] if ln.strip()]
    if len(lines) < MIN_SOURCE_LINES or len(text or "") < MIN_CODE_CHARS:
        return False
    return sum(1 for ln in lines if _LOOKS.match(ln)) / len(lines) >= SOURCE_SHARE


def has_code(text: str) -> bool:
    """A fenced block, or a text that is source from top to bottom (pasted without fences)."""
    return bool(fences(text)) or looks_like_source(text)


def _args(call: Dict[str, Any]) -> Dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def path_of(args: Dict[str, Any]) -> str:
    return next((str(args[k]) for k in _PATH_KEYS if isinstance(args.get(k), str) and args[k].strip()), "")


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def written_code(call: Dict[str, Any]) -> Optional[str]:
    """What a write or edit call puts in a file, when that file is source (or the text itself reads as source); else None."""
    name = (call.get("function") or {}).get("name") or ""
    if not WRITE_TOOLS.search(name):
        return None
    args = _args(call)
    text = "\n".join(s for k, v in args.items() if k not in _PATH_KEYS for s in _strings(v))
    if not text.strip():
        return None
    return text if has_code_extension(path_of(args)) or has_code(text) else None


def is_code_result(name: str, args: Dict[str, Any], content: str) -> bool:
    """Whether the result of a tool call is source: a file read by a source path, search hits in source files, or output that reads as source."""
    text = content or ""
    if len(text.strip()) < 20:
        return False
    if name in READ_TOOLS:
        return has_code_extension(path_of(args)) or has_code(text)
    if name in CODE_TOOLS:
        return any(has_code_extension(m.group(1)) for m in _HIT.finditer(text))
    return has_code(text)


def is_code_message(m: Dict[str, Any], calls: Dict[str, Dict[str, Any]]) -> bool:
    """Whether a message of a conversation holds code that an agent session keeps whole: a user message with a fenced block or pasted source,
    an assistant message with a fenced block or a write call of source, a tool result that is source. `calls` maps a call id to its call."""
    role, content = m.get("role"), m.get("content")
    text = content if isinstance(content, str) else "\n".join(str(p.get("text", "")) for p in content or [] if isinstance(p, dict))
    if role == "tool":
        call = calls.get(m.get("tool_call_id"))
        name = (call.get("function") or {}).get("name") if call else str(m.get("name") or "")
        return is_code_result(str(name or ""), _args(call) if call else {}, text)
    if role == "assistant":
        return bool(fences(text)) or any(written_code(c) for c in m.get("tool_calls") or [])
    if role == "user" and not m.get("note") and not m.get("summary"):
        return has_code(text)
    return False


def guess_language(code: str, tag: str = "") -> str:
    """A name for the language of a block, from its fence tag or its look; "" when there is no telling."""
    if tag.strip():
        return tag.strip().lower()
    for name, pattern in _LANGUAGES:
        if pattern.search(code or ""):
            return name
    return ""


def outline(code: str) -> Dict[str, Any]:
    """What can be read from a piece of code without a model: its line count, the names it defines and its first comment."""
    functions: List[str] = []
    classes: List[str] = []
    first_comment = ""
    for line in (code or "").splitlines():
        found = _FUNCTION.match(line)
        if found:
            functions.append(next(g for g in found.groups() if g))
        found = _CLASS.match(line)
        if found:
            classes.append(found.group(1))
        if not first_comment:
            c = _COMMENT.match(line)
            if c and not c.group(1).startswith(("!", "-*-")):
                first_comment = c.group(1).strip()[:120]
    seen: set = set()
    return {"lines": len((code or "").strip().splitlines()), "functions": [f for f in functions if not (f in seen or seen.add(f))][:8],
            "classes": [c for c in classes if not (c in seen or seen.add(c))][:6], "comment": first_comment}
