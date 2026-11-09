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
TIMEOUTS = {"/brain/say": 40.0, "/session/start": 60.0, "/avatar/speak": 120.0}      # a sentence is synthesised and played; a session loads what it needs
WIZARD_TIMEOUT_S = 120.0                                    # making an avatar: the face is found, the clip and the voice sample are copied
AVATAR_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}"
DRAFT = r"[0-9a-f]{32}"
LANG = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")


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


def _fields(body: Any, allowed: tuple, required: tuple = ()) -> dict:
    if not isinstance(body, dict) or set(body) - set(allowed) or any(k not in body for k in required):
        raise Refused(400, "bad_request", "Send " + ", ".join(allowed) + ".")
    return body


def _avatar_speak(body: Any) -> dict:
    body = _fields(body, ("text", "lang"), ("text",))
    text, lang = body["text"], body.get("lang", "")
    if not isinstance(text, str) or not text.strip() or len(text) > SAY_MAX or not isinstance(lang, str) or (lang and not LANG.fullmatch(lang)):
        raise Refused(400, "bad_request", f"Send a sentence of at most {SAY_MAX} characters, and a language code if you want one.")
    return {"text": text.strip(), **({"lang": lang} if lang else {})}


def _avatar_start(body: Any) -> dict:
    if body in (None, {}):
        return {}
    body = _fields(body, ("persona",), ("persona",))
    if not isinstance(body["persona"], str) or not re.fullmatch(AVATAR_NAME, body["persona"]):
        raise Refused(400, "bad_request", "Send the avatar's name, or nothing for the personality's own.")
    return {"persona": body["persona"]}


def _avatar_prepare(body: Any) -> dict:
    body = _fields(body, ("avatar", "clip"), ("avatar",))
    name, clip = body["avatar"], body.get("clip")
    if not isinstance(name, str) or not re.fullmatch(AVATAR_NAME, name) or (clip is not None and not (isinstance(clip, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_ -]{0,79}\.mp4", clip))):
        raise Refused(400, "bad_request", "Send the avatar's name and, if it has several, the clip's file name.")
    return {"avatar": name, **({"clip": clip} if clip else {})}


def _avatar_finish(body: Any) -> dict:
    body = _fields(body, ("name", "language"), ("name",))
    name, lang = body["name"], body.get("language", "it")
    if not isinstance(name, str) or not name.strip() or len(name) > 40 or any(ord(c) < 32 for c in name) or not isinstance(lang, str) or not LANG.fullmatch(lang):
        raise Refused(400, "bad_request", "Send a name of 1 to 40 characters and a language code.")
    return {"name": name.strip(), "language": lang}


def _avatar_mouth(body: Any) -> dict:
    body = _fields(body, ("p1", "p2"), ("p1", "p2"))
    for p in (body["p1"], body["p2"]):
        if not (isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 20000 for v in p)):
            raise Refused(400, "bad_request", "Send p1 and p2 as [x, y] in pixels of the portrait.")
    return {"p1": body["p1"], "p2": body["p2"]}


#: (method, service route) -> the function that checks and cleans the body
ROUTES: dict[tuple[str, str], Callable[[Any], Any]] = {
    ("GET", "/status"): _none,
    ("GET", "/functions"): _none,
    ("GET", "/brain/state"): _none,
    ("GET", "/gateway"): _none,
    ("GET", "/session/state"): _none,
    ("GET", "/diag/brain"): _none,
    ("POST", "/session/start"): _start,
    ("POST", "/session/stop"): _none,
    ("POST", "/session/new"): _none,
    ("POST", "/brain/say"): _say,
    ("GET", "/call/approvals"): _none,
    ("GET", "/call/audit"): _none,
    # the avatar (the files of the wizard and the preview stream pass through serve/voice_avatar.py, not here)
    ("GET", "/avatar/state"): _none,
    ("GET", "/avatar/list"): _none,
    ("GET", "/avatar/output"): _none,
    ("GET", "/avatar/enhance"): _none,
    ("GET", "/avatar/prepare"): _none,
    ("POST", "/avatar/prepare"): _avatar_prepare,
    ("POST", "/avatar/prepare/cancel"): _none,
    ("POST", "/avatar/speak"): _avatar_speak,
    ("POST", "/avatar/start"): _avatar_start,
    ("POST", "/avatar/wizard/start"): _none,
}
#: (method, pattern of the service route) for the routes that carry an id: the person's answer to a call a program asked for
PATTERNS: list[tuple[str, "re.Pattern[str]", Callable[[Any], Any]]] = [
    ("POST", re.compile(r"/call/approvals/[0-9a-f]{16}/(?:approve|deny)"), _none),
    ("GET", re.compile(rf"/avatar/wizard/{DRAFT}"), _none),
    ("DELETE", re.compile(rf"/avatar/wizard/{DRAFT}"), _none),
    ("POST", re.compile(rf"/avatar/wizard/{DRAFT}/elabora"), _avatar_finish),
    ("DELETE", re.compile(rf"/avatar/{AVATAR_NAME}"), _none),
    ("POST", re.compile(rf"/personas/{AVATAR_NAME}/wav2lip/bocca"), _avatar_mouth),
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
        status, data = voice_service.call(method, path, body, timeout=TIMEOUTS.get(path, WIZARD_TIMEOUT_S if path.endswith("/elabora") else voice_service.TIMEOUT))
    except voice_service.ServiceDown as e:
        raise Refused(503, "voice_service_down", str(e)) from None
    return status, data if data is not None else {}
