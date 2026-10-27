"""The folders of this PC a phone may browse and read: the folders added in the Agent tab and the Chat's own folder, and nothing else.

A path is accepted only when, resolved (links and `..` followed), it lies inside one of those folders; the agent's own `.basalt` and `.git` are never listed or
sent. Nothing here writes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

HIDDEN = (".basalt", ".git")
LIST_MAX = 2000
READ_MAX = 500 * 1024 * 1024


class FilesError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class FilesApi:
    def __init__(self, roots: Callable[[], List[Tuple[str, Path]]]) -> None:
        """`roots()` gives the (name, folder) pairs a client may browse."""
        self._roots = roots

    def roots(self) -> List[Dict[str, Any]]:
        out = []
        for name, folder in self._roots():
            try:
                if Path(folder).is_dir():
                    out.append({"name": name, "path": str(Path(folder).resolve())})
            except OSError:
                continue
        return out

    def _inside(self, raw: Any) -> Tuple[Path, Path]:
        """The path as a real path and the root it is inside; anything else is refused."""
        text = str(raw or "").strip()
        if not text or "\0" in text or len(text) > 1000:
            raise FilesError("Name a folder or a file.", "bad_path")
        try:
            target = Path(text).resolve()
        except (OSError, ValueError):
            raise FilesError("That path is not valid.", "bad_path") from None
        for entry in self.roots():
            root = Path(entry["path"])
            try:
                rel = target.relative_to(root)
            except ValueError:
                if os.path.normcase(str(target)) == os.path.normcase(str(root)):
                    rel = Path(".")
                else:
                    continue
            if any(part in HIDDEN for part in rel.parts):
                raise FilesError("That is a folder the agent keeps for itself.", "hidden")
            return target, root
        raise FilesError("That is not inside a folder this server shares.", "outside")

    def list(self, raw: Any) -> Dict[str, Any]:
        target, _ = self._inside(raw)
        if not target.is_dir():
            raise FilesError("That is not a folder.", "not_folder")
        entries = []
        try:
            for child in os.scandir(target):
                if child.name in HIDDEN:
                    continue
                try:
                    stat = child.stat()
                    is_dir = child.is_dir()
                except OSError:
                    continue
                entries.append({"name": child.name, "path": str(Path(child.path)), "dir": is_dir, "size": 0 if is_dir else stat.st_size, "mtime": stat.st_mtime})
                if len(entries) >= LIST_MAX:
                    break
        except OSError as exc:
            raise FilesError(f"That folder cannot be read: {exc}", "unreadable") from None
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"path": str(target), "entries": entries, "cut": len(entries) >= LIST_MAX}

    def file(self, raw: Any) -> Tuple[Path, str]:
        target, _ = self._inside(raw)
        if not target.is_file():
            raise FilesError("That is not a file.", "not_file")
        if target.stat().st_size > READ_MAX:
            raise FilesError("That file is too large to send.", "too_large")
        return target, target.name
