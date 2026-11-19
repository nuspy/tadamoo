"""Chat's folders: a container of conversations the person made (right-click to add one, to rename it, or a conversation into it -
serve/web/agentchat.js). Global, one list, persisted whole - the same pattern as tadamoo/contacts.py. A folder has no code
workspace of its own (that is Agentic coding's project, serve/projects.py): it is purely a place to group chats, and its own
subfolder under the chat base (<chat base>/sessions/folders/<id>/) is where the chats it holds keep their files, resolved by
AgentSessions from a session's `folder_id` (serve/agent_sessions.py)."""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, Dict, List

from serve.util.files import write_json

NAME_MAX = 80


class FolderError(ValueError):
    pass


def _new_id() -> str:
    return secrets.token_hex(4)


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def default_file() -> Dict[str, Any]:
    return {"schema": "1.0", "folders": []}


class ChatFolderStore:
    """Loaded whole and saved whole: at the scale this holds (folders, not conversations), that is simpler and safer than a
    partial update, and every write is atomic (serve/util/files.py write_json)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("folders"), list):
            return default_file()
        return {"schema": "1.0", "folders": [f for f in data["folders"] if isinstance(f, dict) and isinstance(f.get("id"), str)]}

    def save(self, data: Dict[str, Any]) -> None:
        write_json(self.path, data)

    def list(self) -> List[Dict[str, Any]]:
        return self.load()["folders"]

    def known(self, folder_id: Any) -> bool:
        return any(f["id"] == folder_id for f in self.list())

    def add(self, name: Any) -> Dict[str, Any]:
        name = _text(name, NAME_MAX)
        if not name:
            raise FolderError("A folder needs a name.")
        data = self.load()
        row = {"id": _new_id(), "name": name}
        data["folders"].append(row)
        self.save(data)
        return row

    def rename(self, folder_id: Any, name: Any) -> Dict[str, Any]:
        name = _text(name, NAME_MAX)
        if not name:
            raise FolderError("A folder needs a name.")
        data = self.load()
        row = next((f for f in data["folders"] if f["id"] == folder_id), None)
        if row is None:
            raise FolderError("That folder is not there any more.")
        row["name"] = name
        self.save(data)
        return row

    def remove(self, folder_id: Any) -> bool:
        """Forgets the folder. The conversations it held, and their own files, are someone else's job to take away first
        (serve/handler.py, the same order as removing an Agentic coding project: sessions, then the subfolder, then this)."""
        data = self.load()
        before = len(data["folders"])
        data["folders"] = [f for f in data["folders"] if f["id"] != folder_id]
        if len(data["folders"]) == before:
            raise FolderError("That folder is not there any more.")
        self.save(data)
        return True
