"""The routes of the VRAM broker (serve/vram_broker.py), from the PC the server runs on and, for the lease routes, from the voice service with its token.

    POST /api/vram/lease {owner, mib, purpose?, hold_s?, wait_s?}   200 {ok, lease}, or 409 {ok: false, reason, message, message_it}
    POST /api/vram/lease/<id>/touch                                 extend the hold
    POST /api/vram/lease/<id>/release                               give the VRAM back
    GET  /api/vram/leases                                           the state: leases, free VRAM, lent, reserve
    GET  /api/vram/settings, POST /api/vram/settings {enabled?, hold_s?}"""
from __future__ import annotations

import json
import re

from serve import settings, userdata
from serve.vram_broker import BrokerPrefs, VramBroker

LEASE = "/api/vram/lease"
LEASES = "/api/vram/leases"
SETTINGS = "/api/vram/settings"
ID = re.compile(r"^/api/vram/lease/([0-9a-f]{16})/(touch|release)$")
STATUS = {"bad_request": 400, "not_found": 404, "no_engine": 409, "unavailable": 409, "busy": 409, "not_enough": 409, "engine_died": 409,
          "disabled": 409}
BODY_MAX = 1 << 12


def prefs() -> BrokerPrefs:
    return BrokerPrefs(userdata.path("vram-broker.json"))


def broker_of(svc) -> VramBroker:
    """The server's one broker, made on first use and kept on the service."""
    b = getattr(svc, "vram_broker", None)
    if b is None:
        b = svc.vram_broker = VramBroker(svc, prefs=prefs())
    return b


def _this_pc(handler) -> bool:
    return not handler.remote_listener and settings.is_loopback(handler.client_address[0])


def _answer(handler, out: dict) -> None:
    handler._json(200 if out.get("ok") else STATUS.get(out.get("reason"), 409), out)


def get(handler, svc, path: str) -> None:
    if not _this_pc(handler):
        return handler._json(403, {"error": {"type": "forbidden", "message": "the VRAM broker is shown only on the PC the server runs on"}})
    if path == SETTINGS:
        return handler._json(200, prefs().read())
    handler._json(200, broker_of(svc).state())


def post(handler, svc, path: str) -> None:
    if not _this_pc(handler):
        handler._discard_body()
        return handler._json(403, {"error": {"type": "forbidden", "message": "the VRAM is lent only on the PC the server runs on"}})
    try:
        req = json.loads(handler._body(BODY_MAX) or b"{}")
        if not isinstance(req, dict):
            raise ValueError("send a JSON object")
    except Exception as e:  # noqa: BLE001 - a body that cannot be read (BadBody), or is not JSON
        return handler._json(getattr(e, "status", 400), {"error": {"type": "invalid_request_error", "message": str(e) or "the body is not valid JSON"}})
    if path == SETTINGS:
        try:
            return handler._json(200, prefs().write(req))
        except ValueError as e:
            return handler._json(400, {"error": {"type": "vram_broker", "message": str(e)}})
    broker = broker_of(svc)
    if path == LEASE:
        return _answer(handler, broker.acquire(req.get("owner"), req.get("mib"), req.get("purpose", ""), req.get("hold_s"), req.get("wait_s", 0)))
    m = ID.match(path)
    if m is None:
        return handler._json(404, {"error": {"message": "not found"}})
    _answer(handler, broker.touch(m.group(1)) if m.group(2) == "touch" else broker.release(m.group(1)))
