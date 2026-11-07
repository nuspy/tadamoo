"""Which conversation owns a shared file, read by the conversation that works on it (tadamoo/tools/filesystem.py checks every write and
edit against it). Set and changed by the server (serve/file_owners.py), in one small file every conversation's process reads again on
each check, since the person can change an owner while conversations are running.

`mode` is "owner" (only that conversation writes it; any other write is refused, pointed at session_send) or "append" (any conversation
may add whole lines to the end, under a lock, but not rewrite or remove what is already there)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

FILE = "file_owners.json"
MODES = ("owner", "append")


def norm(path: Any) -> str:
    """A path as the key it is stored and looked up under: resolved, forward slashes, case folded (Windows paths are not case sensitive)."""
    return str(Path(str(path)).resolve()).replace("\\", "/").lower()


class FileOwnership:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _stored(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}

    def owner_of(self, path: Any) -> Optional[Tuple[str, str]]:
        """(owner conversation id, mode) for a path, or None when nobody owns it."""
        entry = self._stored().get(norm(path))
        if not isinstance(entry, dict) or not isinstance(entry.get("owner"), str) or not entry["owner"]:
            return None
        mode = entry.get("mode")
        return entry["owner"], mode if mode in MODES else "owner"
