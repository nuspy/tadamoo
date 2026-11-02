"""`tree`: a folder as an indented tree with the number of lines in each file and in each folder, .gitignore aware."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tadamoo.tools.registry import EffectClass
from tadamoo.tools.walk import MAX_FILES, Walker, count_lines

DEFAULT_DEPTH = 2
MAX_DEPTH = 8
MAX_ENTRIES = 400


class _Totals:
    def __init__(self) -> None:
        self.files = 0
        self.lines = 0
        self.seen = 0                       # files looked at in all, against MAX_FILES
        self.partial = False


def _walk(walker: Walker, folder: Path, depth: int, level: int, totals: _Totals, rows: List[str]) -> Tuple[int, int]:
    """Adds the rows of `folder`'s entries down to `depth` levels; returns (files, lines) of everything below it."""
    dirs, files = walker.entries(folder)
    indent = "  " * level
    here_files = here_lines = 0
    file_rows: List[str] = []
    for f in files:
        totals.seen += 1
        if totals.seen > MAX_FILES:
            totals.partial = True
            break
        lines = count_lines(f)
        here_files += 1
        here_lines += lines or 0
        if level <= depth:
            file_rows.append(f"{indent}{f.name} - " + (f"{lines} lines" if lines is not None else "binary or large"))
    for d in dirs:
        if totals.partial:
            break
        mark = len(rows)
        if level <= depth:
            rows.append("")                                    # the folder's own row, filled in once its totals are known
        sub_files, sub_lines = _walk(walker, d, depth, level + 1, totals, rows)
        here_files += sub_files
        here_lines += sub_lines
        if level <= depth:
            rows[mark] = f"{indent}{d.name}/ - {sub_files} file{'s' if sub_files != 1 else ''}, {sub_lines} lines"
    rows.extend(file_rows)
    return here_files, here_lines


def build_tree(root: Path, resolve: Callable[[Path], Path], folder: Path, depth: int = DEFAULT_DEPTH) -> str:
    depth = max(1, min(int(depth), MAX_DEPTH))
    walker = Walker(root, resolve)
    totals = _Totals()
    rows: List[str] = []
    files, lines = _walk(walker, folder, depth, 1, totals, rows)
    shown = folder.relative_to(root).as_posix() if folder != root else "."
    out = [f"{shown}/ - {files} file{'s' if files != 1 else ''}, {lines} lines"]
    out.extend(rows if len(rows) <= MAX_ENTRIES else rows[:MAX_ENTRIES])
    if len(rows) > MAX_ENTRIES:
        out.append(f"[{len(rows) - MAX_ENTRIES} more entries not shown: give a subfolder as the path, or a smaller depth]")
    if totals.partial:
        out.append(f"[only the first {MAX_FILES} files were counted, so the totals are partial: give a subfolder as the path]")
    return "\n".join(out)


def register_tree(registry: Any, resolve_safe: Callable[[str], Path], root: Path) -> None:
    def tree(path: str = ".", depth: int = DEFAULT_DEPTH) -> str:
        folder = resolve_safe(path or ".")
        if not folder.is_dir():
            raise NotADirectoryError(f"Folder not found: {path}. The working folder is {root}; paths are relative to it.")
        return build_tree(root, resolve_safe, folder, depth)

    registry.register(
        name="tree",
        description="Shows a folder as a tree with the number of lines in each file and in each folder; skips what .gitignore lists. "
                    "Use it to find your way in a project.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder, relative to the workspace (default: all of it)"},
                "depth": {"type": "integer", "description": "How many levels to show (1 to 8, default 2)"},
            },
        },
        effect=EffectClass.PURE,
        handler=tree,
    )
