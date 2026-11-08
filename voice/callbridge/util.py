"""Helper condivisi della pipeline voce (code STT/TTS).

Erano replicati in streaming.py, voice_gateway.py e copilot.py: questa e'
l'unica copia.
"""
from __future__ import annotations

import queue
import re
import threading

# ID di artefatti su disco (riunioni, analisi, ...): solo caratteri sicuri, mai
# separatori di percorso o `.` — cosi' `<root>/<id>` non puo' risalire la
# gerarchia (`..`, `.`, nomi con slash) e finire per cancellare l'intera root.
_ARTIFACT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def is_safe_artifact_id(value: str) -> bool:
    """True se `value` e' un id di artefatto sicuro da concatenare a una root."""
    return bool(value) and _ARTIFACT_ID_RE.match(value) is not None


# Numero di telefono componibile: cifre, `+` iniziale, `*`/`#` (codici MMI).
# Serve a impedire l'iniezione di comandi AT (es. `;`+CR) via il campo `number`
# che finisce testualmente in `ATD<number>;` sulla seriale del modem.
_PHONE_RE = re.compile(r"^\+?[0-9*#]{2,20}$")


def normalize_phone_number(value: str) -> str:
    """Rimuove i separatori decorativi comuni (spazi, trattini, parentesi, punti)."""
    return re.sub(r"[\s\-().]", "", value or "")


def is_valid_phone_number(value: str) -> bool:
    """True se `value` (gia' normalizzato) e' un numero componibile sicuro."""
    return _PHONE_RE.match(value or "") is not None


def drain_queue(q: "queue.Queue") -> int:
    """Svuota una coda senza bloccare; ritorna quanti elementi ha scartato."""
    n = 0
    try:
        while True:
            q.get_nowait()
            n += 1
    except queue.Empty:
        pass
    return n


# Parole che NON possono chiudere un discorso: se l'enunciato finisce cosi',
# chi parla sta quasi certamente continuando (congiunzioni, preposizioni,
# articoli, avverbi di legame). Serve a distinguere la pausa-per-pensare dalla
# fine del turno senza allungare l'attesa per tutti.
_PAROLE_SOSPESE = {
    # congiunzioni e connettivi
    "e", "ed", "o", "od", "oppure", "ma", "pero", "però", "anzi", "quindi",
    "dunque", "allora", "perche", "perché", "poiche", "poiché", "siccome",
    "se", "quando", "mentre", "finche", "finché", "affinche", "affinché",
    "come", "cioe", "cioè", "ossia", "inoltre", "anche", "pure", "poi",
    "invece", "tuttavia", "sebbene", "benche", "benché", "nonostante",
    "che", "cui", "chi", "dove", "quanto", "quale", "quali",
    # preposizioni
    "di", "a", "ad", "da", "in", "con", "su", "per", "tra", "fra",
    "del", "dello", "della", "dei", "degli", "delle", "dal", "dallo",
    "dalla", "dai", "dagli", "dalle", "al", "allo", "alla", "ai", "agli",
    "alle", "nel", "nello", "nella", "nei", "negli", "nelle", "sul", "sullo",
    "sulla", "sui", "sugli", "sulle", "col", "coi",
    # articoli e indefiniti
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "un'",
    "questo", "questa", "questi", "queste", "quel", "quello", "quella",
    # inglese, per le sessioni in lingua
    "and", "or", "but", "so", "because", "if", "when", "while", "that",
    "which", "the", "a", "an", "of", "to", "for", "with", "in", "on",
}


def _sembra_sospeso(testo: str) -> bool:
    """True se l'enunciato pare interrotto a meta' pensiero.

    Due indizi: termina con una parola che non puo' chiudere un discorso
    ("...e", "...perche'", "...il"), oppure con virgola/due punti. In quel caso
    conviene concedere piu' tempo prima di chiudere il turno.
    """
    t = (testo or "").strip()
    if not t:
        return False
    if t[-1] in ",;:":
        return True
    if t[-1] in ".!?…":
        # Frase chiusa: MA lo STT mette il punto anche ai frammenti, quindi si
        # guarda comunque l'ultima parola.
        t = t.rstrip(".!?… ")
    ultima = re.split(r"[\s]+", t)[-1] if t else ""
    ultima = re.sub(r"[^\w']", "", ultima).lower()
    return ultima in _PAROLE_SOSPESE


def coalesce_utterances(
    q: "queue.Queue[str]",
    first: str,
    pause_s: float,
    stop: threading.Event | None = None,
    pause_max_s: float | None = None,
) -> str:
    """Unisce gli enunciati STT ravvicinati in un solo turno.

    Lo STT spezza una frase lunga sulle pause brevi: si continua a raccogliere
    finche' non passa una pausa di silenzio credibile, cosi' al cervello arriva
    UNA richiesta col testo completo.

    L'attesa e' ADATTIVA. Con un tempo fisso si e' costretti a scegliere tra
    rispondere in fretta e non tagliare chi si ferma a pensare; qui si guarda
    COME finisce l'enunciato: se e' sospeso ("...e", "...perche'", "...il", o
    finisce con una virgola) si concede `pause_max_s`, altrimenti basta
    `pause_s`. Cosi' una frase conclusa parte subito e un pensiero a meta'
    aspetta.
    """
    text = first
    lungo = pause_max_s if pause_max_s and pause_max_s > pause_s else pause_s
    while stop is None or not stop.is_set():
        attesa = lungo if _sembra_sospeso(text) else pause_s
        try:
            more = q.get(timeout=attesa)
        except queue.Empty:
            break
        if more:
            text = f"{text} {more}".strip()
    return text
