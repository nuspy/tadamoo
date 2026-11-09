"""callAPIcall's routes, republished on TaDaMoo's own port: the same paths, answered through the voice service.

A table (ROUTES) says, for each path, its methods, how big a body may be, how long to wait for the service, whether the body is a file
that passes through unread, and which kind of caller may use it:

    api     a program with the server's API key (or the control secret) as a Bearer token, or any program on this PC when no key is set
    local   the same, but only from this PC

The service's token is added here and never leaves the server: a response is searched for it before it is sent. A route whose service
handler is not there yet answers 501 `not_available_yet` with the reason. Nothing is under /v1, which stays the interface of the loaded
model. The listener for phones does not offer any of these."""
from __future__ import annotations

import http.client
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from serve import personas_compat, settings, voice_service, voice_settings
from serve.security import key_matches

JSON_MAX = 1 << 16
UPLOAD_MAX = 2 << 30
QUERY_MAX = 2048
CHUNK = 1 << 20
BUFFERED_MAX = 64 << 20                    # a JSON or text answer is read whole (to look for the token); anything else streams
WAIT_CAP_S = 120.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_CALL = r"[A-Za-z0-9_-]{1,64}"
_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}"
_PERSONA = r"[^/\s]{1,120}"                 # a slug or a name as a path segment (percent-encoded spaces allowed)
PERSONA_BODY = 1 << 17

SOON = {
    "choice": "The voice and the engine of the speech are chosen in Settings > Voice.",
    "load": "Loading and unloading a voice function plans the VRAM and may unload and reload the language model, so it needs the person's confirmation in the Voice functions dialog in the Loader; there is no one-call form here.",
    "stream": "The voice service has no streaming speech route behind its token: only the old Handy server (off by default, no token) speaks WebSocket, and this port does not relay it.",
    "personas": "A personality's memory, clips, poster and avatar files are not offered here: the memory is the person's (Settings > Personalities) and the avatar files come with the avatar phase.",
}


@dataclass(frozen=True)
class Route:
    method: str                            # "" for any method (a route that is not there yet)
    pattern: re.Pattern
    kind: str = "api"                      # api | local
    upload: bool = False                   # the body is a file, copied through in pieces (needs a Content-Length)
    max_body: int = JSON_MAX
    timeout: float = 30.0
    wait: str = ""                         # a query value (seconds) the service waits that long for: the timeout grows with it
    soon: str = ""                         # not there yet: 501 with this reason
    here: bool = False                     # answered by this server (serve/personas_compat.py), not passed to the voice service
    gate: str = ""                         # "programs_join": a program may use it only when Settings > Voice > Meetings lets programs start the bot


PROGRAMS_JOIN = "programs_join"


def _r(method: str, path: str, **kw) -> Route:
    return Route(method, re.compile(path), **kw)


