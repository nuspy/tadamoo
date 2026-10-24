"""Asks the person through the server's pages: posts an approval request and waits for the answer.

Whatever goes wrong (the server is gone, the answer never comes, the run is cancelled) is a refusal.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

POLL_SECONDS = 1.0
WAIT_SECONDS = 900.0
FAILURES_BEFORE_REFUSING = 3


class ServerApprover:
    def __init__(self, base_url: str, api_key: Optional[str], run_id: str, cancel: Optional[threading.Event] = None,
                 wait: float = WAIT_SECONDS, poll: float = POLL_SECONDS, effect_of=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.run_id = run_id
        self.cancel = cancel
        self.wait = wait
        self.poll = poll
        self.effect_of = effect_of

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def __call__(self, tool: str, arguments: Dict[str, Any]) -> bool:
        try:
            made = self._call("POST", "/api/agent/approvals", {"run": self.run_id, "tool": tool, "arguments": arguments,
                                                                "effect": self.effect_of(tool) if self.effect_of else ""})
            approval_id = made["id"]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return False
        deadline = time.monotonic() + self.wait
        failures = 0
        while time.monotonic() < deadline:
            if self.cancel is not None and self.cancel.is_set():
                return False
            try:
                status = self._call("GET", f"/api/agent/approvals/{approval_id}").get("status")
                failures = 0
            except (urllib.error.URLError, OSError, ValueError):
                failures += 1
                if failures >= FAILURES_BEFORE_REFUSING:
                    return False
                status = "pending"
            if status == "approved":
                return True
            if status != "pending":
                return False
            time.sleep(self.poll)
        return False
