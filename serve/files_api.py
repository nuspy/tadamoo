"""The files of this PC a phone may browse and read: any folder or file, from where it starts to wherever it goes. Nothing here writes."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple


class FilesError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def _drives() -> List[Tuple[str, Path]]:
    """The places the whole disk starts from: the drive letters that exist on Windows, `/` elsewhere."""
    if os.name != "nt":
        return [("/", Path("/"))]
    return [(f"{letter}:", Path(f"{letter}:\\")) for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:\\").exists()]


class FilesApi:
    def __init__(self, roots: Callable[[], List[Tuple[str, Path]]]) -> None:
        """`roots()` gives the (name, folder) pairs offered first; the drives of the PC follow them."""
        self._roots = roots

    def roots(self) -> List[Dict[str, Any]]:
        out = []
        for name, folder in [*self._roots(), *_drives()]:
            try:
                if Path(folder).is_dir():
                    out.append({"name": name, "path": str(Path(folder).resolve())})
            except OSError:
                continue
        return out

    @staticmethod
    def _path(raw: Any) -> Path:
        text = str(raw or "").strip()
        if not text or "\0" in text or len(text) > 1000:
            raise FilesError("Name a folder or a file.", "bad_path")
        try:
            return Path(text).resolve()
        except (OSError, ValueError):
            raise FilesError("That path is not valid.", "bad_path") from None

    def list(self, raw: Any) -> Dict[str, Any]:
        target = self._path(raw)
        if not target.is_dir():
            raise FilesError("That is not a folder.", "not_folder")
        entries = []
        try:
            for child in os.scandir(target):
                try:
                    stat = child.stat()
                    is_dir = child.is_dir()
                except OSError:
                    continue
                entries.append({"name": child.name, "path": str(Path(child.path)), "dir": is_dir, "size": 0 if is_dir else stat.st_size, "mtime": stat.st_mtime})
        except OSError as exc:
            raise FilesError(f"That folder cannot be read: {exc}", "unreadable") from None
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"path": str(target), "entries": entries}

    def file(self, raw: Any) -> Tuple[Path, str]:
        target = self._path(raw)
        if not target.is_file():
            raise FilesError("That is not a file.", "not_file")
        return target, target.name
