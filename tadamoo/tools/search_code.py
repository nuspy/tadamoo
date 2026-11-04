"""`search_code`: a regular-expression search over the text files of the workspace, in Python, so it works without ripgrep or grep.

It skips what .gitignore lists, binary files and files over 1 MB. Each hit is `path:line: text`; context lines are `path-line- text`.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from tadamoo.gitignore import glob_regex
from tadamoo.tools.registry import EffectClass
from tadamoo.tools.walk import MAX_FILE_BYTES, MAX_FILES, Walker, is_binary

MAX_HITS = 60
MAX_CONTEXT = 10
LINE_CHARS = 240
MAX_OUTPUT_CHARS = 24_000
TIME_LIMIT_S = 30.0


def _globs(spec: str) -> List["re.Pattern[str]"]:
    flags = re.I if sys.platform == "win32" else 0
    out = []
    for part in re.split(r"[,\s]+", spec.strip()):
        if part:
            out.append(re.compile(glob_regex(part[2:] if part.startswith("./") else part, anchored="/" in part), flags))
    return out


def _clip(line: str) -> str:
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + " ..."


def search(root: Path, resolve: Callable[[Path], Path], pattern: str, start: Path, glob: str = "", context: int = 0,
           max_results: int = MAX_HITS, now: Callable[[], float] = time.monotonic) -> str:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("Give the pattern to search for (a Python regular expression).")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"The pattern is not a valid regular expression ({exc}). It uses Python syntax: escape ( ) [ ] {{ }} . * + ? | ^ $ with a backslash to match them literally.")
    context = max(0, min(int(context), MAX_CONTEXT))
    max_results = max(1, min(int(max_results), MAX_HITS))
    filters = _globs(glob) if glob else []
    walker = Walker(root, resolve)
    if start.is_file():
        files, stopped = [start], False
        base = start.parent
    else:
        files, stopped = walker.files_below(start, MAX_FILES)
        base = start
    out: List[str] = []
    hits = matched_files = searched = skipped = 0
    size = 0
    deadline = now() + TIME_LIMIT_S
    capped = timed_out = False
    for path in files:
        if filters:
            rel = path.relative_to(base).as_posix()
            if not any(f.search(rel) for f in filters):
                continue
        if now() > deadline:
            timed_out = True
            break
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if is_binary(data):
            skipped += 1
            continue
        searched += 1
        lines = data.decode("utf-8", errors="replace").split("\n")
        found = []
        for number, line in enumerate(lines, 1):
            if regex.search(line.rstrip("\r")):
                found.append(number)
                if hits + len(found) > max_results:         # one hit more than wanted says there are more
                    found.pop()
                    capped = True
                    break
        if not found:
            continue
        matched_files += 1
        hits += len(found)
        name = path.relative_to(root).as_posix()
        wanted = set(found)
        shown = sorted({n for hit in found for n in range(max(1, hit - context), min(len(lines), hit + context) + 1)})
        previous = 0
        block: List[str] = []
        for n in shown:
            if context and previous and n != previous + 1:
                block.append("--")
            text = _clip(lines[n - 1].rstrip("\r"))
            block.append(f"{name}:{n}: {text}" if n in wanted else f"{name}-{n}- {text}")
            previous = n
        piece = "\n".join(block)
        size += len(piece) + 1
        out.append(piece)
        if size > MAX_OUTPUT_CHARS:
            capped = True
        if capped:
            break
    notes = [f"{hits} hit{'s' if hits != 1 else ''} in {matched_files} file{'s' if matched_files != 1 else ''}; {searched} files searched"]
    if skipped:
        notes.append(f"{skipped} binary or large files skipped")
    if capped:
        notes.append(f"stopped at {hits} hits: refine the search with a more specific pattern, a path or a glob")
    if stopped:
        notes.append(f"only the first {MAX_FILES} files were looked at: give a narrower path")
    if timed_out:
        notes.append(f"stopped after {TIME_LIMIT_S:g} seconds: give a narrower path or glob")
    if not hits:
        return f"No match for {pattern!r}. [" + "; ".join(notes) + "]"
    text = "\n".join(out)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS].rsplit("\n", 1)[0]
    return text + "\n[" + "; ".join(notes) + "]"


def register_search_code(registry: Any, resolve_safe: Callable[[str], Path], root: Path) -> None:
    """Adds `search_code`; `resolve_safe` turns a path the model gave into one inside the workspace."""

    def search_code(pattern: str, path: str = ".", glob: str = "", context: int = 0, max_results: int = MAX_HITS) -> str:
        start = resolve_safe(path or ".")
        if not start.exists():
            raise FileNotFoundError(f"Path not found: {path}. The working folder is {root}; paths are relative to it.")
        return search(root, resolve_safe, pattern, start, glob, context, max_results)

    registry.register(
        name="search_code",
        description="Searches the text files of the workspace for a regular expression (Python syntax, case-sensitive: start it with (?i) to ignore case). "
                    "Skips files that .gitignore lists, binary files and files over 1 MB. Each hit is path:line: text. At most 60 hits.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression matched against each line"},
                "path": {"type": "string", "description": "Folder or file to search, relative to the workspace (default: all of it)"},
                "glob": {"type": "string", "description": "Only files whose name or path matches, e.g. *.py or src/**/*.ts; several separated by commas"},
                "context": {"type": "integer", "description": "Lines to show before and after each hit (0 to 10, default 0)"},
                "max_results": {"type": "integer", "description": "Most hits to return (1 to 60, default 60)"},
            },
            "required": ["pattern"],
        },
        effect=EffectClass.PURE,
        handler=search_code,
    )
