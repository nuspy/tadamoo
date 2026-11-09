"""Routes of the meeting bot, the meetings folder, the calendars and the copilot. The work is `voice/meetings_service.py`; this only carries it.

    POST /meetbot/join   {url, rights_confirmed: true, title?, on_behalf_of?, speak?, proactive?, consent_mode?, consent_window_s?}  -> 202 {session_id, state}
    GET  /meetbot/status                  state, consent, announcement, participants, speak, the tail of the transcript once consent is given
    POST /meetbot/leave                   the bot leaves (before consent: whatever it held is deleted)
    GET  /meetings/list                   GET /meetings/<id> (with the minutes)   GET /meetings/<id>/transcript   DELETE /meetings/<id> (transcript and minutes)
    GET  /calendar/status                 POST /calendar/<google|microsoft>/connect|disconnect
    GET  /calendar/events                 POST /calendar/events/<id>/autojoin {enabled}
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
    return _run(lambda s: s.watcher().fetch_events(days=days), key="events")


@bp.post("/calendar/events/<path:event_id>/autojoin")
def calendar_autojoin(event_id: str):
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    return _run(lambda s: (s.watcher().store.set(event_id, enabled), {"id": event_id, "autojoin": enabled})[1])
