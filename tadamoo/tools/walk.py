"""Walking a workspace folder for the read-only tools `search_code` and `tree`: .gitignore aware, never leaving the workspace."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from tadamoo.gitignore import GitIgnore
from tadamoo.tools.filesystem import SKIP_DIRS

MAX_FILES = 50_000                 # most files one walk looks at
MAX_FILE_BYTES = 1_000_000         # a larger file is skipped by the search and not counted line by line


def is_binary(head: bytes) -> bool:
    return b"\x00" in head[:8000]


class Walker:
    """Lists a folder's visible entries: not in a skipped folder, not matched by a .gitignore, not a link out of the workspace, and not
    a path the workspace's rules keep from the file tools (`resolve` raises PermissionError for those)."""

    def __init__(self, root: Path, resolve: Callable[[Path], Path]) -> None:
        self.root = root
        self.resolve = resolve
        self.ignore = GitIgnore(root)

    def allowed(self, path: Path, is_dir: bool) -> bool:
        try:
            self.resolve(path)
        except (PermissionError, OSError):
            return False
        return not self.ignore.ignored(path, is_dir)

    def entries(self, folder: Path) -> Tuple[List[Path], List[Path]]:
        """(folders, files) of `folder`, each sorted by name."""
        dirs: List[Path] = []
        files: List[Path] = []
        try:
            names = sorted(os.listdir(folder), key=lambda n: n.lower())
        except OSError:
            return dirs, files
        for name in names:
            full = folder / name
            try:
                is_dir = full.is_dir()
                is_file = full.is_file()
            except OSError:
                continue
            if is_dir:
                if name in SKIP_DIRS or full.is_symlink() or not self.allowed(full, True):
                    continue
                dirs.append(full)
            elif is_file and self.allowed(full, False):
                files.append(full)
        return dirs, files

    def files_below(self, folder: Path, limit: int = MAX_FILES) -> Tuple[List[Path], bool]:
        """Every visible file below `folder`, depth first in name order, and whether the walk stopped at `limit`."""
        found: List[Path] = []
        stack = [folder]
        while stack:
            dirs, files = self.entries(stack.pop())
            for f in files:
                if len(found) >= limit:
                    return found, True
                found.append(f)
            stack.extend(reversed(dirs))
        return found, False


def count_lines(path: Path, limit: int = 5_000_000) -> Optional[int]:
    """The number of lines of a text file; None for a binary file or one larger than `limit` bytes."""
    try:
        if path.stat().st_size > limit:
            return None
        lines, last = 0, b"\n"
        with open(path, "rb") as f:
            first = True
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                if first and is_binary(chunk):
                    return None
                first = False
                lines += chunk.count(b"\n")
                last = chunk[-1:]
        return lines + (0 if last == b"\n" else 1)
    except OSError:
        return None
