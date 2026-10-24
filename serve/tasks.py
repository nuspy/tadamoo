"""Task-state journal and change journal: what the agent is doing and what it has edited.

Both are small JSON files in one folder, updated atomically and under a lock, so concurrent requests
of the threading server never lose each other's updates. A task that is closed while an open task
still depends on it is kept, marked closed, and purged as soon as nothing open depends on it.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import review as rv

STATE_FILE = "tasks.json"
CHANGES_FILE = "changes.json"
MAX_EDITS = 200
MAX_REVIEWABLE = 30                 # edits whose before/after text is kept so that blocks can be undone
REVIEW_FILE = "changes_review.json"
ACTIONS = ("keep", "reject", "keep_all", "reject_all")
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
    """The agent's recent file edits, as the changes panel shows them, and the review of their blocks."""

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory) / CHANGES_FILE
        self.review_path = Path(directory) / REVIEW_FILE
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("edits"), list) else {"schema": "1.0", "edits": []}
        except (OSError, json.JSONDecodeError):
            return {"schema": "1.0", "edits": []}

    def _load_review(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.review_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

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
        cleaned: List[Dict[str, Any]] = []
        new_review: Dict[str, Any] = {}
        for raw in edits:
            edit = self.clean(raw)
            edit["id"] = secrets.token_hex(5)
            texts = rv.usable(raw.get("review"), edit["kind"])
            if texts:
                edit["diff"] = rv.unified(texts["old"], texts["current"], edit["file"])[:MAX_DIFF_CHARS]
                edit["added"], edit["removed"] = rv.counts(edit["diff"])
                edit["review"] = "pending" if edit["diff"] else "kept"
                if edit["diff"]:
                    new_review[edit["id"]] = {**texts, "kept": []}
            else:
                edit["review"] = "unavailable"
            cleaned.append(edit)
        with self._lock:
            data = self.load()
            data["edits"] = (data["edits"] + cleaned)[-MAX_EDITS:]
            review = {**self._load_review(), **new_review}
            keep = [e["id"] for e in data["edits"] if e.get("id") in review][-MAX_REVIEWABLE:]
            review = {k: review[k] for k in keep}
            for e in data["edits"]:
                if e.get("review") in ("pending", "partial") and e.get("id") not in review:
                    e["review"] = "unavailable"                      # its text was dropped to bound the file's size
            _write_json(self.review_path, review)
            _write_json(self.path, data)
            return self._view(data, review)

    def clear(self) -> None:
        with self._lock:
            _write_json(self.path, {"schema": "1.0", "edits": []})
            _write_json(self.review_path, {})

    # -- review ----------------------------------------------------------------------------------

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return self._view(self.load(), self._load_review())

    @staticmethod
    def _blocked_by_later(edits: List[Dict[str, Any]], pos: int, review: Dict[str, Any]) -> bool:
        edit, mine = edits[pos], review.get(edits[pos].get("id"))
        if mine is None:
            return False
        for later in edits[pos + 1:]:
            theirs = review.get(later.get("id"))
            if theirs is not None and theirs["root"] == mine["root"] and later.get("file") == edit.get("file"):
                return True
        return False

    @staticmethod
    def _view(data: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
        """The edits as the panel sees them: an open one says which blocks are kept, or that a later edit blocks it."""
        edits = data.get("edits", [])
        out = []
        for pos, edit in enumerate(edits):
            shown = dict(edit)
            entry = review.get(edit.get("id"))
            if entry is not None and edit.get("review") in ("pending", "partial"):
                gs = rv.groups(entry["old"], entry["current"])
                shown["blocks"] = [rv.fingerprint(entry["old"], entry["current"], g) in entry["kept"] for g in gs]
                if ChangeJournal._blocked_by_later(edits, pos, review):
                    shown["review"] = "superseded"
            out.append(shown)
        return {**data, "edits": out}

    def review_action(self, edit_id: str, action: str, block: Optional[int] = None) -> Dict[str, Any]:
        """Keeps or undoes a block or the whole edit. Raises rv.ReviewError with a message for the person."""
        if action not in ACTIONS:
            raise ValueError("action must be one of " + ", ".join(ACTIONS))
        if action in ("keep", "reject") and (not isinstance(block, int) or isinstance(block, bool) or block < 0):
            raise ValueError("a block number (from 0) is needed")
        with self._lock:
            data, review = self.load(), self._load_review()
            edits = data["edits"]
            pos = next((i for i, e in enumerate(edits) if e.get("id") == edit_id), None)
            if pos is None:
                raise rv.ReviewError("That change is not in the list any more.")
            edit, entry = edits[pos], review.get(edit_id)
            if entry is None or edit.get("review") not in ("pending", "partial"):
                raise rv.ReviewError("This change cannot be reviewed here.")
            if self._blocked_by_later(edits, pos, review):
                raise rv.ReviewError("A later edit changed this file; review that one first.")
            target = rv.safe_path(entry["root"], edit["file"])
            if rv.read(target) != entry["current"]:
                raise rv.ReviewError("The file changed after the agent's edit. Review it in your editor.")
            old, current = entry["old"], entry["current"]
            gs = rv.groups(old, current)
            if action in ("keep", "reject") and block >= len(gs):
                raise rv.ReviewError(f"There is no block {block + 1} in this change.")
            kept = set(entry["kept"])
            if action == "keep":
                kept.add(rv.fingerprint(old, current, gs[block]))
            elif action == "keep_all":
                kept.update(rv.fingerprint(old, current, g) for g in gs)
            else:
                new_text = old if action == "reject_all" else rv.revert(old, current, block)
                if edit["kind"] == "added" and new_text == "":
                    try:
                        target.unlink()
                    except OSError as exc:
                        raise rv.ReviewError(f"The file cannot be removed: {exc.strerror or exc}") from None
                else:
                    rv.write(target, new_text)
                entry["current"] = current = new_text
            gs = rv.groups(old, current)
            prints = [rv.fingerprint(old, current, g) for g in gs]
            entry["kept"] = sorted(k for k in kept if k in prints)
            edit["diff"] = rv.unified(old, current, edit["file"])[:MAX_DIFF_CHARS]
            edit["added"], edit["removed"] = rv.counts(edit["diff"])
            if not gs:
                edit["review"] = "reverted"
            elif all(p in entry["kept"] for p in prints):
                edit["review"] = "kept"
            else:
                edit["review"] = "partial" if entry["kept"] else "pending"
            if edit["review"] in ("kept", "reverted"):
                review.pop(edit_id, None)                            # nothing is left to decide: drop the stored text
            else:
                review[edit_id] = entry
            _write_json(self.review_path, review)
            _write_json(self.path, data)
            return next(e for e in self._view(data, review)["edits"] if e["id"] == edit_id)