ROUTES: tuple[Route, ...] = (
    # a call driven by a program: it can ring a phone, so this PC only
    _r("POST", r"/external/call", kind="local"),
    _r("POST", rf"/external/{_CALL}/say", kind="local"),
    _r("GET", rf"/external/{_CALL}/heard", kind="local", wait="timeout_s", timeout=30.0),
    _r("GET", rf"/external/{_CALL}/(?:transcript|state)", kind="local"),
    _r("POST", rf"/external/{_CALL}/hangup", kind="local"),
    # the analysis of a recording
    _r("POST", r"/analysis/start", upload=True, max_body=UPLOAD_MAX, timeout=600.0),
    _r("GET", r"/analysis/list"),
    _r("GET", rf"/analysis/{_ID}(?:/transcript)?"),
    _r("POST", rf"/analysis/{_ID}/speakers"),
    _r("DELETE", rf"/analysis/{_ID}"),
    # speech to text and text to speech
    _r("POST", r"/transcribe", upload=True, max_body=512 << 20, timeout=900.0),
    _r("POST", r"/tts/test", timeout=180.0),
    _r("GET", r"/tts/(?:models|voices)"),
    _r("GET", r"/voices/cloned"),
    _r("POST", r"/voices/clone", kind="local", max_body=64 << 20, timeout=60.0),
    _r("DELETE", rf"/voices/cloned/{_NAME}", kind="local"),
    _r("GET", r"/models/(?:status|available)"),
    _r("", r"/tts/(?:model|voice)", kind="local", soon=SOON["choice"]),
    _r("", r"/models/(?:load|unload)", kind="local", soon=SOON["load"]),
    _r("", r"/(?:tts|stt)/stream", soon=SOON["stream"]),
    # the voice session
    _r("GET", r"/session/(?:state|language)", kind="local"),
    _r("POST", r"/session/(?:start|stop|new|language)", kind="local", timeout=60.0),
    # the personalities: TaDaMoo's own, with callAPIcall's paths; what they keep of the person (memory) is not part of it
    _r("GET", r"/personas", kind="local", here=True, max_body=PERSONA_BODY),
    _r("POST", r"/personas", kind="local", here=True, max_body=PERSONA_BODY),
    _r("", r"/personas/[^/]+/.+", kind="local", soon=SOON["personas"]),
    _r("GET", rf"/personas/{_PERSONA}", kind="local", here=True),
    _r("PUT", rf"/personas/{_PERSONA}", kind="local", here=True, max_body=PERSONA_BODY),
    _r("PATCH", rf"/personas/{_PERSONA}", kind="local", here=True, max_body=PERSONA_BODY),
    _r("DELETE", rf"/personas/{_PERSONA}", kind="local", here=True),
    # not there yet
    _r("", r"/avatar/(?!preview\.mjpg|stream).*", kind="local", timeout=60.0),                    # the avatar function of the service; the live streams have their own routes
    # meetings: a program on this PC may start the bot, but the announcement and the consent rule are the service's and cannot be skipped
    _r("POST", r"/meetbot/join", kind="local", timeout=60.0, gate=PROGRAMS_JOIN),
    _r("POST", r"/meetbot/leave", kind="local", timeout=60.0),
    _r("GET", r"/meetbot/status", kind="local"),
    _r("GET", r"/meetings/list", kind="local"),
    # the plan: a program may read it; adding, changing or joining a planned meeting is a way to start the bot, so it is gated like a join
    _r("GET", r"/meetings/plan", kind="local"),
    _r("POST", r"/meetings/plan", kind="local", gate=PROGRAMS_JOIN),
    _r("PUT", rf"/meetings/plan/{_ID}", kind="local", gate=PROGRAMS_JOIN),
    _r("PATCH", rf"/meetings/plan/{_ID}", kind="local", gate=PROGRAMS_JOIN),
    _r("DELETE", rf"/meetings/plan/{_ID}", kind="local", gate=PROGRAMS_JOIN),
    _r("POST", rf"/meetings/plan/{_ID}/join-now", kind="local", timeout=60.0, gate=PROGRAMS_JOIN),
    _r("GET", rf"/meetings/{_ID}(?:/transcript)?", kind="local"),
    _r("DELETE", rf"/meetings/{_ID}", kind="local"),
    _r("GET", r"/calendar/(?:status|events)", kind="local"),
    _r("POST", r"/calendar/(?:google|microsoft)/(?:connect|disconnect)", kind="local"),
    _r("POST", r"/calendar/events/[A-Za-z0-9][A-Za-z0-9:_=.+-]{0,199}/autojoin", kind="local", gate=PROGRAMS_JOIN),
    _r("GET", r"/copilot/state", kind="local"),
    _r("POST", r"/copilot/toggle", kind="local"),
)


def owns(path: str) -> bool:
    """Whether the path is one of the table, for any method."""
    return any(r.pattern.fullmatch(path) for r in ROUTES)


def find(method: str, path: str) -> tuple[Route | None, bool]:
    """(the route for this method and path, whether the path is in the table for another method)."""
    seen = False
    for r in ROUTES:
        if r.pattern.fullmatch(path):
            if r.method in ("", method):
                return r, True
            seen = True
    return None, seen


def _fail(h, status: int, code: str, message: str, **more) -> None:
    h._json(status, {"error": message, "code": code, **more})


def _gate_open(route: Route) -> bool:
    """Whether the person lets programs use a gated route: the switch is read at every request, so turning it off acts at once."""
    return route.gate != PROGRAMS_JOIN or voice_settings.meetings().get("allow_programs_join") is True


