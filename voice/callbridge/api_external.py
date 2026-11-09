"""API REST per chiamate pilotate da un programma esterno (es. PGDCA).

Il chiamante esterno decide OGNI battuta: qui si piazza la chiamata, si
spingono le frasi da dire, si legge cio' che risponde l'interlocutore e si
riaggancia. Il "cervello" della chiamata e' l'ExternalBrain (vedi
external_brain.py), non l'agente.

Endpoint (stessa auth Bearer del control server):
  POST /external/call                {number, greeting, voice?, lang?, transport?, approval_id?}
                                     -> {call_id, status, transport}
       `greeting` e' obbligatorio: e' la PRIMA frase pronunciata e deve dire che
       chi chiama e' un'IA (EU AI Act art. 50), salvo che la persona abbia
       spento l'avviso. Passa dalla politica (call_policy.py): con
       `outgoing_approval` = manual un programma riceve 202 e `approval_id`, la
       persona approva, e la stessa richiesta con `approval_id` parte; l'agente
       di TaDaMoo manda `X-Call-Origin: agent` e chiede lui alla persona.
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

log = logging.getLogger("callbridge.api.external")

bp = Blueprint("external", __name__)


def _calls():
    return current_app.config["CB_CALLS"]


def _get(call_id: str):
    s = external_brain.get_session(call_id)
    if s is None:
        return None, (jsonify({"error": f"chiamata sconosciuta: {call_id}"}), 404)
    return s, None


@bp.post("/external/call")
def external_call():
    body = request.get_json(silent=True) or {}
    if external_brain.active_session() is not None:
        return jsonify({"error": "una chiamata esterna e' gia' in corso", "code": "busy"}), 409
    made: dict = {}

    def brain_for(decision):
        """Chiamato dopo il via della politica: la sessione nasce solo per una chiamata che parte davvero."""
        session = external_brain.new_session(decision.number, decision.greeting)
        made["session"], made["greeting"] = session, decision.greeting
        made["brain"] = external_brain.ExternalBrain(session)
        return made["brain"]

    origin = "agent" if request.headers.get("X-Call-Origin") == "agent" else "program"
    code, result = _calls().place(origin, str(body.get("number") or ""), greeting=str(body.get("greeting") or ""), voice=body.get("voice"),
                                  lang=body.get("lang"), transport=body.get("transport"), approval_id=str(body.get("approval_id") or ""),
                                  brain_for=brain_for)
    session = made.get("session")
    if code != 200:
        if session is not None:
            session.mark_ended()
            return jsonify({**result, "call_id": session.id}), code
        return jsonify(result), code

    session.transport = result.get("transport")
    if session.transport == "gsm":
        # il greeting lo pronuncia run_outgoing() prima di ascoltare:
        # il brain non deve ripeterlo, ma va registrato nel transcript
        made["brain"].greeting_pending = False
        session.add("assistant", made["greeting"])
    return jsonify({"call_id": session.id, "status": "calling", "transport": session.transport})


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
    if s.status != "ended":
        try:
            _calls().hangup()
        except Exception as e:  # noqa: BLE001 - riagganciare non deve fallire mai
            log.error("external[%s]: hangup: %s", call_id, e)
        s.mark_ended()
    return jsonify(s.snapshot())
