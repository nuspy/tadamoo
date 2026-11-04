"""The Lessons page's side: the lessons of a project folder and the global ones, the outcomes summary, and the person's edits.

Only the server's own page reaches it. A folder is named by its path and must be one the server already knows (a project of the Agent
tab or the Chat's own folder). Promoting a lesson into the project's AGENTS.md is two requests: one shows the text, the other writes it
and only with `confirm: true`; the file is never overwritten, only appended to.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from tadamoo import repeats
from tadamoo.lessons import Lesson, LessonStore, OutcomeLog, RESULTS, screen_reason, write_atomic

RECENT_OUTCOMES = 50              # the outcomes the summary counts
TOP_ERROR_TOOLS = 3
AGENTS_FILE = "AGENTS.md"
AGENTS_MAX_BYTES = 1 << 20
AGENTS_HEADING = "## Lessons from earlier work"
AGENTS_HEADER = "# AGENTS.md\n\n" + AGENTS_HEADING + "\n\n"
EDITABLE = ("situation", "went_wrong", "fix")
ACTIONS = ("add", "edit", "pin", "confirm", "delete", "promote", "append_agents")


class LessonsError(ValueError):
    pass


def _same(a: "str | Path", b: "str | Path") -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def view(lesson: Lesson) -> Dict[str, Any]:
    return {"id": lesson.id, "scope": lesson.scope, "situation": lesson.situation, "went_wrong": lesson.went_wrong, "fix": lesson.fix,
            "date": lesson.date, "confirmed": lesson.confirmed, "pinned": lesson.pinned, "evidence": list(lesson.evidence)}


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts of the recent outcomes: by result, the average number of steps, the tools that failed most, and how often the person corrected."""
    steps = [r["steps"] for r in records if isinstance(r.get("steps"), (int, float)) and not isinstance(r.get("steps"), bool)]
    errors: Counter = Counter()
    for r in records:
        for tool, n in (r.get("errors") if isinstance(r.get("errors"), dict) else {}).items():
            if isinstance(n, int) and not isinstance(n, bool):
                errors[str(tool)] += n
    return {"count": len(records), "by_result": {k: sum(1 for r in records if r.get("result") == k) for k in RESULTS},
            "avg_steps": round(sum(steps) / len(steps), 1) if steps else 0, "top_error_tools": [[t, n] for t, n in errors.most_common(TOP_ERROR_TOOLS)],
            "corrected": sum(1 for r in records if r.get("corrected_by_person") is True)}


class LessonsApi:
    """`folders` gives the (name, path) of every folder the page may use; `global_dir` holds the lessons that hold everywhere."""

    def __init__(self, folders: Callable[[], List[Tuple[str, Path]]], global_dir: "str | Path") -> None:
        self.folders, self.global_dir = folders, Path(global_dir)

    def _folder(self, given: Any) -> Path:
        known = [(n, Path(p)) for n, p in self.folders()]
        if not isinstance(given, str) or not given.strip():
            raise LessonsError("choose a folder")
        for _, path in known:
            if _same(path, given.strip()):
                return path
        raise LessonsError("that folder is not one of the projects or the Chat's folder")

    def _store(self, folder: Any) -> LessonStore:
        return LessonStore(self._folder(folder), self.global_dir)

    def overview(self, folder: str = "") -> Dict[str, Any]:
        known = [{"name": n, "path": str(p), "exists": Path(p).is_dir()} for n, p in self.folders()]
        if not folder and known:
            folder = known[0]["path"]
        out: Dict[str, Any] = {"folders": known, "folder": "", "project": [], "global": [], "outcomes": summarize([]), "agents": False, "repeats": []}
        if not known:
            return out
        path = self._folder(folder)
        store = LessonStore(path, self.global_dir)
        every = OutcomeLog(path).read()
        out.update({"repeats": repeats.groups(every)[:5], "folder": str(path), "project": [view(l) for l in store.list("project")], "global": [view(l) for l in store.list("global")],
                    "outcomes": summarize(every[-RECENT_OUTCOMES:]), "agents": (path / AGENTS_FILE).is_file()})
        return out

    def post(self, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        if action not in ACTIONS:
            raise LessonsError("unknown action")
        store, lesson_id = self._store(req.get("folder")), str(req.get("id") or "")
        if action == "add":
            scope = req.get("scope", "project")
            if scope not in ("project", "global"):
                raise LessonsError("the scope is project or global")
            return self._done(store.add(*(str(req.get(k) or "") for k in EDITABLE), scope=scope), store)
        if not store.get(lesson_id):
            raise LessonsError("that lesson is not there any more")
        if action == "edit":
            fields = {k: str(req[k]) for k in EDITABLE if k in req}
            if not fields:
                raise LessonsError("send the situation, what went wrong or the fix")
            return self._done(store.edit(lesson_id, **fields), store)
        if action == "pin":
            return self._done(store.edit(lesson_id, pinned=req.get("pinned") is not False), store)
        if action == "confirm":
            store.confirm(lesson_id)
            return {"lesson": view(store.get(lesson_id))}
        if action == "delete":
            if not store.delete(lesson_id):
                raise LessonsError("that lesson could not be deleted")
            return {"deleted": True}
        if action == "promote":
            return {"text": self._promotion(store, lesson_id), "file": str(self._folder(req.get("folder")) / AGENTS_FILE)}
        if action == "append_agents":
            if req.get("confirm") is not True:
                raise LessonsError("adding to AGENTS.md needs the confirmation")
        return self._append(self._folder(req.get("folder")), self._promotion(store, lesson_id))

    @staticmethod
    def _done(result: Any, store: LessonStore) -> Dict[str, Any]:
        if not result.ok:
            raise LessonsError(result.reason or "the lesson was not kept")
        return {"lesson": view(store.get(result.id)), "action": result.action}

    @staticmethod
    def _promotion(store: LessonStore, lesson_id: str) -> str:
        text = store.promote(lesson_id)
        if not text or screen_reason(text):
            raise LessonsError("that lesson cannot be promoted")
        return text

    @staticmethod
    def _append(folder: Path, bullet: str) -> Dict[str, Any]:
        """Adds the bullet to the end of the project's AGENTS.md (a new file gets a short header). Nothing already there is changed."""
        path = folder / AGENTS_FILE
        try:
            if path.exists():
                if path.stat().st_size > AGENTS_MAX_BYTES:
                    raise LessonsError("AGENTS.md is too large to add to from here")
                old = path.read_bytes().decode("utf-8")
            else:
                old = ""
        except UnicodeDecodeError:
            raise LessonsError("AGENTS.md is not UTF-8 text, so it was left alone") from None
        except OSError as e:
            raise LessonsError(f"AGENTS.md could not be read: {e.strerror or e}") from None
        if bullet in old:
            return {"appended": False, "already": True, "file": str(path)}
        eol = "\r\n" if "\r\n" in old else "\n"
        if not old:
            new = AGENTS_HEADER + bullet + "\n"
        else:
            lead = "" if old.endswith("\n") else eol
            heading = "" if AGENTS_HEADING in old else eol + AGENTS_HEADING + eol + eol
            new = old + lead + heading + bullet + eol
        try:
            write_atomic(path, new)
        except OSError as e:
            raise LessonsError(f"AGENTS.md could not be written: {e.strerror or e}") from None
        return {"appended": True, "created": not old, "file": str(path)}
