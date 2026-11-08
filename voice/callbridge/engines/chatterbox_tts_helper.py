#!/usr/bin/env python3
"""Helper TTS Chatterbox Multilingual (Resemble AI) — voice cloning zero-shot.

Stesso protocollo stdio degli helper Kokoro/Piper (avviato da tts.rs):
  stdin:  {"text": "...", "voice": "<nome-voce-clonata>", "lang": "it", "speed": 1.0}
          {"cmd": "quit"}
  stdout: [u32 LE count][u32 LE sample_rate][count * f32 LE]
          count == 0  -> errore (dettagli su stderr)
  stderr: log ed errori

Cloning: `voice` e' il nome (senza estensione) di un wav di riferimento in
$KOKORO_MODEL_DIR/cloned_voices/ (creato da POST /voices/clone). Se la voce
non esiste si usa la voce builtin del modello. I conditionals del riferimento
vengono calcolati una volta e tenuti in cache (cambiare voce costa solo la
prima sintesi).

`lang` e' il language_id di Chatterbox Multilingual (it, en, fr, de, es, pt...).
`speed` non e' supportato dal modello: viene ignorato (avviso su stderr).

Pause da punteggiatura: il modello tende a leggere "piatto", quindi il testo
viene spezzato sui segni di interpunzione, sintetizzato per segmenti e
ricomposto inserendo silenzi. ATTENZIONE al costo: ogni segmento e' una
chiamata generate() in piu' (~1-3s di overhead l'una, misurato +55% su una
frase con due virgole), quindi di default si spezza SOLO sulla punteggiatura
forte (.!?) e le virgole le rende il modello. Configurazione via env:
CHATTERBOX_SPLIT_CHARS (".!?"; mettere ",;:.!?" per riavere anche le pause
artificiali di virgole e punti e virgola), CHATTERBOX_PAUSE_COMMA_MS (200),
CHATTERBOX_PAUSE_SEMI_MS (300), CHATTERBOX_PAUSE_PERIOD_MS (450),
CHATTERBOX_MIN_SEGMENT_CHARS (4: i segmenti piu' corti vengono fusi col
vicino — generate() crasha sui testi cortissimi).

Ambiente: il venv dedicato (chatterbox-venv, creato con --system-site-packages
per riusare il torch CUDA di sistema). Dipendenza: pip install chatterbox-tts.
Alla prima esecuzione scarica i pesi da HuggingFace (~3 GB, una tantum).
"""
from __future__ import annotations

import os as _os

# Prima di qualunque import di torch: la memoria che torch libera torna subito
# al sistema invece di restare committata (vedi call-bridge/callbridge/memoria.py).
_os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

import json
import os
import re
import struct
import sys


def log(*a) -> None:
    print("[chatterbox_tts_helper]", *a, file=sys.stderr, flush=True)


