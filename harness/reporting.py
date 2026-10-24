"""Tells the server's Monitor what the agent is doing and what it edited.

The agent posts to the server it already talks to, to /api/tasks (one task per run, with its progress) and
/api/changes (each file edit with its diff). Reporting is a courtesy: it never changes a run, and after two
failures in a row it stops trying so an unreachable server cannot slow the agent down.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

RESULT_CHARS = 300
FAILURES_BEFORE_GIVING_UP = 2
FILE_TOOLS = ("write_file", "edit_file")
STATUS_BY_STOP = {"final": "done", "cancelled": "closed"}


class PanelReporter:
    def __init__(self, base_url: str, api_key: Optional[str], sandbox: Any, goal: str, timeout: float = 3.0,
                 session: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.sandbox = sandbox
        self.goal = " ".join(goal.split())[:RESULT_CHARS]
        self.timeout = timeout
        self.task_id = f"agent-{session or secrets.token_hex(3)}"
        self.error = ""
        self._failures = 0
        self._sent_changes = len(getattr(sandbox, "changes", []))
        self._turn = 0

    @property
    def active(self) -> bool:
        return self._failures < FAILURES_BEFORE_GIVING_UP

    def _post(self, path: str, body: Dict[str, Any]) -> bool:
        if not self.active:
            return False
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            self._failures = 0
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._failures += 1
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def _task(self, status: str, result: str) -> None:
        self._post("/api/tasks", {"id": self.task_id, "status": status, "result": result[:RESULT_CHARS],
                                  "metadata": {"kind": "agent", "goal": self.goal}})

    def start(self) -> None:
        self._task("running", f"{self.goal}")

    def on_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "tool_start":
            self._turn += 1
            self._task("running", f"{self.goal} · step {self._turn}: {event.get('name')}")
        elif kind == "tool_end" and event.get("name") in FILE_TOOLS and event.get("status") == "completed":
            self._send_new_changes()

    def _send_new_changes(self) -> None:
        changes = getattr(self.sandbox, "changes", [])
        fresh = changes[self._sent_changes:]
        if not fresh:
            return
        edits = []
        for c in fresh:
            edit = {k: c[k] for k in ("file", "kind", "added", "removed", "diff") if k in c}
            if isinstance(c.get("old_text"), str) and isinstance(c.get("new_text"), str):
                # the texts let the person keep or undo single blocks from the Monitor
                edit["review"] = {"root": str(getattr(self.sandbox, "root", "")), "old_text": c["old_text"], "new_text": c["new_text"]}
            edits.append(edit)
        if self._post("/api/changes", {"edits": edits}):
            self._sent_changes = len(changes)

    def finish(self, stop_reason: str, final_output: str = "", error: str = "") -> None:
        self._send_new_changes()
        status = STATUS_BY_STOP.get(stop_reason, "failed")
        text = final_output if stop_reason == "final" else (error or f"stopped: {stop_reason}")
        self._task(status, text or self.goal)
