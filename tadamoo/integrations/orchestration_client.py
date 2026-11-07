"""Client for the server's session routes used by one conversation to reach another: /api/agent/sessions/<ref>/deliver and /status.

The conversation's process never holds the server's shared secret; it proves which conversation it is with the id and token the
manager gave it when it started (TADAMOO_SESSION_ID, TADAMOO_SESSION_TOKEN), sent as one header. The server checks the token and
uses the id as `sender` on the message, so a reply never has to be told who it answers.
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Dict, Optional

from ._http import ServiceError, request_json
from tadamoo import control_secret

SESSION_HEADER = "X-Tadamoo-Session"


class OrchestrationClient:
    def __init__(self, endpoint: str, api_key: Optional[str] = None, session_id: str = "", session_token: str = "", timeout: float = 10.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.session_id = session_id or os.environ.get("TADAMOO_SESSION_ID", "")
        self.session_token = session_token or os.environ.get("TADAMOO_SESSION_TOKEN", "")
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        headers.update(control_secret.header_for(self.endpoint))
        if self.session_id and self.session_token:
            headers[SESSION_HEADER] = f"{self.session_id}.{self.session_token}"
        return request_json(method, self.endpoint + path, payload, headers, self.timeout)

    @staticmethod
    def _ref(ref: str) -> str:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("name the conversation")
        return urllib.parse.quote(ref.strip())

    def deliver(self, ref: str, message: str) -> Dict[str, Any]:
        """Sends `message` to the conversation `ref` names; `{"delivery": {...}}` with its state (pending, delivered or dead)."""
        return self._call("POST", f"/api/agent/sessions/{self._ref(ref)}/deliver", {"message": message})

    def status(self, ref: str) -> Dict[str, Any]:
        return self._call("GET", f"/api/agent/sessions/{self._ref(ref)}/status")

    def create(self, workspace: str, title: str = "", alias: str = "", unattended: bool = False, rules: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"workspace": workspace, "title": title, "parent": self.session_id}
        if alias:
            body["alias"] = alias
        if unattended:
            body["unattended"] = True
        if rules:
            body["rules"] = rules
        return self._call("POST", "/api/agent/sessions", body)

    def send(self, sid: str, text: str) -> Dict[str, Any]:
        return self._call("POST", f"/api/agent/sessions/{self._ref(sid)}/send", {"text": text})


__all__ = ["OrchestrationClient", "ServiceError", "SESSION_HEADER"]
