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

The same port also analyses recordings, which is how the Chat reads audio and video attachments:
  POST   /analysis/start       multipart file (+ title, speakers) -> 202 {"id", "state"}; 409 while one runs;
                               400 for an extension it does not take; 501 without ffmpeg
  GET    /analysis/<id>        -> {"id", "state", "progress": {"phase", "percent"}, "error", "analysis": {...}}
  GET    /analysis/<id>/transcript -> {"segments": [{"start_s", "end_s", "text", "speaker"?}]}
  DELETE /analysis/<id>
It takes the sound of a video, not its pictures, and runs one job at a time.
"""
from __future__ import annotations

import os
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from ._http import ServiceError, request_json, request_multipart

DEFAULT_URL = "http://127.0.0.1:8770"
TRANSPORTS = {"gsm_modem": "gsm", "bluetooth_android": "android"}
AI_DISCLOSURE_MARKERS = ("intelligenza artificiale", "assistente virtuale", "artificial intelligence", "virtual assistant")
_NUMBER = re.compile(r"^\+?[0-9][0-9 ()\-]{5,19}$")
_CALL_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
VIDEO_EXT = frozenset({".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts"})
AUDIO_EXT = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".wma"})
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT


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
        upload_timeout: float = 900.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.place_timeout = place_timeout
        self.upload_timeout = upload_timeout
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

    # ---- recordings: the Chat's audio and video attachments ------------------------------------------------

    def start_media_analysis(self, path: str, title: Optional[str] = None, speakers: int = 0,
                             max_bytes: Optional[int] = None) -> Dict[str, Any]:
        """Sends one audio or video file to be transcribed. Raises ValueError for a file the service would refuse
        anyway; otherwise returns {"status": "queued", "id"}, {"status": "busy"} while another job runs (try again
        later) or {"status": "failed", "error"}. The file is only read, never changed."""
        if not isinstance(path, str) or not os.path.isfile(path):
            raise ValueError(f"not a file: {path!r}")
        ext = os.path.splitext(path)[1].lower()
        if ext not in MEDIA_EXT:
            raise ValueError(f"not an audio or video file the service takes: {ext or 'no extension'}")
        size = os.path.getsize(path)
        if max_bytes is not None and size > max_bytes:
            raise ValueError(f"the file is {size} bytes, over the limit of {max_bytes}")
        fields = {"speakers": str(max(0, int(speakers)))}
        if title:
            fields["title"] = title
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            res = request_multipart(self.endpoint + "/analysis/start", fields, "file", path, os.path.basename(path),
                                    headers, self.upload_timeout)
        except ServiceError as e:
            if e.status == 409:
                return {"status": "busy", "error": "another recording is being analysed"}
            if e.kind == "timeout":
                # The upload may have been accepted: look at the analyses before sending it again.
                return {"status": "unknown", "error": "no answer in time; check the analyses before sending it again"}
            why = e.detail or e.kind
            return {"status": "failed", "error": f"callAPIcall could not take the file: {why}"}
        if not isinstance(res, dict) or not isinstance(res.get("id"), str) or not _CALL_ID.match(res["id"]):
            return {"status": "failed", "error": "callAPIcall answered without an analysis id"}
        return {"status": "queued", "id": res["id"]}

    def media_analysis(self, job_id: str) -> Optional[Dict[str, Any]]:
        """The job's state: {"state": queued|extracting|transcribing|diarizing|analyzing|done|error, "progress", ...}."""
        try:
            data = self._call("GET", f"/analysis/{self._check_id(job_id)}")
        except ServiceError:
            return None
        return data if isinstance(data, dict) else None

    def media_transcript(self, job_id: str) -> Optional[List[Dict[str, Any]]]:
        """The transcript's segments [{"start_s", "end_s", "text", "speaker"?}], None until there is one."""
        try:
            data = self._call("GET", f"/analysis/{self._check_id(job_id)}/transcript", timeout=30.0)
        except ServiceError:
            return None
        segments = data.get("segments") if isinstance(data, dict) else None
        return [x for x in segments if isinstance(x, dict)] if isinstance(segments, list) else None

    def wait_media_analysis(self, job_id: str, timeout_s: float = 3600.0, poll_s: float = 2.0,
                            sleep: Callable[[float], None] = time.sleep,
                            clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
        """Polls until the job is done or failed: the last state, or {"state": "timeout"} / {"state": "unavailable"}."""
        end = clock() + timeout_s
        misses = 0
        while True:
            state = self.media_analysis(job_id)
            if state is None:
                misses += 1                  # a few lost answers are not the end of the job; a dead service is
                if misses >= 5:
                    return {"state": "unavailable", "id": job_id}
            else:
                misses = 0
                if state.get("state") in ("done", "error"):
                    return state
            if clock() >= end:
                return {"state": "timeout", "id": job_id, **({"progress": state.get("progress")} if state else {})}
            sleep(poll_s)

    def delete_media_analysis(self, job_id: str) -> bool:
        try:
            self._call("DELETE", f"/analysis/{self._check_id(job_id)}")
        except ServiceError:
            return False
        return True


def format_transcript(segments: List[Dict[str, Any]]) -> str:
    """Segments as lines the model can read: "[mm:ss] Speaker 1: text" (the speaker only when the service named one)."""
    lines = []
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        try:
            t = max(0, int(float(seg.get("start_s", 0))))
            stamp = f"[{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}] " if t >= 3600 else f"[{t // 60:02d}:{t % 60:02d}] "
        except (TypeError, ValueError):
            stamp = ""
        who = str(seg.get("speaker") or "").strip()
        lines.append(f"{stamp}{who + ': ' if who else ''}{text}")
    return "\n".join(lines)
