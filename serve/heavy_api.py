"""The routes of the heavy-job swap (serve/heavy_swap.py), from the PC the server runs on.

    POST /api/heavy/begin {kind, purpose, minutes?, wait_s?}   200 {ok, swap}, or 409 {ok: false, reason, message, message_it}; it waits for the person's answer
    POST /api/heavy/<id>/touch, POST /api/heavy/<id>/end       keep the swap alive, end it
    GET  /api/heavy                                            the level, the provider, the swap in force and the questions waiting
    POST /api/heavy/answer {id, allow, always?}                the person's answer to a question
    POST /api/heavy/settings {mode?, provider?}                the level (ask, allow, never) and the provider the conversations go to"""
from __future__ import annotations

import json
import re

from serve import settings, userdata
from serve.heavy_swap import HeavyPrefs, HeavySwap

BASE = "/api/heavy"
BEGIN, ANSWER, SETTINGS = BASE + "/begin", BASE + "/answer", BASE + "/settings"
ID = re.compile(r"^/api/heavy/([0-9a-f]{16})/(touch|end)$")
STATUS = {"bad_request": 400, "not_found": 404}
BODY_MAX = 1 << 12


def prefs() -> HeavyPrefs:
    return HeavyPrefs(userdata.path("heavy-swap.json"))


def swap_of(svc) -> HeavySwap:
    """The server's one swap manager, made on first use and kept on the service."""
    h = getattr(svc, "heavy_swap", None)
    if h is None:
        from tadamoo import llm
        h = svc.heavy_swap = HeavySwap(svc, llm.registry(), prefs(), ledger=llm.Ledger())
    return h


def _this_pc(handler) -> bool:
    return not handler.remote_listener and settings.is_loopback(handler.client_address[0])


def get(handler, svc) -> None:
    if not _this_pc(handler):
        return handler._json(403, {"error": {"type": "forbidden", "message": "heavy jobs are shown only on the PC the server runs on"}})
    handler._json(200, swap_of(svc).state())


def post(handler, svc, path: str) -> None:
    if not _this_pc(handler):
        handler._discard_body()
        return handler._json(403, {"error": {"type": "forbidden", "message": "the local model sleeps for a heavy job only on the PC the server runs on"}})
    try:
        req = json.loads(handler._body(BODY_MAX) or b"{}")
        if not isinstance(req, dict):
            raise ValueError("send a JSON object")
    except Exception as e:  # noqa: BLE001 - a body that cannot be read, or is not JSON
        return handler._json(getattr(e, "status", 400), {"error": {"type": "invalid_request_error", "message": str(e) or "the body is not valid JSON"}})
    swap = swap_of(svc)
    if path == SETTINGS:
        try:
            return handler._json(200, {**swap.prefs.write(req), "active": swap.state()["active"]})
        except ValueError as e:
            return handler._json(400, {"error": {"type": "heavy_swap", "message": str(e)}})
    if path == ANSWER:
        out = swap.answer(str(req.get("id") or ""), req.get("allow") is True, req.get("always") is True)
    elif path == BEGIN:
        out = swap.begin(req.get("kind"), req.get("purpose", ""), req.get("minutes", 5), req.get("wait_s", 600))
    else:
        m = ID.match(path)
        if m is None:
            return handler._json(404, {"error": {"message": "not found"}})
        out = swap.touch(m.group(1)) if m.group(2) == "touch" else swap.end(m.group(1))
    handler._json(200 if out.get("ok") else STATUS.get(out.get("reason"), 409), out)