def _allowed(h, svc, route: Route) -> bool:
    """The caller's right to use the route; sends the refusal when there is none."""
    if h.remote_listener:
        _fail(h, 403, "forbidden", "These routes are not offered through the listener for phones.")
        return False
    local = settings.is_loopback(h.client_address[0])
    if route.kind == "local" and not local:
        _fail(h, 403, "loopback_only", "This route is used only from the PC the server runs on.")
        return False
    secrets = [s for s in (getattr(svc, "api_key", ""), getattr(svc, "control_token", "")) if s]
    if secrets:
        auth = h.headers.get("Authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else h.headers.get("x-api-key", "")
        if given and any(key_matches(given, s) for s in secrets):
            return True
        _fail(h, 401, "unauthorized", "Send the server's API key as a Bearer token.")
        return False
    if not local:
        _fail(h, 403, "loopback_only", "The server has no API key: these routes answer this PC only. Set a key in Settings to open them to others.")
        return False
    named, origin = h.headers.get("Host") or "", h.headers.get("Origin")
    if named and _host_of(named) not in LOOPBACK_HOSTS:                 # a name that points here from another site (DNS rebinding)
        _fail(h, 403, "forbidden", "The request names a host that is not this PC.")
        return False
    if origin and urlsplit(origin).netloc != named:
        _fail(h, 403, "forbidden", "A page of another site may not use these routes.")
        return False
    return True


def _host_of(value: str) -> str:
    """The host of a Host header, without the port."""
    if value.startswith("["):
        return value[:value.find("]") + 1].lower()
    return value.rsplit(":", 1)[0].lower() if value.count(":") == 1 else value.lower()


def _wait_s(route: Route, query: str) -> float:
    if not route.wait:
        return route.timeout
    try:
        asked = float((parse_qs(query).get(route.wait) or ["0"])[0])
    except ValueError:
        asked = 0.0
    return route.timeout + min(max(asked, 0.0), WAIT_CAP_S)


def _length(h, limit: int) -> int | None:
    """The Content-Length of an upload, or None after answering 411/413/400."""
    if h._chunked():
        _fail(h, 411, "length_required", "Send the file with a Content-Length.")
        return None
    try:
        n = int(h.headers.get("Content-Length", "0"))
    except ValueError:
        n = -1
    if n < 0:
        _fail(h, 400, "bad_request", "invalid Content-Length")
        return None
    if n > limit:
        h.close_connection = True
        _fail(h, 413, "too_large", f"The body is larger than {limit >> 20} MiB.")
        return None
    return n


def handle(h, svc) -> None:
    """Answers one request whose path is in the table: refuses it, says 501, or passes it to the voice service and its answer back."""
    parts = urlsplit(h.path)
    path = parts.path.rstrip("/") or "/"
    route, other = find(h.command, path)
    if route is None:
        if h.command in ("POST", "PUT", "PATCH"):
            h._discard_body()
        return _fail(h, 405 if other else 404, "method_not_allowed" if other else "not_found", "That method is not offered on this route." if other else "not found")
    if not _allowed(h, svc, route):
        if h.command in ("POST", "PUT", "PATCH"):
            h._discard_body()
        return
    if not _gate_open(route):
        if h.command in ("POST", "PUT", "PATCH"):
            h._discard_body()
        return _fail(h, 403, "programs_join_off", "A program may not start the meeting bot: the person has not allowed it. Settings > Voice > Meetings has the switch.",
                     message_it="Un programma non può far entrare il bot in una riunione: la persona non l'ha consentito. L'interruttore è in Impostazioni > Voce > Riunioni.")
    if route.soon:
        if h.command in ("POST", "PUT"):
            h._discard_body()
        return _fail(h, 501, "not_available_yet", route.soon)
    if len(parts.query) > QUERY_MAX:
        return _fail(h, 414, "bad_request", "The query is too long.")
    send_body = h.command in ("POST", "PUT", "PATCH")
    size = _length(h, route.max_body) if send_body and route.upload else None
    body = b""
    if send_body and not route.upload:
        try:
            body = h._body(route.max_body)
        except Exception as e:                                                  # BadBody: the status and the sentence
            h._discard_body()                                                   # what a small refused body still holds is read away, so the caller sees the answer
            return _fail(h, getattr(e, "status", 400), "bad_request", str(e))
    elif send_body and size is None:
        return
    if route.here:
        status, out = personas_compat.respond(h._personas(), h.command, path, parts.query, body)
        return h._json(status, out)
    try:
        host, port, token = voice_service.endpoint()
    except voice_service.ServiceDown as e:
        if send_body and route.upload:
            h.close_connection = True
        return _fail(h, 503, "voice_service_down", str(e))
    target = path + ("?" + parts.query if parts.query else "")
    conn = http.client.HTTPConnection(host, port, timeout=_wait_s(route, parts.query))
    try:
        conn.putrequest(h.command, target, skip_accept_encoding=True)
        conn.putheader("Authorization", "Bearer " + token)
        ctype = h.headers.get("Content-Type")
        if ctype and send_body:
            conn.putheader("Content-Type", ctype)
        if send_body:
            conn.putheader("Content-Length", str(size if route.upload else len(body)))
        conn.endheaders()
        if send_body and route.upload:
            left = size
            while left > 0:
                piece = h.rfile.read(min(left, CHUNK))
                if not piece:
                    h.close_connection = True
                    return _fail(h, 400, "bad_request", "The upload ended early.")
                conn.send(piece)
                left -= len(piece)
        elif body:
            conn.send(body)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as e:
        h.close_connection = True
        return _fail(h, 503, "voice_service_down", f"the voice service does not answer ({e.__class__.__name__})")
    try:
        _relay(h, resp, token)
    except (OSError, http.client.HTTPException):
        h.close_connection = True                                                # the service or the client went away mid-answer
    finally:
        conn.close()


def _relay(h, resp, token: str) -> None:
    kind = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
    textual = kind in ("application/json", "") or kind.startswith("text/")
    h.send_response(resp.status)
    for name in ("Content-Type", "Content-Disposition", "Cache-Control"):
        value = resp.getheader(name)
        if value:
            h.send_header(name, value)
    if textual:
        data = resp.read(BUFFERED_MAX + 1)
        if len(data) > BUFFERED_MAX:
            raise OSError("answer too large")
        data = data.replace(token.encode("utf-8"), b"[hidden]")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return
    length = resp.getheader("Content-Length")
    if length and length.isdigit():
        h.send_header("Content-Length", length)
    h.end_headers()
    while True:
        piece = resp.read(CHUNK)
        if not piece:
            break
        h.wfile.write(piece)


def routes_summary() -> list[dict]:
    """The table for the documentation and the tests: method, pattern, kind and whether it is there yet."""
    return [{"method": r.method or "*", "pattern": r.pattern.pattern, "kind": r.kind, "available": not r.soon} for r in ROUTES]
