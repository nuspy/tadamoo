"""Blueprint /session/* e /copilot/* — controllo sessioni voce dalla GUI.

Equivalente programmatico delle hotkey (harness Shift+F1/F2, diretto Ctrl+Shift+F4/F5, copilota Shift+F4), operante sulla
STESSA istanza di PushToTalkManager/CopilotController delle hotkey (vedi
`main.py all` -> `create_app(ptt=..., copilot=...)`). Se l'istanza non e'
disponibile (es. `main.py serve` con PTT disabilitato) risponde 503.

Endpoint:
  POST /session/start   {"mode": "half"|"full", "brain": "harness"|"direct" (opzionale), "avatar": bool}
  POST /session/stop
  GET  /session/state   -> {"active", "mode", "phase", "avatar"}
  GET  /session/voice    -> {"active", "channel", "identity"} - chi la voce della sessione in corso ha riconosciuto (tadamoo/speech/voiceid.py)
  POST /session/voice/confirm {"contact_id"} -> arruola l'audio recente della sessione su quel contatto
  POST /copilot/toggle  -> {"active"}
  GET  /copilot/state   -> {"enabled", "active"}
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

log = logging.getLogger("callbridge.api.session")

bp = Blueprint("session", __name__)


def _ptt():
    return current_app.config.get("CB_PTT")


def _copilot():
    return current_app.config.get("CB_COPILOT")


@bp.post("/session/start")
def session_start():
    ptt = _ptt()
    if ptt is None:
        return jsonify({"error": "push-to-talk non disponibile in questa istanza"}), 503
    body = request.get_json(silent=True) or {}
    mode = (body.get("mode") or "half").strip().lower()
    avatar = bool(body.get("avatar"))
    brain = (body.get("brain") or "").strip().lower()
    ok, detail = ptt.start_session(mode, avatar=avatar, brain=brain)
    if ok:
        return jsonify({"status": "started", "mode": detail, "avatar": avatar})
    if detail.startswith("not_loaded:"):
        return jsonify({"error": "the speech to text and the speech synthesis must be loaded first (Models > Voice services)",
                        "code": "not_loaded", "missing": detail.split(":", 1)[1].split(",")}), 409
    if detail.startswith("unavailable:"):
        return jsonify({"error": detail.split(":", 1)[1], "code": "unavailable"}), 503
    if detail == "suspended":
        from .sospensione import MSG_SOSPESO
        return jsonify({"error": MSG_SOSPESO, "code": "suspended"}), 409
    if detail == "session_active":
        return jsonify({"error": "session_active"}), 409
    if detail == "start_failed":
        return jsonify({"error": "avvio fallito: modelli Handy non pronti "
                                 "o device audio non disponibili"}), 503
    return jsonify({"error": detail}), 400


@bp.post("/session/stop")
def session_stop():
    ptt = _ptt()
    if ptt is None:
        return jsonify({"error": "push-to-talk non disponibile in questa istanza"}), 503
    had = ptt.stop_session()
    return jsonify({"status": "stopped" if had else "idle"})


@bp.get("/session/state")
def session_state():
    ptt = _ptt()
    if ptt is None:
        return jsonify({"error": "push-to-talk non disponibile in questa istanza"}), 503
    return jsonify(ptt.session_state())


@bp.get("/session/voice")
def session_voice():
    ptt = _ptt()
    if ptt is None:
        return jsonify({"error": "push-to-talk non disponibile in questa istanza"}), 503
    return jsonify(ptt.voice_status())


@bp.post("/session/voice/confirm")
def session_voice_confirm():
    ptt = _ptt()
    if ptt is None:
        return jsonify({"error": "push-to-talk non disponibile in questa istanza"}), 503
    contact_id = str((request.get_json(silent=True) or {}).get("contact_id") or "")
    if not contact_id:
        return jsonify({"error": "contact_id"}), 400
    problem = ptt.voice_confirm(contact_id)
    if problem == "no_session":
        return jsonify({"error": "no session is active"}), 409
    if problem == "unavailable":
        return jsonify({"error": "voice identification is not available"}), 503
    if problem == "not_enough_audio":
        return jsonify({"error": "not enough recent audio yet - let the person speak a little more first"}), 409
    if problem:
        return jsonify({"error": problem}), 400
    return jsonify({"status": "enrolled"})


@bp.post("/copilot/toggle")
def copilot_toggle():
    copilot = _copilot()
    if copilot is None:
        return jsonify({"error": "the copilot is not available in this instance"}), 503
    copilot.toggle()
    return jsonify({"active": copilot.is_active, "note": copilot.note})


@bp.get("/copilot/state")
def copilot_state():
    copilot = _copilot()
    if copilot is None:
        return jsonify({"enabled": False, "active": False})
    return jsonify(copilot.state())
