"""Immagine fissa dell'avatar: il primo fotogramma del suo clip.

A cosa serve. Nell'app il video WebRTC si accende solo quando si attiva
l'audio. Prima di allora l'avatar non ha nulla da dire, e tenere aperto un
flusso per mostrare una faccia immobile fa lavorare per niente l'encoder del
PC, la rete e soprattutto il decoder del telefono — che è la batteria. Con un
JPEG si vede la stessa identica cosa a costo zero.

Perché il PRIMO fotogramma e non uno qualsiasi: è l'immagine da cui il video
partirebbe, quindi quando il flusso si accende non c'è nessuno scatto fra la
posa ferma e il primo fotogramma animato.

Sta in un modulo suo perché lo servono due server diversi: il control server
Flask (:8770, per la GUI del PC) e il media server (:8772, per l'app).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("callbridge.poster")

#: Il decode di un mp4 non è gratis: il JPEG si tiene accanto al manifest.
POSTER_NOME = "poster.jpg"

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def cartella_persona(nome: str, radice: Path) -> Path | None:
    """`<radice>/<nome>`, con lo stesso addolcimento usato dalle API persone.

    Ritorna None per un nome che non passa il filtro: questa funzione riceve
    un pezzo di URL che arriva dal telefono, quindi un `..` non deve poter
    uscire dalla cartella delle persone.
    """
    safe = re.sub(r"\s+", "_", (nome or "").strip())
    if not safe or not _NAME_RE.match(safe):
        return None
    return radice / safe


def genera(pdir: Path) -> Path | None:
    """Estrae il primo fotogramma del clip in `<persona>/poster.jpg`.

    Ritorna il path del JPEG, o None se la persona non ha clip da cui
    ricavarlo (caso legittimo: una persona appena creata).
    """
    dest = pdir / POSTER_NOME
    try:
        manifest = json.loads((pdir / "manifest.json").read_text(encoding="utf-8"))
        clips = manifest.get("clips") or []
    except (OSError, json.JSONDecodeError):
        clips = []
    if not clips:
        # Nessun clip: e' un avatar creato da FOTO (wizard). Il poster.jpg e'
        # gia' quello, messo dal wizard; servirlo com'e'. Solo se manca pure
        # quello non c'e' nulla da mostrare.
        return dest if dest.is_file() else None
    sorgente = pdir / str(clips[0])
    if not sorgente.is_file():
        return None
    # Se il poster è più recente del clip è ancora buono: rigenerarlo a ogni
    # richiesta vorrebbe dire aprire un mp4 a ogni apertura dell'app.
    if dest.is_file() and dest.stat().st_mtime >= sorgente.stat().st_mtime:
        return dest
    import cv2  # pesante: si importa solo quando c'è davvero da generare
    cap = cv2.VideoCapture(str(sorgente))
    try:
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok or frame is None:
        log.warning("poster: nessun fotogramma leggibile in %s", sorgente)
        return None
    if not cv2.imwrite(str(dest), frame, [cv2.IMWRITE_JPEG_QUALITY, 88]):
        log.warning("poster: scrittura fallita in %s", dest)
        return None
    log.info("poster generato per %s (%dx%d)", pdir.name,
             frame.shape[1], frame.shape[0])
    return dest


def per_nome(nome: str, radice: Path) -> Path | None:
    """Poster della persona `nome`, generandolo se manca."""
    pdir = cartella_persona(nome, radice)
    if pdir is None or not pdir.is_dir():
        return None
    return genera(pdir)
