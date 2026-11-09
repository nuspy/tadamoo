"""What the Voice tab may ask of the voice service, through the server: a short list of routes, with their bodies checked, and nothing else.

The browser never holds the service's token and never learns its port: `POST /api/voice/service/call {method, path, body}` is answered here by
one request to the service on loopback. A route that is not in ROUTES is refused before anything is sent."""
from __future__ import annotations

import re
from typing import Any, Callable

from serve import voice_service

HOLD_MODES = ("half", "full")
BRAINS = ("harness", "direct", "")
SAY_MAX = 300
TIMEOUTS = {"/brain/say": 40.0, "/session/start": 60.0}      # a sentence is synthesised and played; a session loads what it needs


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _none(body: Any) -> None:
    if body not in (None, {}):
        raise Refused(400, "bad_request", "This route takes no body.")
    return None


def _start(body: Any) -> dict:
    body = body if isinstance(body, dict) else {}
    if set(body) - {"mode", "brain"}:
        raise Refused(400, "bad_request", "Send mode, and brain if you want one.")
    mode, brain = body.get("mode", "half"), body.get("brain", "")
    if mode not in HOLD_MODES or brain not in BRAINS:
        raise Refused(400, "bad_request", "mode is half or full; brain is harness, direct or empty.")
    return {"mode": mode, "brain": brain, "avatar": False}


def _say(body: Any) -> dict:
    text = body.get("text") if isinstance(body, dict) and set(body) == {"text"} else None
    if not isinstance(text, str) or not text.strip() or len(text) > SAY_MAX:
        raise Refused(400, "bad_request", f"Send a sentence of at most {SAY_MAX} characters.")
    return {"text": text.strip()}


#: (method, service route) -> the function that checks and cleans the body
ROUTES: dict[tuple[str, str], Callable[[Any], Any]] = {
    ("GET", "/status"): _none,
    ("GET", "/functions"): _none,
    ("GET", "/brain/state"): _none,
    ("GET", "/session/state"): _none,
    ("GET", "/diag/brain"): _none,
    ("POST", "/session/start"): _start,
    ("POST", "/session/stop"): _none,
    ("POST", "/session/new"): _none,
    ("POST", "/brain/say"): _say,
    ("GET", "/call/approvals"): _none,
    ("GET", "/call/audit"): _none,
}
#: (method, pattern of the service route) for the routes that carry an id: the person's answer to a call a program asked for
PATTERNS: list[tuple[str, "re.Pattern[str]", Callable[[Any], Any]]] = [
    ("POST", re.compile(r"/call/approvals/[0-9a-f]{16}/(?:approve|deny)"), _none),
]


def call(req: Any) -> tuple[int, dict]:
    """(HTTP status, JSON body) for one request of the page."""
    if not isinstance(req, dict) or set(req) - {"method", "path", "body"}:
        raise Refused(400, "bad_request", "Send {method, path, body}.")
    method, path = req.get("method"), req.get("path")
    check = ROUTES.get((method, path)) if isinstance(method, str) and isinstance(path, str) else None
    if check is None and isinstance(method, str) and isinstance(path, str):
        check = next((c for m, pattern, c in PATTERNS if m == method and pattern.fullmatch(path)), None)
    if check is None:
        raise Refused(403, "not_allowed", "The voice tab may not call that route of the voice service.")
    body = check(req.get("body"))
    try:
        status, data = voice_service.call(method, path, body, timeout=TIMEOUTS.get(path, voice_service.TIMEOUT))
    except voice_service.ServiceDown as e:
        raise Refused(503, "voice_service_down", str(e)) from None
    return status, data if data is not None else {}
