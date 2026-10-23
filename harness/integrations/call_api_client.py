"""Client for the callAPIcall telephony service (REST control port).

Contract (callAPIcall external API, Bearer token, default port 8770). The caller decides every turn:
  POST /external/call {number, greeting, voice?, lang?, transport?}  -> {"call_id", "status", "transport"}
  POST /external/<id>/say {text}                                      -> {"status": "queued"}
  GET  /external/<id>/heard?after=N&timeout_s=T                       -> {"text" | null, "index"}
  GET  /external/<id>/transcript                                      -> {"transcript": [{"speaker", "text"}]}
  GET  /external/<id>/state                                           -> {"status", ...}
  POST /external/<id>/hangup                                          -> final state

A call reaches a real person and cannot be undone: callers must obtain explicit approval first, the
greeting must disclose that the caller is an AI, and a failed placement is never retried here.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, Dict, Optional

from ._http import ServiceError, request_json

DEFAULT_URL = "http://127.0.0.1:8770"
TRANSPORTS = {"gsm_modem": "gsm", "bluetooth_android": "android"}
AI_DISCLOSURE_MARKERS = ("intelligenza artificiale", "assistente virtuale", "artificial intelligence", "virtual assistant")
_NUMBER = re.compile(r"^\+?[0-9][0-9 ()\-]{5,19}$")
_CALL_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def has_ai_disclosure(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in AI_DISCLOSURE_MARKERS)


def build_greeting(on_behalf_of: str, purpose: str) -> str:
    """First sentence of a call: states that the caller is an AI and why it calls."""
    who = on_behalf_of.strip() or "una persona"
    why = purpose.strip().rstrip(".")
    return f"Buongiorno, sono un assistente virtuale basato su intelligenza artificiale e chiamo per conto di {who}. {why}."


class CallAPICallClient:
    def __init__(
        self,
        endpoint: str = DEFAULT_URL,
        timeout: float = 5.0,
        token: Optional[str] = None,
        place_timeout: float = 20.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.place_timeout = place_timeout
        self._token = token

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "CallAPICallClient":
        return cls(
            endpoint=os.environ.get("CALLAPICALL_URL", DEFAULT_URL),
            token=os.environ.get("CALLAPICALL_TOKEN") or None,
            **kwargs,
        )

    def _call(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        return request_json(method, self.endpoint + path, payload, headers, timeout or self.timeout)

    @staticmethod
    def _check_id(call_id: str) -> str:
        if not isinstance(call_id, str) or not _CALL_ID.match(call_id):
            raise ValueError(f"invalid call id: {call_id!r}")
        return urllib.parse.quote(call_id)

    def is_available(self) -> bool:
        """True when something answers on the control port (an error status still means it is up)."""
        try:
            self._call("GET", "/external/_probe/state")
        except ServiceError as e:
            return e.kind in ("not_found", "unauthorized", "http_error", "bad_response")
        return True

    def place_call(
        self,
        number: str,
        greeting: str,
        voice: Optional[str] = None,
        lang: Optional[str] = None,
        transport: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Places one call. Raises ValueError for a bad number or a greeting without AI disclosure."""
        if not isinstance(number, str) or not _NUMBER.match(number.strip()):
            raise ValueError("invalid phone number")
        if not has_ai_disclosure(greeting):
            raise ValueError("the greeting must state that the caller is an AI")
        payload: Dict[str, Any] = {"number": number.strip(), "greeting": greeting}
        for key, value in (("voice", voice), ("lang", lang), ("transport", transport)):
            if value:
                payload[key] = value
        try:
            return self._call("POST", "/external/call", payload, timeout=self.place_timeout)
        except ServiceError as e:
            if e.kind == "timeout":
                # The service may have started dialling: never retry blindly.
                return {"status": "unknown", "error": "no answer in time; check the call state before retrying",
                        "phone_number": number}
            return {"status": "failed", "error": f"callAPIcall service unavailable: {e.kind}", "phone_number": number}

    def make_phone_call(self, phone_number: str, goal: str, persona_prompt: str = "", mode: str = "gsm_modem") -> Dict[str, Any]:
        """Places a call on behalf of `persona_prompt` for `goal`; conversation turns follow via say/heard."""
        if mode not in TRANSPORTS:
            raise ValueError(f"mode must be one of {sorted(TRANSPORTS)}")
        return self.place_call(phone_number, build_greeting(persona_prompt, goal), transport=TRANSPORTS[mode])

    def say(self, call_id: str, text: str) -> bool:
        try:
            self._call("POST", f"/external/{self._check_id(call_id)}/say", {"text": text})
        except ServiceError:
            return False
        return True

    def heard(self, call_id: str, after: int = -1, timeout_s: float = 15.0) -> Optional[Dict[str, Any]]:
        qs = urllib.parse.urlencode({"after": int(after), "timeout_s": float(timeout_s)})
        try:
            data = self._call("GET", f"/external/{self._check_id(call_id)}/heard?{qs}", timeout=timeout_s + self.timeout)
        except ServiceError:
            return None
        return data if isinstance(data, dict) else None

    def state(self, call_id: str) -> Optional[Dict[str, Any]]:
        try:
            data = self._call("GET", f"/external/{self._check_id(call_id)}/state")
        except ServiceError:
            return None
        return data if isinstance(data, dict) else None

    def get_call_transcript(self, call_id: str) -> Optional[str]:
        try:
            data = self._call("GET", f"/external/{self._check_id(call_id)}/transcript")
        except ServiceError:
            return None
        turns = data.get("transcript") if isinstance(data, dict) else None
        if not isinstance(turns, list):
            return None
        return "\n".join(f"{t.get('speaker', '?')}: {t.get('text', '')}" for t in turns if isinstance(t, dict))

    def hangup(self, call_id: str) -> bool:
        try:
            self._call("POST", f"/external/{self._check_id(call_id)}/hangup", {})
        except ServiceError:
            return False
        return True