def _env_ms(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


# Pausa (ms) dopo ogni classe di punteggiatura.
PAUSE_COMMA_MS = _env_ms("CHATTERBOX_PAUSE_COMMA_MS", 200)
PAUSE_SEMI_MS = _env_ms("CHATTERBOX_PAUSE_SEMI_MS", 300)
PAUSE_PERIOD_MS = _env_ms("CHATTERBOX_PAUSE_PERIOD_MS", 450)
MIN_SEGMENT_CHARS = _env_ms("CHATTERBOX_MIN_SEGMENT_CHARS", 4)

# Su quali segni spezzare: di default SOLO punteggiatura forte — ogni
# segmento in piu' costa una generate() intera (vedi docstring).
SPLIT_CHARS = os.environ.get("CHATTERBOX_SPLIT_CHARS", ".!?")

# Split che CONSERVA il delimitatore in coda al segmento.
_SPLIT_RE = re.compile(r"(?<=[" + re.escape(SPLIT_CHARS) + r"])\s+")


def _pause_for(segment: str) -> int:
    """Millisecondi di silenzio dopo `segment`, in base all'ultimo segno."""
    tail = segment.rstrip()[-1:] if segment.rstrip() else ""
    if tail in ".!?":
        return PAUSE_PERIOD_MS
    if tail in ";:":
        return PAUSE_SEMI_MS
    if tail == ",":
        return PAUSE_COMMA_MS
    return 0


def split_segments(text: str) -> list[tuple[str, int]]:
    """Spezza `text` sulla punteggiatura -> [(segmento, pausa_ms_dopo)].

    I segmenti sotto MIN_SEGMENT_CHARS vengono fusi col successivo (l'ultimo
    col precedente): generate() va in errore tensoriale sui testi cortissimi.
    """
    raw = [s for s in _SPLIT_RE.split(text) if s.strip()]
    if not raw:
        return [(text, 0)]

    merged: list[str] = []
    for seg in raw:
        if merged and len(merged[-1].rstrip(".,;:!? ")) < MIN_SEGMENT_CHARS:
            merged[-1] = merged[-1] + " " + seg
        else:
            merged.append(seg)
    if (len(merged) > 1
            and len(merged[-1].rstrip(".,;:!? ")) < MIN_SEGMENT_CHARS):
        merged[-2] = merged[-2] + " " + merged[-1]
        merged.pop()

    out: list[tuple[str, int]] = []
    for i, seg in enumerate(merged):
        # Nessuna pausa artificiale dopo l'ULTIMO segmento (fine battuta).
        out.append((seg, _pause_for(seg) if i < len(merged) - 1 else 0))
    return out


def write_error(stdout) -> None:
    stdout.write(struct.pack("<II", 0, 0))
    stdout.flush()


def cloned_dir() -> str:
    base = os.environ.get("KOKORO_MODEL_DIR") or "."
    d = os.path.join(base, "cloned_voices")
    os.makedirs(d, exist_ok=True)
    return d


def main() -> int:
    # PRIMA di importare qualsiasi cosa: cattura lo stdout BINARIO del
    # protocollo e dirotta sys.stdout su stderr. Le librerie di Chatterbox
    # fanno print() su stdout (perth: "Loaded PerTh Watermarker...") e una
    # sola riga di testo corromperebbe l'header binario letto da tts.rs
    # (count/sr diventano byte ASCII -> lettura di GB inesistenti -> watchdog).
    stdout = sys.stdout.buffer
    sys.stdout = sys.stderr

    try:
        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    except Exception as e:  # noqa: BLE001
        log(f"import chatterbox fallito (venv chatterbox-venv? pip install "
            f"chatterbox-tts?): {e}")
        return 1

    # Device: CHATTERBOX_DEVICE (impostato dal bridge) ha precedenza sull'auto.
    forced = (os.environ.get("CHATTERBOX_DEVICE") or "").strip().lower()
    if forced in ("cpu", "cuda"):
        device = "cpu" if (forced == "cuda" and not torch.cuda.is_available()) else forced
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"caricamento Chatterbox Multilingual su {device} "
        "(primo avvio: scarica i pesi da HuggingFace)...")
    try:
        model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    except Exception as e:  # noqa: BLE001
        log(f"caricamento modello fallito: {e}")
        return 1
    sample_rate = int(model.sr)
    log(f"pronto (sr={sample_rate})")

    # Cache dei conditionals per voce: nome -> True (gia' preparati nel modello).
    # Il modello tiene UN set di conditionals alla volta: si rigenerano solo
    # quando cambia la voce richiesta.
    current_voice: str | None = object()  # sentinella != None (None = builtin)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log(f"richiesta JSON non valida: {line!r}")
            write_error(stdout)
            continue
        if req.get("cmd") == "quit":
            log("ricevuto quit, esco")
            break

        text = (req.get("text") or "").strip()
        if not text:
            write_error(stdout)
            continue
        lang = (req.get("lang") or "it").split("-")[0].lower()
        voice = (req.get("voice") or "").strip() or None
        if req.get("speed") not in (None, 1.0):
            log("nota: 'speed' non supportato da Chatterbox, ignorato")

        try:
            # Voce clonata: prepara i conditionals solo se la voce e' cambiata.
            if voice != current_voice:
                ref = None
                if voice:
                    for ext in (".wav", ".mp3", ".flac", ".ogg"):
                        cand = os.path.join(cloned_dir(), voice + ext)
                        if os.path.isfile(cand):
                            ref = cand
                            break
                if voice and ref is None:
                    log(f"voce clonata '{voice}' non trovata in {cloned_dir()}: "
                        "uso la voce builtin")
                if ref:
                    model.prepare_conditionals(ref)
                    log(f"voce clonata attiva: {os.path.basename(ref)}")
                current_voice = voice if ref else None

            # Sintesi per segmenti con pause da punteggiatura. Su errore di
            # un segmento si ritenta UNA volta col testo intero (comportamento
            # storico), poi si segnala l'errore.
            import numpy as np

            def synth(t: str):
                with torch.inference_mode():
                    wav = model.generate(t, language_id=lang)
                return wav.squeeze().detach().cpu().numpy().astype("<f4")

            segments = split_segments(text)
            pieces: list = []
            try:
                for seg, pause_ms in segments:
                    pieces.append(synth(seg))
                    if pause_ms > 0:
                        pieces.append(np.zeros(
                            int(sample_rate * pause_ms / 1000), dtype="<f4"))
            except Exception as e:  # noqa: BLE001 - fallback testo intero
                log(f"sintesi a segmenti fallita ({e}): ritento col testo intero")
                pieces = [synth(text)]

            samples = pieces[0] if len(pieces) == 1 else np.concatenate(pieces)
            stdout.write(struct.pack("<II", samples.size, sample_rate))
            stdout.write(samples.tobytes())
            stdout.flush()
        except Exception as e:  # noqa: BLE001 - segnala e continua
            log(f"sintesi fallita per {text[:60]!r}: {e}")
            write_error(stdout)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
