"""How much of a conversation's day a conversation has spent, in tokens (serve/agent_sessions.py `over_budget`, used by the
delivery queue to refuse a message to a conversation that has spent what it may spend today).

A conversation's process reports a cumulative total since it last started (`AgentLoop.total_tokens`, in every "done" event); it is
not reset between turns, only when the process itself restarts. A number lower than the one last seen means the process started
over, so that number is this call's own usage, not a difference. The day resets at local midnight; nothing here persists across a
server restart, so a restart of TaDaMoo itself also resets the day (the same as any other in-memory count in this file)."""
from __future__ import annotations

import datetime
import threading
from typing import Dict, Optional, Tuple


class SessionBudget:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage: Dict[str, Tuple[str, int, int]] = {}      # sid -> (date, last total seen, tokens counted today)

    @staticmethod
    def _today() -> str:
        return datetime.date.today().isoformat()

    def record(self, sid: str, total_tokens: int) -> None:
        if not isinstance(total_tokens, int) or isinstance(total_tokens, bool) or total_tokens < 0:
            return
        today = self._today()
        with self._lock:
            date, last_total, day_tokens = self._usage.get(sid, (today, 0, 0))
            if date != today:
                last_total, day_tokens = 0, 0
            delta = total_tokens if total_tokens < last_total else total_tokens - last_total
            self._usage[sid] = (today, total_tokens, day_tokens + delta)

    def used_today(self, sid: str) -> int:
        with self._lock:
            date, _, day_tokens = self._usage.get(sid, (self._today(), 0, 0))
            return day_tokens if date == self._today() else 0

    def over(self, sid: str, limit: Optional[int]) -> bool:
        return bool(limit) and self.used_today(sid) >= limit
