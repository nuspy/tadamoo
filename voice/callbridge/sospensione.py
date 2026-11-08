"""Stato «componenti sospesi» (pulsante Sospendi della GUI).

Lo stato sta SU DISCO, in un file sentinella accanto a `ptt.stop`
(`call-bridge/sospeso.on`): sopravvive al riavvio del bridge e al riavvio di
Windows (nessuno script lo cancella, a differenza di `ptt.stop` che
`run-ptt.ps1` scarta se anteriore al boot) e finisce SOLO con «Riprendi».
Non e' in `config.json`: e' uno stato di lavoro, non una preferenza.

Finche' esiste NIENTE accende un componente — ne' un giro automatico (keep-alive,
warm-up all'avvio, preload dell'avatar), ne' un'azione dell'utente diversa da
«Riprendi» (avvio di una sessione, avatar, copilota, chiamate, «Load models»,
sintesi di prova...): chi ci prova riceve `ComponentiSospesi` / un 409 col
messaggio `MSG_SOSPESO`. I cambi di impostazione si SALVANO soltanto. Restano
sempre attivi la dettatura e il suo STT unico.

Questo modulo non importa nulla del pacchetto, cosi' lo usano tutti senza cicli.
"""

from __future__ import annotations

import time
from pathlib import Path

SOSPENSIONE_FILE = Path(__file__).resolve().parents[1] / "sospeso.on"

MSG_SOSPESO = "componenti sospesi: premi Riprendi"


class ComponentiSospesi(RuntimeError):
    """Un componente e' stato richiesto mentre i componenti sono sospesi."""

    def __init__(self, msg: str = MSG_SOSPESO) -> None:
        super().__init__(msg)


def in_sospensione() -> bool:
    return SOSPENSIONE_FILE.exists()


def richiedi_attivi() -> None:
    """Solleva `ComponentiSospesi` se i componenti sono sospesi."""
    if in_sospensione():
        raise ComponentiSospesi()


def sospendi() -> None:
    """Scrive la sentinella (OSError se non puo': chi chiama lo riporta)."""
    SOSPENSIONE_FILE.write_text(
        "sospeso dalla GUI il " + time.strftime("%Y-%m-%d %H:%M:%S")
        + "\nSi toglie con «Riprendi» (POST /components/resume).\n",
        encoding="utf-8")


def riprendi() -> None:
    SOSPENSIONE_FILE.unlink(missing_ok=True)


# Azioni della GUI/API che ACCENDEREBBERO un componente: da sospeso rispondono
# 409 con MSG_SOSPESO (un messaggio che la GUI mostra), invece di fallire in
# silenzio. UN solo elenco, qui. Ogni voce e' un prefisso di percorso (POST/PUT).
ENDPOINT_BLOCCATI = (
    "/call",                 # chiamata in uscita (apre sessione e motori)
    "/prepare",              # prepara una chiamata (carica i motori)
    "/models/load",          # «Load models»
    "/session/start",        # sessione voce
    "/copilot/toggle",       # copilota
    "/avatar/start",         # worker avatar
    "/analysis/start",       # analisi: trascrizione sul server voci + diarizzazione
    "/meetbot/join",         # meetbot: STT/TTS sul server voci
    "/external/call",        # chiamata pilotata dall'esterno
    "/diag/echo",            # prove guidate: TTS/STT
    "/diag/tts-roundtrip",
)


def installa_blocco(app) -> None:
    """Registra su un'app Flask il rifiuto 409 per `ENDPOINT_BLOCCATI` e la
    traduzione di `ComponentiSospesi` (sollevata dai componenti) in 409."""
    from flask import jsonify, request

    def _409():
        return jsonify({"error": MSG_SOSPESO, "code": "suspended"}), 409

    @app.before_request
    def _blocca_da_sospeso():
        if request.method not in ("POST", "PUT") or not in_sospensione():
            return None
        p = request.path
        if any(p == e or p.startswith(e + "/") for e in ENDPOINT_BLOCCATI):
            return _409()
        return None

    @app.errorhandler(ComponentiSospesi)
    def _componenti_sospesi(_e):
        return _409()
