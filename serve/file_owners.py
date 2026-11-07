"""Setting which conversation owns a shared file: one small file in the user data folder, read by every conversation's process
(tadamoo/file_owners.py). The owner is stored as a conversation's id, resolved from the name the person typed (serve/handler.py), so
it still matches after the conversation is renamed."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from tadamoo.file_owners import MODES, norm


class OwnersError(ValueError):
    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class FileOwners:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _stored(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        files = data.get("files") if isinstance(data, dict) else None
        return dict(files) if isinstance(files, dict) else {}

    def _write(self, files: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"schema": "1.0", "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self) -> Dict[str, Dict[str, str]]:
        return self._stored()

    def set(self, path: Any, owner_sid: Any, mode: str = "owner") -> Dict[str, str]:
        if not isinstance(path, str) or not path.strip():
            raise OwnersError("Name the file.", "path")
        if not isinstance(owner_sid, str) or not owner_sid.strip():
            raise OwnersError("Name the conversation that owns it.", "owner")
        if mode not in MODES:
            raise OwnersError("The mode is 'owner' or 'append'.", "mode")
        files = self._stored()
        entry = {"path": path.strip(), "owner": owner_sid.strip(), "mode": mode}
        files[norm(path)] = entry
        self._write(files)
        return entry

    def remove(self, path: Any) -> bool:
        files = self._stored()
        key = norm(path)
        if key not in files:
            return False
        del files[key]
        self._write(files)
        return True
