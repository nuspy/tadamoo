"""Nucleo condiviso del "turno" voce: dai chunk del cervello al testo per l'app,
alle frasi pulite per il TTS e ai media estratti.

Questa logica era reimplementata (e divergente) nelle pipeline di turno:
`streaming.py` (PTT), `voice_gateway.py` (app Android) e in parte `copilot.py`.
Le divergenze erano la CAUSA diretta di una classe di bug: il gateway
pronunciava markdown/emoji/blocchi <think> perche' non passava dal sanitizer,
e dimenticava lo `speed` del TTS resettandolo al default. Centralizzare qui la
parte PURA del turno fa si' che un fix valga per tutte le pipeline.

`ReplyProcessor` e' testabile in isolamento (solo testo, nessun I/O), cosi' il
comportamento e' verificabile senza hardware.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config
from .text_sanitize import TtsTextFilter

# Tag media inline emessi dall'agente: [[media:image:SRC]] / [[media:video:SRC]].
_MEDIA_RE = re.compile(r"\[\[media:(image|video):([^\]]+)\]\]", re.IGNORECASE)


def configure_tts(tts, cfg: Config, voice: str, lang: str,
                  engine: str = "") -> None:
    """Configura il TTS passando SEMPRE lo speed di config.

    Dimenticare `speed` (come facevano il gateway e il ramo voice_change)
    reimpostava la velocita' al default 1.0, ignorando `cfg.tts_speed`.
    """
    # `engine` solo se il client lo supporta: HandyTtsClient (backend esterno)
    # ha la firma vecchia, e passarglielo lo farebbe fallire.
    try:
        tts.configure(voice=voice, lang=lang, speed=cfg.tts_speed, engine=engine)
    except TypeError:
        tts.configure(voice=voice, lang=lang, speed=cfg.tts_speed)


@dataclass
class ReplyChunk:
    """Risultato di un feed/flush del processore di risposta."""

    # Testo 'grezzo' (tag media rimossi, tag incompleti trattenuti) per app/display.
    app_text: str = ""
    # Frasi complete gia' ripulite (markdown/emoji/reasoning fuori) per il TTS.
    tts_sentences: list[str] = field(default_factory=list)
    # Media estratti dai tag: lista di (kind, src).
    media: list[tuple[str, str]] = field(default_factory=list)


class ReplyProcessor:
    """Trasforma incrementalmente il flusso di risposta del cervello.

    Uso:
        proc = ReplyProcessor(extract_media=True)   # gateway: mostra i media
        for chunk in brain_stream:
            rc = proc.feed(chunk)
            emit(rc)
        emit(proc.flush())

    `extract_media=False` (PTT/streaming: nessun media da mostrare) salta
    l'estrazione dei tag e passa il testo intero al sanitizer, mantenendo il
    comportamento accumula-e-invia della StreamingSession.
    """

    def __init__(self, extract_media: bool = True,
                 continuazione: bool = False,
                 min_caratteri: int | None = None) -> None:
        self._buf = ""
        self._extract_media = extract_media
        self._tts = TtsTextFilter(continuazione=continuazione,
                                  min_caratteri=min_caratteri)

    def feed(self, chunk: str) -> ReplyChunk:
        self._buf += chunk
        media: list[tuple[str, str]] = []
        if self._extract_media:
            while True:
                m = _MEDIA_RE.search(self._buf)
                if not m:
                    break
                media.append((m.group(1).lower(), m.group(2).strip()))
                self._buf = self._buf[: m.start()] + self._buf[m.end():]
            # Trattieni un tag media ancora APERTO ("[[media:..." senza "]]"):
            # potrebbe completarsi nel chunk successivo.
            open_idx = self._buf.rfind("[[")
            if open_idx != -1 and "]]" not in self._buf[open_idx:]:
                safe, self._buf = self._buf[:open_idx], self._buf[open_idx:]
            else:
                safe, self._buf = self._buf, ""
        else:
            safe, self._buf = self._buf, ""
        sentences = self._tts.feed(safe) if safe else []
        return ReplyChunk(app_text=safe, tts_sentences=sentences, media=media)

    def flush(self) -> ReplyChunk:
        text = self._buf
        self._buf = ""
        if self._extract_media:
            media = [(m.group(1).lower(), m.group(2).strip())
                     for m in _MEDIA_RE.finditer(text)]
            text = _MEDIA_RE.sub("", text)
            sentences = (self._tts.feed(text) if text else []) + self._tts.flush()
            return ReplyChunk(app_text=text, tts_sentences=sentences, media=media)
        # Senza media il buffer e' gia' stato passato al sanitizer ad ogni feed:
        # qui basta il flush finale (comportamento identico a StreamingSession).
        return ReplyChunk(app_text="", tts_sentences=self._tts.flush(), media=[])
