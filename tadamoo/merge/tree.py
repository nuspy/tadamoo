"""Three-way merge of a folder: for each file, what the person changed (the installed copy), what the new version changed, against the original both started from.

Every file ends in one outcome:
  same       the installed copy already matches the new version
  upstream   only the new version changed it (added, changed or removed): the new one is taken
  kept       only the person changed it: it stays as it is
  merged     both changed it, in different places of a text file: the changes are joined line by line
  conflict   both changed the same place (or a file that is not text): the person chooses
Without the original (`base` is None) nothing can be told apart: a file that exists on one side only is kept or taken, a file that differs is a conflict."""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import text as textmerge

SAME, UPSTREAM, KEPT, MERGED, CONFLICT = "same", "upstream", "kept", "merged", "conflict"
OUTCOMES = (SAME, UPSTREAM, KEPT, MERGED, CONFLICT)
CHOICES = ("ours", "theirs", "merged")                    # what a person may pick for one file: the installed one, the new one, or the proposed merge
MAX_TEXT = 200_000
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", ".work", ".versions"})
SKIP_FILES = frozenset({".DS_Store", "Thumbs.db", ".tadamoo-source.json"})


@dataclass
class FileMerge:
    path: str
    outcome: str
    base: Optional[bytes]
    ours: Optional[bytes]
    theirs: Optional[bytes]
    merged: Optional[bytes]                  # the proposal; None when the proposal is that the file is not there
    conflicts: int = 0
    is_text: bool = True
    unresolved: bool = False                 # the proposal still holds conflict markers, or is the installed side only: it cannot be applied as it is

    @property
    def needs_choice(self) -> bool:
        return self.outcome == CONFLICT and self.unresolved


def read_tree(folder: Optional[Path]) -> Dict[str, bytes]:
    """Every regular file under `folder` (tool folders and links left out), by relative path."""
    out: Dict[str, bytes] = {}
    if folder is None or not Path(folder).is_dir():
        return out
    root = Path(folder)
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(current) / d).is_symlink())
        for n in sorted(names):
            p = Path(current) / n
            if n in SKIP_FILES or n.endswith(".pyc") or p.is_symlink() or not p.is_file():
                continue
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def as_text(data: Optional[bytes]) -> Optional[str]:
    """The text of a file, or None when it is not text (not UTF-8, holds a NUL, or is too long to be merged by lines)."""
    if data is None:
        return ""
    if len(data) > MAX_TEXT or b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _one(path: str, base: Optional[bytes], ours: Optional[bytes], theirs: Optional[bytes], has_base: bool) -> FileMerge:
    if ours == theirs:
        return FileMerge(path, SAME, base, ours, theirs, ours)
    if not has_base:
        if ours is None:
            return FileMerge(path, UPSTREAM, base, ours, theirs, theirs)
        if theirs is None:
            return FileMerge(path, KEPT, base, ours, theirs, ours)
        return FileMerge(path, CONFLICT, base, ours, theirs, ours, conflicts=1, is_text=as_text(ours) is not None and as_text(theirs) is not None, unresolved=True)
    if ours == base:
        return FileMerge(path, UPSTREAM, base, ours, theirs, theirs)
    if theirs == base:
        return FileMerge(path, KEPT, base, ours, theirs, ours)
    b, o, t = as_text(base), as_text(ours), as_text(theirs)
    if b is None or o is None or t is None or ours is None or theirs is None:       # both changed it and it cannot be joined by lines: removed on one side and changed on the other, or not text
        return FileMerge(path, CONFLICT, base, ours, theirs, ours, conflicts=1, is_text=not any(x is None for x in (b, o, t)), unresolved=True)
    joined = textmerge.merge_text(b, o, t)
    if joined.clean:
        return FileMerge(path, MERGED, base, ours, theirs, joined.render().encode("utf-8"))
    return FileMerge(path, CONFLICT, base, ours, theirs, joined.render("markers").encode("utf-8"), conflicts=joined.conflicts, unresolved=True)


def plan_trees(base: Optional[Path], ours: Path, theirs: Path) -> Dict[str, FileMerge]:
    """`{relative path: FileMerge}` over every file any of the three folders has. `base` is None when the original is not known."""
    b, o, t = read_tree(base), read_tree(ours), read_tree(theirs)
    has_base = base is not None and Path(base).is_dir()
    return {p: _one(p, b.get(p), o.get(p), t.get(p), has_base) for p in sorted(set(b) | set(o) | set(t))}


def counts(files: Dict[str, FileMerge]) -> Dict[str, int]:
    return {o: sum(1 for f in files.values() if f.outcome == o) for o in OUTCOMES}


def chosen(f: FileMerge, resolution: str = "") -> Optional[bytes]:
    """The content a file ends with: the proposal, or what the person picked for it."""
    if resolution == "ours":
        return f.ours
    if resolution == "theirs":
        return f.theirs
    return f.merged


def unresolved(files: Dict[str, FileMerge], resolutions: Dict[str, str]) -> List[str]:
    """The files that cannot be applied yet: a conflict nobody chose for, or one whose choice is the proposal while the proposal still holds markers or is only the installed side."""
    out = []
    for p, f in files.items():
        r = resolutions.get(p, "")
        if r and r not in CHOICES:
            out.append(p)
        elif f.unresolved and (not r or r == "merged"):
            out.append(p)
    return sorted(out)


def write_final(files: Dict[str, FileMerge], resolutions: Dict[str, str], dest: Path) -> List[str]:
    """Writes the folder the merge ends with (what every file ends as) into the empty `dest`; returns the files it wrote."""
    wrote = []
    for p in sorted(files):
        data = chosen(files[p], resolutions.get(p, ""))
        if data is None:
            continue
        target = Path(dest) / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        wrote.append(p)
    return wrote


def unified(old: Optional[bytes], new: Optional[bytes], name: str, limit: int = 6000) -> str:
    """A short unified diff of one file, for a person or a model to read; empty for a file that is not text."""
    a, b = as_text(old), as_text(new)
    if a is None or b is None:
        return ""
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), f"installed/{name}", f"new/{name}", lineterm="", n=2))
    return "\n".join(lines)[:limit]


def names(files: Dict[str, FileMerge], outcomes: Iterable[str]) -> List[str]:
    wanted = set(outcomes)
    return [p for p, f in files.items() if f.outcome in wanted]
