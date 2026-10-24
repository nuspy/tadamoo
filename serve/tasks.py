"""Task-state journal and change journal: what the agent is doing and what it has edited.

Both are small JSON files in one folder, updated atomically and under a lock, so concurrent requests
of the threading server never lose each other's updates. A task that is closed while an open task
still depends on it is kept, marked closed, and purged as soon as nothing open depends on it.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_FILE = "tasks.json"
CHANGES_FILE = "changes.json"
MAX_EDITS = 200
MAX_DIFF_CHARS = 100_000
OPEN_STATES = ("pending", "running", "in_progress", "blocked")


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    finally:
        if Path(tmp).exists():
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass


class TaskJournal:
    """Manages persistent task state with atomic file updates."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / STATE_FILE
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        """The current journal, or an empty one when the file is missing or unreadable."""
        if not self.path.is_file():
            return {"schema": "1.0", "tasks": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("tasks"), list) else {"schema": "1.0", "tasks": []}
        except (json.JSONDecodeError, OSError):
            return {"schema": "1.0", "tasks": []}

    def record(
        self,
        task_id: str,
        status: str,
        result: str = "",
        children: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Records or updates a task status, returning the updated task object."""
        with self._lock:
            data = self.load()
            tasks = data.setdefault("tasks", [])
            task = next((t for t in tasks if t.get("id") == task_id), None)
            now = time.time()
            if task is None:
                task = {"id": task_id, "created": now, "children": list(children or []), "metadata": metadata or {}}
                tasks.append(task)
            else:
                if children is not None:
                    task["children"] = list(children)
                if metadata is not None:
                    task.setdefault("metadata", {}).update(metadata)
            task["status"] = status
            task["result"] = result
            task["updated"] = now
            self._purge(data)
            _write_json(self.path, data)
            return task

    @staticmethod
    def _purge(data: Dict[str, Any]) -> None:
        """Drops closed tasks that no open task lists as a child."""
        tasks = data.get("tasks", [])
        needed = {c for t in tasks if t.get("status") in OPEN_STATES for c in t.get("children", [])}
        data["tasks"] = [t for t in tasks if t.get("status") != "closed" or t.get("id") in needed]

    def close(self, task_id: str) -> None:
        """Closes a task; it disappears unless an open task still depends on it."""
        with self._lock:
            data = self.load()
            for t in data.get("tasks", []):
                if t.get("id") == task_id:
                    t["status"], t["updated"] = "closed", time.time()
            self._purge(data)
            _write_json(self.path, data)


class ChangeJournal:
    """The agent's recent file edits, as the changes panel shows them."""

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory) / CHANGES_FILE
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("edits"), list) else {"schema": "1.0", "edits": []}
        except (OSError, json.JSONDecodeError):
            return {"schema": "1.0", "edits": []}

    @staticmethod
    def clean(edit: Any) -> Dict[str, Any]:
        """One edit reduced to the fields the panel shows; raises ValueError for anything else."""
        if not isinstance(edit, dict) or not isinstance(edit.get("file"), str) or not edit["file"]:
            raise ValueError("an edit needs a file name")
        kind = edit.get("kind", "modified")
        if kind not in ("added", "modified", "deleted"):
            raise ValueError("kind must be added, modified or deleted")
        added, removed = edit.get("added", 0), edit.get("removed", 0)
        if not isinstance(added, int) or not isinstance(removed, int) or isinstance(added, bool) or isinstance(removed, bool):
            raise ValueError("added and removed must be whole numbers")
        diff = edit.get("diff", "")
        if not isinstance(diff, str):
            raise ValueError("diff must be text")
        return {"file": edit["file"][:500], "kind": kind, "added": max(added, 0), "removed": max(removed, 0),
                "diff": diff[:MAX_DIFF_CHARS], "time": time.time()}

    def add(self, edits: List[Any]) -> Dict[str, Any]:
        if not isinstance(edits, list):
            raise ValueError("edits must be a list")
        cleaned = [self.clean(e) for e in edits]
        with self._lock:
            data = self.load()
            data["edits"] = (data["edits"] + cleaned)[-MAX_EDITS:]
            _write_json(self.path, data)
            return data

    def clear(self) -> None:
        with self._lock:
            _write_json(self.path, {"schema": "1.0", "edits": []})
