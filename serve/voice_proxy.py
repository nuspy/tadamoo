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
TIMEOUTS = {"/meetbot/join": 60.0, "/brain/say": 40.0, "/session/start": 60.0, "/avatar/speak": 120.0}      # a sentence is synthesised and played; a session loads what it needs
WIZARD_TIMEOUT_S = 120.0                                    # making an avatar: the face is found, the clip and the voice sample are copied
AVATAR_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}"
DRAFT = r"[0-9a-f]{32}"
LANG = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")
TRAIN_ENGINES = ("ultralight", "feathertalk", "er_nerf")


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


def _train_plan(body: Any) -> dict:
    body = _fields(body, ("avatar", "engine", "epochs", "asr", "fresh"), ("avatar", "engine"))
    epochs = body.get("epochs")
    if (not isinstance(body["avatar"], str) or not re.fullmatch(AVATAR_NAME, body["avatar"]) or body["engine"] not in TRAIN_ENGINES
            or (epochs is not None and not (isinstance(epochs, int) and not isinstance(epochs, bool) and 1 <= epochs <= 400))
            or body.get("asr", "feather_hubert") not in ("wenet", "hubert", "feather_hubert") or not isinstance(body.get("fresh", False), bool)):
        raise Refused(400, "bad_request", "Send the avatar's name, an engine (ultralight, feathertalk or er_nerf) and, if you want them, epochs (1 to 400), asr and fresh.")
    return body


def _train_setup(body: Any) -> dict:
    body = _fields(body, ("engine", "asr", "avatar", "probe"), ("engine",))
    if (body["engine"] not in TRAIN_ENGINES or body.get("asr", "feather_hubert") not in ("wenet", "hubert", "feather_hubert") or not isinstance(body.get("probe", False), bool)
            or (body.get("avatar") is not None and not (isinstance(body["avatar"], str) and re.fullmatch(AVATAR_NAME, body["avatar"])))):
        raise Refused(400, "bad_request", "Send an engine (ultralight or feathertalk) and, if you want them, asr, avatar and probe.")
    return body


def _train_python(body: Any) -> dict:
    body = _fields(body, ("path",), ("path",))
    if not isinstance(body["path"], str) or len(body["path"]) > 1000 or any(ord(c) < 32 for c in body["path"]):
        raise Refused(400, "bad_request", "Send the full path of the Python, or an empty text to forget it.")
    return body


def _train_export(body: Any) -> dict:
    body = _fields(body, ("avatar", "engine"), ("avatar", "engine"))
    if not isinstance(body["avatar"], str) or not re.fullmatch(AVATAR_NAME, body["avatar"]) or body["engine"] not in TRAIN_ENGINES:
        raise Refused(400, "bad_request", "Send the avatar's name and an engine (ultralight or feathertalk).")
    return body


def _train_start(body: Any) -> dict:
    body = _fields(body, ("token", "confirm"), ("token", "confirm"))
    if not isinstance(body["token"], str) or not re.fullmatch(r"[0-9a-f]{64}", body["token"]) or not isinstance(body["confirm"], bool):
        raise Refused(400, "bad_request", "Send the token of the plan and confirm: true.")
    return body


def _python_path(body: Any) -> dict:
    body = _fields(body, ("path",), ("path",))
    if not isinstance(body["path"], str) or len(body["path"]) > 520 or "\0" in body["path"]:
        raise Refused(400, "bad_request", "Send the path of the Python, or an empty text to forget it.")
    return body


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


MEETING_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
PLAN_TEXT = {"title": 120, "url": 2000, "start": 40, "role": 10, "persona": 61, "consent_mode": 10, "notes": 500}
PLAN_BOOLS = ("participate", "camera", "speak")
EVENT_ID = r"[A-Za-z0-9][A-Za-z0-9:_=.+-]{0,199}"


def _meet_join(body: Any) -> dict:
    body = _fields(body, ("url", "rights_confirmed", "title", "speak", "consent_mode", "consent_window_s"), ("url", "rights_confirmed"))
    url = body["url"]
    if not isinstance(url, str) or not url.startswith(("http://", "https://")) or len(url) > 2000 or body["rights_confirmed"] is not True:
        raise Refused(400, "bad_request", "Send the meeting's address and rights_confirmed: true.")
    if "title" in body and not (isinstance(body["title"], str) and len(body["title"]) <= 120):
        raise Refused(400, "bad_request", "The title is a text of at most 120 characters.")
    if "speak" in body and not isinstance(body["speak"], bool):
        raise Refused(400, "bad_request", "speak is true or false.")
    if "consent_mode" in body and body["consent_mode"] not in ("window", "explicit"):
        raise Refused(400, "bad_request", "consent_mode is window or explicit.")
    w = body.get("consent_window_s", 45)
    if not (isinstance(w, int) and not isinstance(w, bool) and 20 <= w <= 300):
        raise Refused(400, "bad_request", "consent_window_s is a whole number from 20 to 300.")
    return body


