"""The audit of the meeting bot: one JSON line per event in `meetings-audit.jsonl`, next to voice.json.

It says when the bot joined, on which platform, how it announced itself, how consent went, what ended the session and how long it lasted.
It never holds what was said, a meeting address or a name in clear (names are masked)."""
from __future__ import annotations

from ..call_policy import AuditError, CallAudit  # noqa: F401  (AuditError is re-exported for the callers)


def mask_name(name: str) -> str:
    """"Mario Rossi" -> "M*** R***": the first letter of each word."""
    words = [w for w in (name or "").split() if w]
    return " ".join(w[0] + "***" for w in words)


class MeetingAudit(CallAudit):
    """Same file format and rotation as the audit of phone calls."""

    def join(self, session_id: str, platform: str, **fields) -> None:
        """The first row of a session. If it cannot be written the bot does not join (AuditError)."""
        self.write(event="join", id=session_id, platform=platform, **fields)

    def event(self, session_id: str, event: str, **fields) -> None:
        """A later row. A failure to write it is logged by the caller and does not stop the meeting."""
        self.write(event=event, id=session_id, **fields)
