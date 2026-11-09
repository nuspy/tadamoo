"""Routes of the meeting bot, the meetings folder, the calendars and the copilot. The work is `voice/meetings_service.py`; this only carries it.

    POST /meetbot/join   {url, rights_confirmed: true, title?, on_behalf_of?, speak?, proactive?, consent_mode?, consent_window_s?}  -> 202 {session_id, state}
    GET  /meetbot/status                  state, consent, announcement, participants, speak, the tail of the transcript once consent is given
    POST /meetbot/leave                   the bot leaves (before consent: whatever it held is deleted)
    GET  /meetings/list                   GET /meetings/<id> (with the minutes)   GET /meetings/<id>/transcript   DELETE /meetings/<id> (transcript and minutes)
    GET  /calendar/status                 POST /calendar/<google|microsoft>/connect|disconnect
    GET  /calendar/events                 POST /calendar/events/<id>/autojoin {enabled}   (sets Participate of that event's plan item)
    GET  /meetings/plan                   the plan: {items (coming first, then recent), platforms, autojoin, lead_s}
    POST /meetings/plan                   {url, title?, start, duration_min?, participate?, role?, persona?, camera?, consent_mode?, notes?} -> 201 {item}
    PUT|PATCH /meetings/plan/<id>         the same fields, only the ones named change -> {item}      DELETE /meetings/plan/<id>
    POST /meetings/plan/<id>/join-now     {rights_confirmed: true}  -> 202 {session_id, state}
    POST /copilot/toggle                  GET /copilot/state
Every route needs the service token (the control app checks it); the service listens on loopback only."""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from .meetbot.errors import Refused

log = logging.getLogger("callbridge.api.meetings")

bp = Blueprint("meetings", __name__)


def _service():
    return current_app.config.get("CB_MEETINGS")


def _run(fn, *args, ok: int = 200, key: str | None = None):
    service = _service()
    if service is None:
        return jsonify({"error": "meetings are not available in this instance", "code": "unavailable"}), 503
    try:
        out = fn(service, *args)
    except Refused as e:
        return jsonify(e.body()), e.status
    return jsonify({key: out} if key else out), ok


@bp.post("/meetbot/join")
def meetbot_join():
    body = request.get_json(silent=True)
    return _run(lambda s: s.join(body if body is not None else {}), ok=202)


@bp.get("/meetbot/status")
def meetbot_status():
    return _run(lambda s: s.status())


@bp.post("/meetbot/leave")
def meetbot_leave():
    return _run(lambda s: s.leave())


@bp.get("/meetings/list")
def meetings_list():
    return _run(lambda s: s.list(), key="meetings")


@bp.get("/meetings/<mid>")
def meetings_get(mid: str):
    return _run(lambda s: s.get(mid))


@bp.get("/meetings/<mid>/transcript")
def meetings_transcript(mid: str):
    return _run(lambda s: s.transcript(mid), key="segments")


@bp.delete("/meetings/<mid>")
def meetings_delete(mid: str):
    return _run(lambda s: (s.delete(mid), {"status": "ok"})[1])


@bp.get("/calendar/status")
def calendar_status():
    return _run(lambda s: s.calendar_status())


@bp.post("/calendar/<provider>/connect")
def calendar_connect(provider: str):
    return _run(lambda s: s.calendar_connect(provider))


@bp.post("/calendar/<provider>/disconnect")
def calendar_disconnect(provider: str):
    return _run(lambda s: (s.calendar_disconnect(provider), {"status": "ok"})[1])


@bp.get("/calendar/events")
def calendar_events():
    days = max(1, min(31, request.args.get("days", default=7, type=int)))
    return _run(lambda s: s.calendar_events(days), key="events")


@bp.post("/calendar/events/<path:event_id>/autojoin")
def calendar_autojoin(event_id: str):
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled is true or false", "code": "bad_request"}), 400
    return _run(lambda s: s.calendar_participate(event_id, enabled))


@bp.get("/meetings/plan")
def plan_list():
    return _run(lambda s: s.plan_list())


@bp.post("/meetings/plan")
def plan_add():
    body = request.get_json(silent=True)
    return _run(lambda s: s.plan_add(body if body is not None else {}), ok=201)


@bp.route("/meetings/plan/<item_id>", methods=["PUT", "PATCH"])
def plan_update(item_id: str):
    body = request.get_json(silent=True)
    return _run(lambda s: s.plan_update(item_id, body if body is not None else {}))


@bp.delete("/meetings/plan/<item_id>")
def plan_delete(item_id: str):
    return _run(lambda s: s.plan_delete(item_id))


@bp.post("/meetings/plan/<item_id>/join-now")
def plan_join_now(item_id: str):
    body = request.get_json(silent=True)
    return _run(lambda s: s.plan_join_now(item_id, body if body is not None else {}), ok=202)
