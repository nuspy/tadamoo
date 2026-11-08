"""API REST per chiamate pilotate da un programma esterno (es. PGDCA).

Il chiamante esterno decide OGNI battuta: qui si piazza la chiamata, si
spingono le frasi da dire, si legge cio' che risponde l'interlocutore e si
riaggancia. Il "cervello" della chiamata e' l'ExternalBrain (vedi
external_brain.py), non l'agente.

Endpoint (stessa auth Bearer del control server):
  POST /external/call                {number, greeting, voice?, lang?, transport?}
                                     -> {call_id, status, transport}
       `greeting` e' obbligatorio: e' la PRIMA frase pronunciata (gli
       integratori conformi ci mettono la disclosure AI - EU AI Act art. 50).
  POST /external/<cid>/say           {text}      accoda la prossima battuta
  GET  /external/<cid>/heard?after=N&timeout_s=T
                                     prossimo turno del chiamato dopo l'indice
                                     N (blocca fino a T s) -> {text|null, index}
  GET  /external/<cid>/transcript    -> {transcript: [{speaker,text}]}
  GET  /external/<cid>/state         -> {status, transport, duration_s, ...}
  POST /external/<cid>/hangup        -> stato finale con transcript completo
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from . import external_brain
from .util import is_valid_phone_number, normalize_phone_number

log = logging.getLogger("callbridge.api.external")

bp = Blueprint("external", __name__)


def _router():
    return current_app.config.get("CB_ROUTER")


def _bridge():
    return current_app.config.get("CB_BRIDGE")


def _get(call_id: str):
    s = external_brain.get_session(call_id)
    if s is None:
        return None, (jsonify({"error": f"chiamata sconosciuta: {call_id}"}), 404)
    return s, None


@bp.post("/external/call")
def external_call():
    body = request.get_json(silent=True) or {}
    number = normalize_phone_number((body.get("number") or "").strip())
    if not number:
        return jsonify({"error": "campo 'number' mancante"}), 400
    if not is_valid_phone_number(number):
        return jsonify({"error": "numero non valido"}), 400
    greeting = (body.get("greeting") or "").strip()
    if not greeting:
        return jsonify({"error": "campo 'greeting' mancante: la prima frase "
                                 "pronunciata (disclosure AI inclusa) la "
                                 "decide il chiamante"}), 400
    if external_brain.active_session() is not None:
        return jsonify({"error": "una chiamata esterna e' gia' in corso"}), 409

    session = external_brain.new_session(number, greeting)
    brain = external_brain.ExternalBrain(session)

    router = _router()
    if router is not None:
        result = router.place_call(number, voice=body.get("voice"),
                                   lang=body.get("lang"), greeting=greeting,
                                   force=body.get("transport"), brain=brain)
    else:
        # legacy (call_transport=off): solo Android
        _bridge().start_outgoing_call(number, voice=body.get("voice"),
                                      lang=body.get("lang"), brain=brain)
        result = {"status": "calling", "transport": "android"}

    if result.get("status") not in ("calling",):
        session.mark_ended()
        return jsonify({**result, "call_id": session.id}), 502

    session.transport = result.get("transport")
    if session.transport == "gsm":
        # il greeting lo pronuncia run_outgoing() prima di ascoltare:
        # il brain non deve ripeterlo, ma va registrato nel transcript
        brain.greeting_pending = False
        session.add("assistant", greeting)
    return jsonify({"call_id": session.id, "status": "calling",
                    "transport": session.transport})


@bp.post("/external/<call_id>/say")
def external_say(call_id: str):
    s, err = _get(call_id)
    if err:
        return err
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "campo 'text' mancante"}), 400
    if s.status == "ended":
        return jsonify({"error": "chiamata terminata"}), 409
    s.say_q.put(text)
    return jsonify({"status": "queued", "call_id": call_id})


@bp.get("/external/<call_id>/heard")
def external_heard(call_id: str):
    s, err = _get(call_id)
    if err:
        return err
    after = request.args.get("after", default=-1, type=int)
    timeout_s = min(request.args.get("timeout_s", default=15.0, type=float), 60.0)
    ev, index = s.wait_heard(after, timeout_s)
    return jsonify({"call_id": call_id, "status": s.status,
                    "text": ev["text"] if ev else None, "index": index})


@bp.get("/external/<call_id>/transcript")
def external_transcript(call_id: str):
    s, err = _get(call_id)
    if err:
        return err
    return jsonify({"call_id": call_id,
                    "transcript": s.snapshot()["transcript"]})


@bp.get("/external/<call_id>/state")
def external_state(call_id: str):
    s, err = _get(call_id)
    if err:
        return err
    snap = s.snapshot()
    snap.pop("transcript", None)
    return jsonify(snap)


@bp.post("/external/<call_id>/hangup")
def external_hangup(call_id: str):
    s, err = _get(call_id)
    if err:
        return err
    router = _router()
    if s.status != "ended":
        try:
            if router is not None:
                router.hangup()
            else:
                _bridge().hangup()
        except Exception as e:  # noqa: BLE001 - riagganciare non deve fallire mai
            log.error("external[%s]: hangup: %s", call_id, e)
        s.mark_ended()
    return jsonify(s.snapshot())
