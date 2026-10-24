"""Approvals the agent asks for while it runs: it posts one, the person answers on the page, the agent reads the answer.

Pending requests are kept in memory only; an agent that is gone leaves requests nobody answers, so every one
expires. An expired or unanswered request counts as a refusal for the agent that polls it.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

TTL_SECONDS = 900.0
MAX_PENDING = 50
MAX_RECENT = 20
ARGS_CHARS = 2000
FIELD_CHARS = 200


class ApprovalError(ValueError):
    """A request that cannot be accepted or answered, with a message fit for the person."""


class ApprovalBoard:
    def __init__(self, ttl: float = TTL_SECONDS, clock=time.time) -> None:
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}

    def _expire(self) -> None:
        now = self._clock()
        for item in self._items.values():
            if item["status"] == "pending" and now - item["created"] > self.ttl:
                item["status"], item["decided"] = "expired", now

    def request(self, run: str, tool: str, arguments: Any, effect: str = "") -> Dict[str, Any]:
        if not isinstance(tool, str) or not tool.strip():
            raise ApprovalError("an approval needs the name of the tool")
        shown = json.dumps(arguments, ensure_ascii=False, default=str) if not isinstance(arguments, str) else arguments
        with self._lock:
            self._expire()
            if sum(1 for i in self._items.values() if i["status"] == "pending") >= MAX_PENDING:
                raise ApprovalError("too many approvals are waiting for an answer")
            item = {"id": secrets.token_hex(6), "run": str(run)[:FIELD_CHARS], "tool": tool.strip()[:FIELD_CHARS],
                    "arguments": shown[:ARGS_CHARS], "effect": str(effect)[:FIELD_CHARS], "status": "pending",
                    "created": self._clock(), "decided": None}
            self._items[item["id"]] = item
            self._trim()
            return dict(item)

    def _trim(self) -> None:
        done = [i for i in self._items.values() if i["status"] != "pending"]
        for old in sorted(done, key=lambda i: i["decided"] or 0)[:-MAX_RECENT]:
            self._items.pop(old["id"], None)

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._expire()
            item = self._items.get(approval_id)
            return dict(item) if item else None

    def decide(self, approval_id: str, approve: bool) -> Dict[str, Any]:
        with self._lock:
            self._expire()
            item = self._items.get(approval_id)
            if item is None:
                raise ApprovalError("That request is not there any more.")
            if item["status"] != "pending":
                raise ApprovalError(f"That request was already answered or has expired ({item['status']}).")
            item["status"], item["decided"] = ("approved" if approve else "denied"), self._clock()
            return dict(item)

    def view(self) -> Dict[str, List[Dict[str, Any]]]:
        with self._lock:
            self._expire()
            items = sorted(self._items.values(), key=lambda i: i["created"])
            return {"pending": [dict(i) for i in items if i["status"] == "pending"],
                    "recent": [dict(i) for i in items if i["status"] != "pending"][-MAX_RECENT:]}

    def refuse_all(self, run: str) -> None:
        """A run that ended leaves nothing waiting for an answer."""
        with self._lock:
            for item in self._items.values():
                if item["status"] == "pending" and item["run"] == run:
                    item["status"], item["decided"] = "expired", self._clock()
