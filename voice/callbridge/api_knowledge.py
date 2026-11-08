"""API della sessione wikibridge per la GUI (sezione Conoscenza).

La sessione è UNA per l'app: la usano sia la GUI (che chiama wikibridge
direttamente con il token ricevuto da qui) sia l'inferenza (copilota,
riassunti), così quello che l'utente vede e quello che l'LLM riceve
passano dallo stesso filtro di permessi. Il token resta in memoria.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from . import knowledge_client as kc

log = logging.getLogger("callbridge.api_knowledge")

bp = Blueprint("knowledge", __name__)


def _cfg():
    return current_app.config["CB_CONFIG"]


@bp.get("/knowledge/session")
def sessione():
    """Modalità di wikibridge (single/multi-utente, password o SSO) e chi è
    collegato. Se wikibridge è giù: multi_user=false e reachable=false."""
    cfg = _cfg()
    if not kc.available(cfg):
        return jsonify({"reachable": False, "multi_user": False, "logged_in": False})
    return jsonify({"reachable": True, **kc.session_info(cfg)})


@bp.post("/knowledge/login")
def accedi():
    """Body {username, password} per LDAP/AD, oppure {sso: true}: il login
    SSO si apre nel browser e prosegue in background (la GUI interroga
    /knowledge/session finché sso_pending è vero)."""
    cfg = _cfg()
    body = request.get_json(silent=True) or {}
    kc.multi_user(cfg)                        # aggiorna modalità e percorso SSO
    if body.get("sso"):
        started = kc.login_sso_async(cfg)
        return jsonify({"sso_pending": True, "started": started}), 202
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or not password:
        return jsonify({"error": "username e password obbligatori"}), 400
    try:
        kc.login(cfg, username, password)
    except RuntimeError as e:
        log.info("login wikibridge non riuscito per %s", username)
        return jsonify({"error": str(e)}), 401
    return jsonify(kc.session_info(cfg))


@bp.post("/knowledge/logout")
def esci():
    kc.logout(_cfg())
    return jsonify({"logged_in": False})
