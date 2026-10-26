"""The folders of the Agent tab. A project is a folder the person added: the agent's conversations in it work in that folder, and the
folder keeps a registry of its own in `.basalt/` (its name and the conversations that were held there)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from serve.agent_runs import AgentRunner, RunError
from serve.tasks import _write_json

PROJECTS_FILE = "projects.json"
REGISTRY_DIR = ".basalt"
REGISTRY_FILE = "project.json"
# what the agent keeps in the folder for itself is not part of the project's own files
IGNORE_LINES = ("artifacts/",)
REGISTRY_VERSION = 1


def _same(a: str, b: str) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


class ProjectStore:
    def __init__(self, directory: str | Path) -> None:
        self.file = Path(directory) / PROJECTS_FILE
        self._lock = threading.RLock()
        self._items: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return [p for p in data.get("projects", []) if isinstance(p, dict) and isinstance(p.get("path"), str)]
        except (OSError, ValueError, AttributeError):
            return []

    def _save(self) -> None:
        _write_json(self.file, {"schema": "1.0", "projects": self._items})

    def list(self) -> List[Dict[str, Any]]:
        """The folders in the order they were last used, each with whether it is still there."""
        with self._lock:
            rows = [{**p, "exists": Path(p["path"]).is_dir()} for p in self._items]
        return sorted(rows, key=lambda p: p.get("used", 0), reverse=True)

    def add(self, path: Any) -> Dict[str, Any]:
        """Adds a folder (or brings an added one to the top) and gives it its registry. The folder must be one the agent can work in."""
        folder = AgentRunner.check_workspace(path)
        self._write_registry(folder, [])
        now = time.time()
        with self._lock:
            known = next((p for p in self._items if _same(p["path"], str(folder))), None)
            if known is None:
                known = {"path": str(folder), "name": folder.name or str(folder), "added": now}
                self._items.append(known)
            known["used"] = now
            self._save()
            return {**known, "exists": True}

    def remove(self, path: Any) -> bool:
        """Forgets a folder: its files, its `.basalt` and its conversations stay where they are."""
        with self._lock:
            before = len(self._items)
            self._items = [p for p in self._items if not _same(p["path"], str(path))]
            if len(self._items) != before:
                self._save()
            return len(self._items) != before

    def known(self, path: Any) -> bool:
        with self._lock:
            return any(_same(p["path"], str(path)) for p in self._items)

    def touch(self, path: Any) -> None:
        with self._lock:
            for p in self._items:
                if _same(p["path"], str(path)):
                    p["used"] = time.time()
                    self._save()
                    return

    # -- the registry inside the folder ----------------------------------------------------------------

    def sync(self, path: Any, sessions: List[Dict[str, Any]]) -> None:
        """Rewrites the folder's registry from the conversations held there. A courtesy: a folder that cannot be written to is left alone."""
        if not self.known(path):
            return
        mine = [s for s in sessions if _same(str(s.get("workspace", "")), str(path)) and s.get("state") != "closed"]
        try:
            self._write_registry(Path(str(path)), mine)
        except (OSError, RunError):
            pass

    def _write_registry(self, folder: Path, sessions: List[Dict[str, Any]]) -> None:
        inside = folder / REGISTRY_DIR
        try:
            inside.mkdir(exist_ok=True)
            ignore = inside / ".gitignore"
            if not ignore.exists():
                ignore.write_text("\n".join(IGNORE_LINES) + "\n", encoding="utf-8")
            registry = inside / REGISTRY_FILE
            created = time.time()
            try:
                created = float(json.loads(registry.read_text(encoding="utf-8")).get("created", created))
            except (OSError, ValueError, AttributeError, TypeError):
                pass
            rows = [{"id": s["id"], "title": s.get("title", ""), "created": s.get("created"), "updated": s.get("updated"), "mode": s.get("mode", "")}
                    for s in sorted(sessions, key=lambda s: s.get("created", 0))]
            _write_json(registry, {"basalt_project": REGISTRY_VERSION, "name": folder.name or str(folder), "created": created, "conversations": rows})
        except OSError as exc:
            raise RunError(f"Basalt cannot write in that folder, so it cannot keep its registry there: {exc}", "folder_readonly") from None
