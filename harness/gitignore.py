"""A small .gitignore matcher for the tools that walk a folder (`search_code`, `tree`).

Each `.gitignore` applies below its own folder; a deeper file overrides a shallower one and the last matching rule of a file wins,
as in git. Supported: comments, `!` negation, a leading or inner `/` (anchored), a trailing `/` (folders only), `*`, `?`, `[...]`
and `**`. Not supported: `.git/info/exclude`, the global ignore file and `core.excludesFile`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_FLAGS = re.I if sys.platform == "win32" else 0
Rule = Tuple["re.Pattern[str]", bool, bool]        # (pattern on the path below the file's folder, negated, folders only)


def glob_regex(pattern: str, anchored: bool = True) -> str:
    """The regular expression text for one glob (`**` crosses folders, `*` and `?` do not). `anchored` ties it to the start of the path."""
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                j = i + 2
                if pattern[j:j + 1] == "/":                 # `**/` matches zero or more folders
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\[")
            else:
                body = pattern[i + 1:j]
                if body[:1] in "!^":
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return ("^" if anchored else "(?:^|/)") + "".join(out) + "$"


def parse_rules(text: str) -> List[Rule]:
    rules: List[Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        stripped = line.rstrip(" ") if not line.endswith("\\ ") else line
        if not stripped or stripped.startswith("#"):
            continue
        negated = stripped.startswith("!")
        if negated:
            stripped = stripped[1:]
        elif stripped.startswith("\\#") or stripped.startswith("\\!"):
            stripped = stripped[1:]
        folders_only = stripped.endswith("/")
        stripped = stripped.rstrip("/")
        if not stripped:
            continue
        anchored = "/" in stripped
        stripped = stripped.lstrip("/")
        try:
            rules.append((re.compile(glob_regex(stripped, anchored=anchored), _FLAGS), negated, folders_only))
        except re.error:
            continue
    return rules


class GitIgnore:
    """The ignore rules of a folder tree, read from its `.gitignore` files on first use."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._rules: Dict[Path, List[Rule]] = {}

    def _for_folder(self, folder: Path) -> List[Rule]:
        rules = self._rules.get(folder)
        if rules is None:
            try:
                rules = parse_rules((folder / ".gitignore").read_text(encoding="utf-8", errors="replace"))
            except OSError:
                rules = []
            self._rules[folder] = rules
        return rules

    def ignored(self, path: Path, is_dir: bool) -> bool:
        """Whether `path` (inside the root) is ignored by a rule in its own folder or any folder above it up to the root. A file inside an
        ignored folder is not tested here: a walk that skips ignored folders never reaches it."""
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return False
        parts = rel.parts
        verdict: Optional[bool] = None
        for depth in range(len(parts)):
            folder = self.root.joinpath(*parts[:depth])
            below = "/".join(parts[depth:])
            for pattern, negated, folders_only in self._for_folder(folder):
                if folders_only and not is_dir:
                    continue
                if pattern.search(below):
                    verdict = not negated
        return bool(verdict)
