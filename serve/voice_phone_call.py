"""A call the person places from the phone app: `POST /api/voice/phone/call {number, greeting?, lang?}`.

The origin of a call (who started it) is never taken from what a client says. Here it follows from the channel the request came through: only a
request on the listener for phones, with one of the keys, and with the person's switch `telephony.phone_calls` on, is forwarded to the voice
service's route for a call by hand (`POST /call`, origin `user`: no approval, the first sentence still says the caller is an AI, an emergency
number is still refused). A program on this PC, whatever key it holds, cannot use the route and keeps `/external/call`, where it is a
`program`; the `X-Call-Origin` header is not read here and not passed on. The body is cut to three fields: the phone cannot pick the line
or the voice. Nothing about the number is written to the server's log."""
from __future__ import annotations

import json
import logging

from serve import voice_service, voice_settings

log = logging.getLogger(__name__)
PATH = "/api/voice/phone/call"
BODY_MAX = 1 << 12
NUMBER_MAX = 40
GREETING_MAX = 300
LANG_MAX = 12
TIMEOUT_S = 30.0


def _fail(h, status: int, code: str, message: str) -> None:
    h._json(status, {"error": {"type": "voice_phone_call", "code": code, "message": message}})


def clean(req) -> dict | None:
    """The three fields the service gets, or None when the body is not what the phone sends."""
    if not isinstance(req, dict) or set(req) - {"number", "greeting", "lang"}:
        return None
    number, greeting, lang = req.get("number"), req.get("greeting", ""), req.get("lang", "")
    if not isinstance(number, str) or not number.strip() or len(number) > NUMBER_MAX or any(ord(c) < 32 for c in number):
        return None
    if not isinstance(greeting, str) or len(greeting) > GREETING_MAX or any(ord(c) < 32 and c not in "\n\t" for c in greeting):
        return None
    if not isinstance(lang, str) or len(lang) > LANG_MAX or any(ord(c) < 32 for c in lang):
        return None
    out = {"number": number.strip()}
    if greeting.strip():
        out["greeting"] = greeting.strip()
    if lang.strip():
        out["lang"] = lang.strip()
    return out


def post(h, svc) -> None:
    """Answers the request when it came in through the listener for phones with a key and the person allows it; refuses it otherwise."""
    remote = getattr(svc, "remote", None)
    if not (h.remote_listener and h._via_key and remote is not None and remote.listening):
        h._discard_body()
        return _fail(h, 403, "phone_only", "This route is used by the phone app with its key, through the listener for phones. A program places calls "
                                          "with /external/call.")
    if voice_settings.telephony()["phone_calls"] is not True:
        h._discard_body()
        return _fail(h, 403, "phone_calls_off", "The person has not allowed calls from the phone. The switch is in Settings > Voice > Phone calls on the PC.")
    try:
        raw = h._body(BODY_MAX)
        req = clean(json.loads(raw or b"{}"))
    except ValueError:
        req = None
    except Exception as e:                                                      # BadBody: the status and the sentence
        if hasattr(e, "status"):
            return h._json(e.status, {"error": {"message": str(e)}})
        raise
    if req is None:
        return _fail(h, 400, "bad_request", f"Send a JSON object with number (at most {NUMBER_MAX} characters) and, if you want, greeting and lang.")
    try:
        status, data = voice_service.call("POST", "/call", req, timeout=TIMEOUT_S)
    except voice_service.ServiceDown as e:
        return _fail(h, 503, "voice_service_down", str(e))
    log.info("a call was placed from a phone key (%s)", "ok" if status == 200 else f"status {status}")
    h._json(status, data if data is not None else {})