def _plan_item(required: tuple) -> Callable[[Any], dict]:
    """A meeting of the plan from the page: the fields are the service's (voice/callbridge/meetbot/plan_store.py checks them in full)."""
    def check(body: Any) -> dict:
        body = _fields(body, (*PLAN_TEXT, *PLAN_BOOLS, "duration_min"), required)
        for key, limit in PLAN_TEXT.items():
            if key in body and not (isinstance(body[key], str) and len(body[key]) <= limit):
                raise Refused(400, "bad_request", f"{key} is a text of at most {limit} characters.")
        if any(k in body and not isinstance(body[k], bool) for k in PLAN_BOOLS):
            raise Refused(400, "bad_request", "participate, camera and speak are true or false.")
        minutes = body.get("duration_min")
        if "duration_min" in body and minutes is not None and not (isinstance(minutes, int) and not isinstance(minutes, bool)):
            raise Refused(400, "bad_request", "duration_min is a whole number of minutes, or empty.")
        return body
    return check


def _join_now(body: Any) -> dict:
    body = _fields(body, ("rights_confirmed",), ("rights_confirmed",))
    if body["rights_confirmed"] is not True:
        raise Refused(400, "bad_request", "Send rights_confirmed: true.")
    return body


def _autojoin(body: Any) -> dict:
    body = _fields(body, ("enabled",), ("enabled",))
    if not isinstance(body["enabled"], bool):
        raise Refused(400, "bad_request", "enabled is true or false.")
    return body


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
    # the avatar (the files of the wizard, the training video upload and the preview stream pass through serve/voice_avatar.py, not here)
    ("GET", "/avatar/state"): _none,
    ("GET", "/avatar/list"): _none,
    ("GET", "/avatar/output"): _none,
    ("GET", "/avatar/enhance"): _none,
    ("GET", "/avatar/engines"): _none,
    ("GET", "/avatar/prepare"): _none,
    ("POST", "/avatar/prepare"): _avatar_prepare,
    ("POST", "/avatar/prepare/cancel"): _none,
    ("POST", "/avatar/train/plan"): _train_plan,
    ("POST", "/avatar/train/start"): _train_start,
    ("POST", "/avatar/train/setup"): _train_setup,
    ("POST", "/avatar/train/python"): _train_python,
    ("POST", "/avatar/train/export"): _train_export,
    ("GET", "/avatar/train/status"): _none,
    ("POST", "/avatar/train/cancel"): _none,
    ("POST", "/avatar/engines/python"): _python_path,
    ("POST", "/avatar/speak"): _avatar_speak,
    ("POST", "/avatar/start"): _avatar_start,
    ("POST", "/avatar/wizard/start"): _none,
    # meetings, the copilot and the calendar (the rules of consent are the service's: a join must say that the right to bring the assistant is confirmed)
    ("GET", "/meetbot/status"): _none,
    ("POST", "/meetbot/join"): _meet_join,
    ("POST", "/meetbot/leave"): _none,
    ("GET", "/meetings/list"): _none,
    ("GET", "/meetings/plan"): _none,
    ("POST", "/meetings/plan"): _plan_item(("url", "start")),
    ("GET", "/copilot/state"): _none,
    ("POST", "/copilot/toggle"): _none,
    ("GET", "/calendar/status"): _none,
    ("GET", "/calendar/events"): _none,
}
#: (method, pattern of the service route) for the routes that carry an id: the person's answer to a call a program asked for
PATTERNS: list[tuple[str, "re.Pattern[str]", Callable[[Any], Any]]] = [
    ("POST", re.compile(r"/call/approvals/[0-9a-f]{16}/(?:approve|deny)"), _none),
    ("GET", re.compile(r"/avatar/engines/(?:wav2lip_v288|sadtalker|er_nerf)/setup"), _none),
    ("POST", re.compile(r"/avatar/engines/(?:wav2lip_v288|sadtalker|er_nerf)/check"), _none),
    ("GET", re.compile(rf"/avatar/wizard/{DRAFT}"), _none),
    ("DELETE", re.compile(rf"/avatar/wizard/{DRAFT}"), _none),
    ("POST", re.compile(rf"/avatar/wizard/{DRAFT}/elabora"), _avatar_finish),
    ("DELETE", re.compile(rf"/avatar/{AVATAR_NAME}"), _none),
    ("GET", re.compile(rf"/avatar/{AVATAR_NAME}/training-video"), _none),
    ("DELETE", re.compile(rf"/avatar/{AVATAR_NAME}/training-video"), _none),
    ("POST", re.compile(rf"/personas/{AVATAR_NAME}/wav2lip/bocca"), _avatar_mouth),
    ("PUT", re.compile(rf"/meetings/plan/{MEETING_ID}"), _plan_item(())),
    ("DELETE", re.compile(rf"/meetings/plan/{MEETING_ID}"), _none),
    ("POST", re.compile(rf"/meetings/plan/{MEETING_ID}/join-now"), _join_now),
    ("GET", re.compile(rf"/meetings/{MEETING_ID}(?:/transcript)?"), _none),
    ("DELETE", re.compile(rf"/meetings/{MEETING_ID}"), _none),
    ("POST", re.compile(r"/calendar/(?:google|microsoft)/(?:connect|disconnect)"), _none),
    ("POST", re.compile(rf"/calendar/events/{EVENT_ID}/autojoin"), _autojoin),
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
        slow = path.endswith("/join-now") and TIMEOUTS["/meetbot/join"]
        status, data = voice_service.call(method, path, body, timeout=TIMEOUTS.get(path, slow or (WIZARD_TIMEOUT_S if path.endswith("/elabora") else voice_service.TIMEOUT)))
    except voice_service.ServiceDown as e:
        raise Refused(503, "voice_service_down", str(e)) from None
    return status, data if data is not None else {}
