"""Settings > Network: where the server listens, the names it is reached by, and a test that says whether a chosen address answers.

Routes (the settings page and its tests; the writes only from this PC, like the other settings):

  GET  /api/network         what the server runs with, what the config file says (`server.host`, `server.port`, `server.allowed-hosts`, `server.public-name`), and what waits for a restart
  POST /api/network         {"host", "port", "allowed_hosts": [...], "public_name"} -> the config file (null or "" takes a value out); applies when the server is started again
  POST /api/network/test    {"name", "scheme", "port", "host"}: does that address answer, and does the Host rule let its name through (also behind a reverse proxy)
  GET  /api/network/ping    {"tadamoo": true, "host": the Host asked at, "proto"/"forwarded_host": what a proxy forwarded}: what the test asks, with no key, so that the answer shows what arrived

The Host rule is serve/origin_guard.py `host_allowed`: an IP address, localhost, this PC's own name or a name of `allowed-hosts` (and `public-name`); it is off when the server has an API key.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List

from serve import netaddr, origin_guard, settings

TIMEOUT_S = 6.0
DEFAULTS = {"host": "127.0.0.1", "allowed_hosts": [], "public_name": ""}


def _saved(path: str) -> Dict[str, Any]:
    raw = settings.values_of(settings.read_file(path), settings.NETWORK_FIELDS)
    out: Dict[str, Any] = {}
    for field, key in (("host", "server.host"), ("port", "server.port"), ("allowed_hosts", "server.allowed-hosts"), ("public_name", "server.public-name")):
        if key in raw:
            value = raw[key]
            out[field] = [str(v) for v in value] if field == "allowed_hosts" and isinstance(value, list) else value
    return out


def _norm(value: Any) -> Any:
    return [str(v) for v in value] if isinstance(value, list) else value


def view(svc: Any, bound: tuple, local_client: bool) -> Dict[str, Any]:
    """What the server runs with and what the config file says; `bound` is the (host, port) it listens on."""
    info = getattr(svc, "settings_info", None) or {}
    path = info.get("config")
    running = {"host": str(bound[0]), "port": int(bound[1]), "allowed_hosts": [h for h in getattr(svc, "allowed_hosts", ()) if h != getattr(svc, "public_name", "")],
               "public_name": getattr(svc, "public_name", ""), "api_key": bool(getattr(svc, "api_key", ""))}
    out: Dict[str, Any] = {"config": path, "running": running, "saved": {}, "pending": [], "editable": False, "reason": "", "note": settings.RESTART_NOTE,
                           "own_name": socket.gethostname().lower(), "addresses": netaddr.DEFAULT.own()}
    if not path:
        out["reason"] = "no_config"
        return out
    try:
        out["saved"] = _saved(path)
    except (OSError, ValueError):
        out["reason"] = "unreadable"
        return out
    if not local_client:
        out["reason"] = "not_local"
    else:
        import os
        out["editable"] = os.access(path, os.W_OK)
        out["reason"] = "" if out["editable"] else "read_only"
    cli = {k.split(".", 1)[1] for k in (info.get("cli") or [])}
    names = {"host": "host", "port": "port", "allowed_hosts": "allowed-hosts", "public_name": "public-name"}
    out["from_command_line"] = [f for f, n in names.items() if n in cli]                   # given on the command line: it wins over the file
    out["pending"] = [f for f in names if f in out["saved"] and f not in out["from_command_line"] and _norm(out["saved"][f]) != _norm(running[f])]
    return out


def save(svc: Any, req: Any) -> Dict[str, Any]:
    """Writes the fields the request carries to the config file (ValueError with the reason, OSError when the file cannot be written)."""
    path = (getattr(svc, "settings_info", None) or {}).get("config")
    if not path:
        raise ValueError("the server was started without --config: there is no settings file")
    if not isinstance(req, dict) or not (req.keys() & {"host", "port", "allowed_hosts", "public_name"}):
        raise ValueError('send {"host", "port", "allowed_hosts": [...], "public_name"} (one or more)')
    changes: Dict[str, Any] = {}
    for field, key in (("host", "server.host"), ("port", "server.port"), ("allowed_hosts", "server.allowed-hosts"), ("public_name", "server.public-name")):
        if field not in req:
            continue
        value = req[field]
        empty = value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not [v for v in value if str(v).strip()])
        changes[key] = None if empty else value
    result = settings.save(path, changes, settings.NETWORK_FIELDS)
    return {"saved": True, "changed": result["changed"], "note": settings.RESTART_NOTE}


def ping_body(headers: Any, client_ip: str) -> Dict[str, Any]:
    fwd = netaddr.forwarded(headers, client_ip)
    return {"tadamoo": True, "host": str(headers.get("Host") or ""), "proto": fwd["proto"], "forwarded_host": fwd["host"]}


def _rule(name: str, svc: Any, saved: List[str]) -> Dict[str, Any]:
    allowed = tuple(getattr(svc, "allowed_hosts", ()))
    now = origin_guard.host_allowed(name, allowed, socket.gethostname())
    after = origin_guard.host_allowed(name, tuple(dict.fromkeys([*allowed, *saved])), socket.gethostname())
    return {"id": "rule", "name": name, "ok": now, "after_restart": after, "off_by_key": bool(getattr(svc, "api_key", ""))}


def _local(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "", "::") else host


def _tcp(host: str, port: int) -> str:
    """"" when something accepts a connection there, else the reason."""
    try:
        with socket.create_connection((_local(host), port), timeout=TIMEOUT_S):
            return ""
    except OSError as e:
        return e.strerror or str(e)


def test(svc: Any, bound: tuple, req: Any) -> Dict[str, Any]:
    """The checks for an address: `name` (a domain or an IP) with `scheme` and `port`, or only the address to listen on (`host`, `port`).
    Every check says `ok` and carries what the page needs to explain it; nothing is changed."""
    req = req if isinstance(req, dict) else {}
    name = str(req.get("name") or "").strip().lower().rstrip(".")
    scheme = str(req.get("scheme") or "").lower() or ("https" if name and not name[0].isdigit() else "http")
    if scheme not in ("http", "https"):
        raise ValueError("scheme is http or https")
    port_raw = req.get("port")
    try:
        port = int(port_raw) if port_raw not in (None, "") else (int(bound[1]) if not name else (443 if scheme == "https" else 80))
    except (TypeError, ValueError):
        raise ValueError("port is a whole number") from None
    if not 1 <= port <= 65535:
        raise ValueError("port is 1 to 65535")
    checks: List[Dict[str, Any]] = []
    listen = str(req.get("host") or bound[0])
    path = (getattr(svc, "settings_info", None) or {}).get("config")
    try:
        file = _saved(path) if path else {}
        saved_hosts = [*file.get("allowed_hosts", []), *([file["public_name"]] if file.get("public_name") else [])]
    except (OSError, ValueError):
        saved_hosts = []
    if name:
        try:
            if not _is_ip(name):
                name = origin_guard.check_hostname(name)
        except ValueError as e:
            raise ValueError(f"name: {e}") from None
        checks.append(_rule(name, svc, saved_hosts))
    if not name:
        reason = _tcp(listen, port)
        checks.append({"id": "listen", "host": listen, "port": port, "running": port == int(bound[1]) and _local(listen) == _local(str(bound[0])), "answers": not reason, "reason": reason})
        return {"checks": checks}
    target = f"{scheme}://{name}" + ("" if (scheme, port) in (("http", 80), ("https", 443)) else f":{port}") + "/api/network/ping"
    check: Dict[str, Any] = {"id": "reach", "url": target, "ok": False, "status": 0, "error": "", "seen": {}}
    ctx = ssl.create_default_context() if scheme == "https" else None
    try:
        with urllib.request.urlopen(urllib.request.Request(target, headers={"Accept": "application/json", "User-Agent": "tadamoo-network-test"}), timeout=TIMEOUT_S, context=ctx) as r:
            body = json.loads(r.read(4096).decode("utf-8", "replace") or "{}")
            check.update(status=r.status, ok=bool(body.get("tadamoo")), seen={k: body.get(k, "") for k in ("host", "proto", "forwarded_host")})
            if not check["ok"]:
                check["error"] = "not_tadamoo"
    except urllib.error.HTTPError as e:
        check["status"] = e.code
        try:
            body = json.loads(e.read(4096).decode("utf-8", "replace") or "{}")
        except ValueError:
            body = {}
        code = ((body.get("error") or {}) if isinstance(body.get("error"), dict) else {}).get("code") or body.get("reason") or ""
        check["error"] = "host_not_allowed" if code == "host_not_allowed" else f"http_{e.code}"
    except ssl.SSLError as e:
        check["error"] = "tls: " + (getattr(e, "reason", "") or str(e))
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        check["error"] = str(getattr(reason, "strerror", None) or reason)[:200]
    checks.append(check)
    return {"checks": checks}


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False
