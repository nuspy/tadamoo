"""The Memories page's side: the switches, the weights and the notes themselves, for the person to read, correct and delete.

Only the server's own page reaches it (a phone's list of routes does not include it): the notes are about the person and stay on this PC.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from harness.personal_memory import INFERRED_SECTIONS, LONG_SECTIONS, ORIGINS, SHORT_SECTIONS, WEIGHTS, PersonalMemory, PersonalMemoryError

SHOWN = 500                     # notes one answer lists; the page narrows them with a section or a search


class MemoriesApi:
    def __init__(self, memory: PersonalMemory, worker: Any = None) -> None:
        self.memory, self.worker = memory, worker

    def overview(self, query: str = "", kind: str = "", section: str = "") -> Dict[str, Any]:
        rows = self.memory.items(kind or None, section or None, query)
        return {"settings": self.memory.settings(), "weights": list(WEIGHTS), "origins": list(ORIGINS),
                "sections": {"long": list(LONG_SECTIONS), "short": list(SHORT_SECTIONS)}, "inferred": sorted(INFERRED_SECTIONS),
                "counts": {"long": self.memory.count("long"), "short": self.memory.count("short")},
                "items": rows[:SHOWN], "more": max(0, len(rows) - SHOWN), "log": self.memory.log()[:30],
                "worker": self.worker.status() if self.worker is not None else {"waiting": self.memory.queue_len(), "error": "", "last": None}}

    def post(self, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        if action == "settings":
            return {"settings": self.memory.set_settings(req)}
        if action == "item":
            if req.get("id"):
                return {"item": self.memory.update(str(req["id"]), text=req.get("text"), section=req.get("section"),
                                                   origin=req.get("origin"), confidence=req.get("confidence"))}
            return {"item": self.memory.add(str(req.get("kind") or ""), str(req.get("section") or ""), req.get("text"), origin="manual", source="page")}
        if action == "confirm":
            return {"item": self.memory.confirm(str(req.get("id") or ""))}
        if action == "delete":
            if not self.memory.delete(str(req.get("id") or "")):
                raise PersonalMemoryError("that note is not there any more")
            return {"deleted": True}
        if action == "clear":
            kind: Optional[str] = req.get("kind") or None
            if kind not in (None, "long", "short"):
                raise PersonalMemoryError("clear the long-term memory, the short-term one, or both")
            if req.get("confirm") is not True:
                raise PersonalMemoryError("deleting every note needs the confirmation")
            return {"deleted": self.memory.clear(kind)}
        raise PersonalMemoryError("unknown action")
