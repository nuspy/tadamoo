"""API del tab "LLM Providers": registro dei provider, loro modelli e a chi
sono assegnati.

Le credenziali NON escono mai da qui: `GET /llm/providers` restituisce
`has_key`/`oauth_connected`, mai la chiave. Il registro sta fuori dal repo
(vedi `llm_providers.registry_path`).
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from . import llm_providers as reg

log = logging.getLogger("callbridge.api_llm")

bp = Blueprint("llm", __name__)


def _cfg():
    return current_app.config["CB_CONFIG"]


@bp.get("/llm/providers")
def elenco():
    """Provider configurati, tipi disponibili e assegnazioni correnti."""
    reg.seed_from_config(_cfg())      # primo avvio: LM Studio/vLLM da config.json
    return jsonify(reg.public())


@bp.post("/llm/providers")
def aggiungi():
    body = request.get_json(silent=True) or {}
    try:
        voce = reg.add(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    log.info("Provider LLM aggiunto: %s (%s)", voce["id"], voce["type"])
    return jsonify(reg.public())


@bp.put("/llm/providers/<pid>")
def modifica(pid: str):
    body = request.get_json(silent=True) or {}
    try:
        reg.update(pid, body)
    except KeyError:
        return jsonify({"error": f"provider '{pid}' non trovato"}), 404
    return jsonify(reg.public())


@bp.delete("/llm/providers/<pid>")
def elimina(pid: str):
    reg.remove(pid)
    log.info("Provider LLM rimosso: %s", pid)
    return jsonify(reg.public())


@bp.get("/llm/providers/<pid>/models")
def modelli(pid: str):
    """Modelli del provider, chiesti AL MOMENTO (5' di cache per i remoti).

    Sui provider locali i modelli già in memoria arrivano per primi e marcati:
    sono quelli che non costano un caricamento."""
    force = request.args.get("refresh") in ("1", "true", "yes")
    try:
        return jsonify({"models": reg.models(_cfg(), pid, force=force)})
    except KeyError:
        return jsonify({"error": f"provider '{pid}' non trovato"}), 404
    except Exception as e:  # noqa: BLE001 - provider irraggiungibile/chiave errata
        log.warning("modelli di '%s' non letti: %s", pid, e)
        return jsonify({"error": str(e)[:300], "models": []}), 502


@bp.post("/llm/providers/<pid>/test")
def prova(pid: str):
    """Prova la connessione: elenca i modelli e riferisce cosa è andato storto."""
    return jsonify(reg.test(_cfg(), pid))


@bp.post("/llm/assign")
def assegna():
    """Collega un consumatore (chat | copilot | embed) a un provider."""
    body = request.get_json(silent=True) or {}
    consumer = (body.get("consumer") or "").strip()
    pid = (body.get("provider") or "").strip() or None
    try:
        reg.assign(consumer, pid)
    except KeyError:
        return jsonify({"error": f"provider '{pid}' non trovato"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    log.info("Consumatore '%s' -> provider '%s'", consumer, pid or "(nessuno)")
    return jsonify(reg.public())
