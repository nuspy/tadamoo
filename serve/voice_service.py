"""TaDaMoo's view of the voice service, the Python process the desktop app starts (voice/service.py).

The server never starts it. It finds it through two files in the user data folder:
    voice-service.json    {host, port, pid, started}, written by the service when it listens and removed when it stops cleanly
    voice-service.token   the bearer token the service checks on every route but /health: 64 hex characters, made here at first use
                          (the app makes it too when it starts the service first), owner-only where the system allows it
The token is read only to be sent to 127.0.0.1; no route returns it and no message contains it."""
from __future__ import annotations

import http.client
import json
import hmac
import os
import re
import secrets
from pathlib import Path

from serve import userdata, voice_settings

INFO_FILE = "voice-service.json"
TOKEN_FILE = "voice-service.token"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
TIMEOUT = 3.0


def info_path() -> Path:
    return userdata.path(INFO_FILE)


def token_path() -> Path:
    return userdata.path(TOKEN_FILE)


def info() -> dict | None:
    """What the service wrote about itself, or None when there is no such file or it is not what the service writes."""
    try:
        data = json.loads(info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    port, pid = data.get("port"), data.get("pid")
    if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 and isinstance(pid, int) and not isinstance(pid, bool)):
        return None
    host = data.get("host") if isinstance(data.get("host"), str) else "127.0.0.1"
    return {"host": host, "port": port, "pid": pid, "started": data.get("started") if isinstance(data.get("started"), str) else ""}


def _read_token() -> str:
    try:
        return token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_token() -> Path:
    """The token file, made with a random 32-byte hex token when it is missing or empty; an existing token is never replaced."""
    path = token_path()
    if _read_token():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:                                     # empty or unreadable: another process may be writing it, or it is damaged
        if _read_token():
            return path
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secrets.token_hex(32))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _get(host: str, port: int, route: str, token: str) -> tuple[int, dict | None]:
    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    try:
        conn.request("GET", route, headers={"Authorization": "Bearer " + token} if token else {})
        r = conn.getresponse()
        raw = r.read(1 << 20)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return r.status, body if isinstance(body, dict) else None
    finally:
        conn.close()


class ServiceDown(Exception):
    """The voice service cannot be reached; the message says why."""


def call(method: str, route: str, body: dict | None = None, timeout: float = TIMEOUT) -> tuple[int, dict | None]:
    """One request to the running service with its token, to 127.0.0.1 only: (status code, JSON object or None). ServiceDown when it is not there."""
    found = info()
    if found is None:
        raise ServiceDown("the voice service has not started: it leaves no info file")
    if found["host"] not in LOOPBACK:
        raise ServiceDown("the info file names a host that is not this PC")
    token = _read_token()
    if not token:
        raise ServiceDown("there is no token for the voice service")
    conn = http.client.HTTPConnection("127.0.0.1" if found["host"] == "localhost" else found["host"], found["port"], timeout=timeout)
    try:
        headers = {"Authorization": "Bearer " + token}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, route, body=payload, headers=headers)
        r = conn.getresponse()
        raw = r.read(1 << 20)
    except (OSError, http.client.HTTPException) as e:
        raise ServiceDown(f"the voice service does not answer on port {found['port']} ({e.__class__.__name__})") from None
    finally:
        conn.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if r.status == 401:
        raise ServiceDown("the voice service refused the token")
    return r.status, data if isinstance(data, dict) else None


def status() -> dict:
    """{running, port, pid, status}: `status` is the service's own /status, or {"reason": ...} when it is not reachable."""
    found = info()
    if found is None:
        return {"running": False, "port": None, "pid": None, "status": {"reason": "the voice service has not started: it leaves no info file"}}
    out = {"running": False, "port": found["port"], "pid": found["pid"]}
    if found["host"] not in LOOPBACK:
        return {**out, "status": {"reason": "the info file names a host that is not this PC"}}
    token = _read_token()
    if not token:
        return {**out, "status": {"reason": "there is no token for the voice service"}}
    try:
        code, body = _get("127.0.0.1" if found["host"] == "localhost" else found["host"], found["port"], "/status", token)
    except (OSError, http.client.HTTPException) as e:
        return {**out, "status": {"reason": f"the voice service does not answer on port {found['port']} ({e.__class__.__name__})"}}
    if code == 401:
        return {**out, "status": {"reason": "the voice service refused the token"}}
    if code != 200 or body is None:
        return {**out, "status": {"reason": f"the voice service answered {code} to its status request"}}
    mine = voice_settings.models_dir()
    theirs = body.get("models_dir")
    return {**out, "running": True, "models_dir_matches": os.path.normcase(os.path.normpath(theirs)) == os.path.normcase(os.path.normpath(mine)) if isinstance(theirs, str) else None, "status": body}


# ---------------------------------------------------------------------------------------- the service calling the server
# The same token, the other way: the service sends it to TaDaMoo's server, which accepts it on loopback for these routes and no others.
# They are what the two brains need: the model, a personality's text, and an agent conversation.
SERVICE_ROUTES = (
    ("POST", re.compile(r"^/v1/chat/completions$")),
    ("POST", re.compile(r"^/api/personas/[a-z0-9][a-z0-9-]{0,60}/compose$")),
    ("GET", re.compile(r"^/api/personas$")),
    ("POST", re.compile(r"^/api/agent/sessions$")),
    ("GET", re.compile(r"^/api/agent/sessions/[A-Za-z0-9_-]{1,64}/events$")),
    ("POST", re.compile(r"^/api/agent/sessions/[A-Za-z0-9_-]{1,64}/(send|answer|mode|persona)$")),
)


def accepts_token(given: str, method: str, path: str, client_ip: str) -> bool:
    """Whether the voice service's token, presented by a client on this PC, opens this request. Constant time; no route outside SERVICE_ROUTES."""
    from serve import settings
    if not given or not settings.is_loopback(client_ip):
        return False
    route = path.split("?", 1)[0].rstrip("/")
    if not any(m == method and rx.match(route) for m, rx in SERVICE_ROUTES):
        return False
    token = _read_token()
    return bool(token) and hmac.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8"))
