"""Task-state journal — persists task and sub-task status for canvas and agent tracking.

The journal is append-only and atomically updated on state changes (running,
done, failed, closed). When a task closes and no open task depends on its output,
it is safely purged.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_FILE = "tasks.json"


class TaskJournal:
    """Manages persistent task state with atomic file updates."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / STATE_FILE

    def load(self) -> Dict[str, Any]:
        """Loads the current task journal from disk, returning an empty task dict if missing."""
        if not self.path.is_file():
            return {"schema": "1.0", "tasks": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
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
        data = self.load()
        tasks = data.setdefault("tasks", [])
        task = next((t for t in tasks if t.get("id") == task_id), None)
        now = time.time()

        if task is None:
            task = {
                "id": task_id,
                "created": now,
                "children": list(children or []),
                "metadata": metadata or {},
            }
            tasks.append(task)
        else:
            if children is not None:
                task["children"] = list(children)
            if metadata is not None:
                task.setdefault("metadata", {}).update(metadata)

        task["status"] = status
        task["result"] = result
        task["updated"] = now
        self._write(data)
        return task

    def close(self, task_id: str) -> None:
        """Closes a task. Purges it if no pending task references it as child/dependency."""
        data = self.load()
        tasks = data.get("tasks", [])
        open_ids = {t["id"] for t in tasks if t.get("status") not in ("closed", "done")}

        dependent_ids = set()
        for t in tasks:
            if t.get("id") in open_ids:
                dependent_ids.update(t.get("children", []))

        # Keep if it is not the closed task, or if an open task still depends on it
        data["tasks"] = [
            t for t in tasks
            if t.get("id") != task_id or t.get("id") in dependent_ids
        ]
        self._write(data)

    def _write(self, data: Dict[str, Any]) -> None:
        """Atomically writes JSON using a temp file and replace."""
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.directory), prefix=".tasks.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(self.path))
        finally:
            if Path(tmp).exists():
                try:
                    Path(tmp).unlink(missing_ok=True)
                except OSError:
                    pass
