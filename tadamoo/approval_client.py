"""Asks the person through the server's pages: posts an approval request and waits for the answer.

The answer is waited for as long as it takes: a server that does not answer for a while is tried again, and only the run's own
cancellation ends the wait (as a refusal).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from tadamoo import control_secret

POLL_SECONDS = 1.0


class ServerApprover:
    def __init__(self, base_url: str, api_key: Optional[str], run_id: str, cancel: Optional[threading.Event] = None,
                 poll: float = POLL_SECONDS, effect_of=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.run_id = run_id
        self.cancel = cancel
        self.poll = poll
        self.effect_of = effect_of

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(control_secret.header_for(self.base_url))
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def available(self) -> bool:
        """Whether the server answers now, so a question put to its board will be seen (a run that has no one at a terminal)."""
        try:
            self._call("GET", "/v1/status")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def __call__(self, tool: str, arguments: Dict[str, Any]) -> bool:
        approval_id = None
        while approval_id is None:
            if self.cancel is not None and self.cancel.is_set():
                return False
            try:
                made = self._call("POST", "/api/agent/approvals", {"run": self.run_id, "tool": tool, "arguments": arguments,
                                                                    "effect": self.effect_of(tool) if self.effect_of else ""})
                approval_id = made["id"]
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                time.sleep(self.poll)
        while True:
            if self.cancel is not None and self.cancel.is_set():
                return False
            try:
                status = self._call("GET", f"/api/agent/approvals/{approval_id}").get("status")
            except (urllib.error.URLError, OSError, ValueError):
                status = "pending"
            if status == "approved":
                return True
            if status != "pending":
                return False
            time.sleep(self.poll)
