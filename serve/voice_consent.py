"""The questions the voice service puts to the person about the knowledge archives: "may the content of this archive be said, written or shown to the other
people of this meeting or call?".

An archive's `others_ok` is allow, deny, ask_dialog or ask_voice. For the two asking kinds the voice service posts a question here the first time a passage of
the archive would reach other people. The page shows it as a dialog on any page (`serve/web/voiceconsent.js`) naming the archive and what would be output; with
ask_voice the service also asks aloud over a channel the others cannot hear, and the first answer to arrive counts. The answers:

    once     this time only                      meeting   for the rest of this meeting or call
    always   for this archive: its `others_ok` becomes allow          no   not now: nothing of this archive reaches the others in this meeting

Nothing waits for an answer: until one comes the archive's content is left out of what the others can perceive. A question not answered in `TTL_S` seconds is
answered no."""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional

ANSWERS = ("once", "meeting", "always", "no")
VIAS = ("dialog", "voice")
HOWS = ("spoken", "written", "shown")
TTL_S = 180.0
KEEP_S = 600.0
MAX_PENDING = 20
TEXT_MAX = 400


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


class Consents:
    """The open questions and their answers; one per (session, archive) at a time."""

    def __init__(self, save_always: Callable[[str], bool] = lambda aid: False, clock: Callable[[], float] = time.monotonic) -> None:
        self._save, self._clock = save_always, clock
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        for row in self._rows.values():
            if row["state"] == "pending" and now >= row["expires"]:
                row.update(state="answered", answer="no", by="timeout", answered=now)
        for rid in [r for r, row in self._rows.items() if row["state"] == "answered" and now - row["answered"] > KEEP_S]:
            del self._rows[rid]

    def ask(self, session: str, function: str, archive_id: str, archive: str, preview: str, how: str = "spoken", via: str = "dialog",
            fallback: bool = False) -> Dict[str, Any]:
        """The question about an archive in a session: the open one when there is one already, else a new one."""
        if how not in HOWS or via not in VIAS:
            raise ValueError("how is spoken, written or shown; via is dialog or voice")
        now = self._clock()
        with self._lock:
            self._expire(now)
            for row in self._rows.values():
                if row["state"] == "pending" and row["session"] == session and row["archive_id"] == archive_id:
                    return self._view(row)
            if sum(1 for r in self._rows.values() if r["state"] == "pending") >= MAX_PENDING:
                raise ValueError("too many questions are waiting")
            rid = secrets.token_hex(8)
            row = {"id": rid, "session": _text(session, 80), "function": _text(function, 20), "archive_id": _text(archive_id, 80), "archive": _text(archive, 80),
                   "preview": _text(preview, TEXT_MAX), "how": how, "via": via, "state": "pending", "answer": "", "by": "", "created": now, "expires": now + TTL_S,
                   "answered": 0.0, "saved": False, "fallback": bool(fallback)}
            self._rows[rid] = row
            return self._view(row)

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._expire(self._clock())
            row = self._rows.get(rid)
            return self._view(row) if row else None

    def answer(self, rid: str, answer: str, by: str = "dialog") -> Optional[Dict[str, Any]]:
        """The answer of the person (the first one counts). None for a question that is not known; the row as it stands otherwise."""
        if answer not in ANSWERS:
            raise ValueError("the answer is once, meeting, always or no")
        with self._lock:
            now = self._clock()
            self._expire(now)
            row = self._rows.get(rid)
            if row is None:
                return None
            if row["state"] == "pending":
                row.update(state="answered", answer=answer, by=by, answered=now)
                if answer == "always":
                    row["saved"] = bool(self._save(row["archive_id"]))
            return self._view(row)

    def pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._expire(self._clock())
            return [self._view(r) for r in self._rows.values() if r["state"] == "pending"]

    @staticmethod
    def _view(row: Dict[str, Any]) -> Dict[str, Any]:
        return {k: row[k] for k in ("id", "session", "function", "archive_id", "archive", "preview", "how", "via", "state", "answer", "by", "saved", "fallback")}
