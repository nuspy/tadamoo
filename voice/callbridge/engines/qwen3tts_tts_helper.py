#!/usr/bin/env python3
"""Helper TTS Qwen3-TTS (Alibaba Qwen) — voci preimpostate + cloning zero-shot.

Runtime: `faster-qwen3-tts` (CUDA graphs, streaming vero, gira su Windows),
nel venv `tts-venvs/dia` (condiviso con Dia: stesso torch cu128, transformers
fissato a 5.15.1 perche' lo shim di qwen-tts-hf legge `config.rope_theta`, che
dalla 5.19 non esiste piu').

Protocollo stdio degli helper del bridge, esteso con lo streaming:
  stdin:  {"text","voice","lang","speed"[,"stream":true][,"instruct"]}\\n
          {"cmd":"quit"}\\n
  stdout: senza stream: [u32 count][u32 sr][count * f32]   (count==0 = errore)
          con stream:   N blocchi [u32 count][u32 sr][count * f32] mentre il
                        modello genera, poi [0xFFFFFFFF][0] = fine; un
                        [0][0] a meta' = errore (e fine).
  stderr: log

Due famiglie di modelli, scelte dalla VOCE:
  - voce preimpostata (vivian, ryan, ...) -> modello CustomVoice;
  - qualunque altra -> riferimento in <models folder>/tts/voices/ ->
    modello Base (cloning). La trascrizione del riferimento migliora molto il
    clone: si legge da `<voce>.txt`, altrimenti si ricava una volta con
    Whisper e si salva li'; se nemmeno quella riesce si clona dal solo timbro
    (x-vector), che e' meno fedele.
Si tiene in memoria un modello alla volta: passare da una famiglia all'altra
scarica quello vecchio.

Env:
  QWEN3TTS_SIZE           0.6B | 1.7B           (default 0.6B)
  QWEN3TTS_CHUNK          passi del codec per blocco in streaming (12,5/s; default 8)
  TTS_DEVICE              cuda | cpu

Backend: torch (pesi BF16 Hugging Face, CUDA graphs, ~3,7x realtime, ~4 GB per
0.6B). `speed` non e' supportato: viene ignorato.

Modelli (Qwen/Qwen3-TTS-12Hz-<taglia>-Base e -CustomVoice): si leggono solo da
<models folder>/tts/qwen3tts-<taglia>/ (base/ per il cloning, custom-voice/ per le
voci preimpostate), in BASALT_VOICE_MODELS_DIR. L'helper non scarica nulla.
"""
from __future__ import annotations

import os as _os

# Prima di qualunque import di torch: la memoria che torch libera torna subito
# al sistema invece di restare committata (vedi call-bridge/callbridge/memoria.py).
_os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

import json
import os
import struct
import sys

import helper_models

# Protezione dello stdout binario PRIMA di importare librerie: faster-qwen3-tts
# stampa "Warming up predictor..." su stdout e corromperebbe il protocollo.
_BINARY_OUT = sys.stdout.buffer
sys.stdout = sys.stderr

import numpy as np  # noqa: E402


def log(*a) -> None:
    print("[qwen3tts_helper]", *a, file=sys.stderr, flush=True)


FINE_STREAM = 0xFFFFFFFF
# Voci del modello CustomVoice (minuscole; il modello le dichiara cosi').
PRESET = ("vivian", "serena", "uncle_fu", "dylan", "eric", "ryan", "aiden",
          "ono_anna", "sohee")
LINGUE = {"it": "italian", "en": "english", "fr": "french", "de": "german",
          "es": "spanish", "pt": "portuguese", "ru": "russian", "ja": "japanese",
          "ko": "korean", "zh": "chinese", "cmn": "chinese"}
# Livello d'uscita: Qwen3 restituisce la voce clonata al volume del
# riferimento, spesso molto basso (misurato: picco 0,14 su "Pluto"). Si porta a
# un RMS paragonabile agli altri motori, con un tetto al guadagno.
RMS_OBIETTIVO = 0.07
GUADAGNO_MAX = 6.0
# Tetto ai token audio (il codec ne emette 12,5 al secondo). Su input brevi
# come "Ciao," il modello a volte non emette la fine e prosegue: misurato, 28 s
# di farfugliato per una parola. Margine generoso (2x la durata stimata + 1,5 s
# a ~14 caratteri al secondo): tagliare una frase vera sarebbe peggio.
_TOKEN_AL_SECONDO = 12.5
_CHAR_AL_SECONDO = 14.0


def _tetto_token(testo: str) -> int:
    secondi = len(testo) / _CHAR_AL_SECONDO * 2.0 + 1.5
    return max(30, min(2048, int(secondi * _TOKEN_AL_SECONDO)))


_modello = None
_famiglia = ""          # "clone" | "preset"
_guadagni: dict[str, float] = {}


def _cartella(famiglia: str) -> str:
    """Cartella locale dei pesi della famiglia, o `ModelNotInstalled`."""
    taglia = (os.environ.get("QWEN3TTS_SIZE") or "0.6B").strip().lower()
    return helper_models.require(f"qwen3tts-{taglia}",
                                 "custom-voice" if famiglia == "preset" else "base")


def _carica(famiglia: str):
    """Modello della famiglia richiesta; scarica l'altro se era in memoria."""
    global _modello, _famiglia
    if _modello is not None and _famiglia == famiglia:
        return _modello
    cartella = _cartella(famiglia)
    import torch
    if _modello is not None:
        log(f"passo da '{_famiglia}' a '{famiglia}': scarico il modello precedente")
        _modello = None
        import gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    from faster_qwen3_tts import FasterQwen3TTS
    device = os.environ.get("TTS_DEVICE") or "cuda"
    kw = {"device": device, "dtype": torch.bfloat16 if device == "cuda" else torch.float32}
    log(f"carico {cartella} ({device})...")
    _modello = FasterQwen3TTS.from_pretrained(cartella, **kw)
    _famiglia = famiglia
    log("modello caricato")
    return _modello


def _riferimento(voce: str) -> str | None:
    return helper_models.voice_sample(voce)


def _trascrizione(rif: str) -> str:
    """Testo del riferimento: da `<rif>.txt`, altrimenti Whisper (e lo salva)."""
    txt = os.path.splitext(rif)[0] + ".txt"
    if os.path.isfile(txt):
        with open(txt, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    try:
        log(f"trascrizione assente per {os.path.basename(rif)}: la ricavo con Whisper")
        import whisper
        t = (whisper.load_model(helper_models.whisper_small())
             .transcribe(rif).get("text") or "").strip()
        if t:
            with open(txt, "w", encoding="utf-8") as f:
                f.write(t + "\n")
        return t
    except Exception as e:  # noqa: BLE001 - si ripiega sul solo timbro
        log(f"Whisper non disponibile ({e}): clone dal solo timbro (x-vector)")
        return ""


def _livella(voce: str, pezzo: np.ndarray) -> np.ndarray:
    """Porta il volume a RMS_OBIETTIVO con un guadagno per voce che si assesta
    sui primi pezzi parlati (in streaming non si conosce la frase intera)."""
    a = np.asarray(pezzo, dtype=np.float32).ravel()
    rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
    g = _guadagni.get(voce)
    if rms > 0.005:                       # pezzo con voce, non silenzio
        voluto = min(GUADAGNO_MAX, max(1.0, RMS_OBIETTIVO / rms))
        g = voluto if g is None else 0.8 * g + 0.2 * voluto
        _guadagni[voce] = g
    return np.clip(a * (g or 1.0), -0.99, 0.99)


def _scrivi(samples, sr: int) -> None:
    a = np.asarray(samples, dtype="<f4").ravel()
    _BINARY_OUT.write(struct.pack("<II", a.size, int(sr)))
    _BINARY_OUT.write(a.tobytes())
    _BINARY_OUT.flush()


def _fine_stream() -> None:
    _BINARY_OUT.write(struct.pack("<II", FINE_STREAM, 0))
    _BINARY_OUT.flush()


def _errore() -> None:
    _BINARY_OUT.write(struct.pack("<II", 0, 0))
    _BINARY_OUT.flush()


def _generatore(req: dict):
    """Generatore di (pezzo, sr) per la richiesta, in streaming o in blocco."""
    testo = (req.get("text") or "").strip()
    voce = (req.get("voice") or "").strip()
    lang = LINGUE.get((req.get("lang") or "it").lower().split("-")[0], "auto")
    stream = bool(req.get("stream"))
    passi = int(os.environ.get("QWEN3TTS_CHUNK") or 8)
    istruzione = (req.get("instruct") or "").strip() or None
    tetto = _tetto_token(testo)

    if voce.lower() in PRESET:
        m = _carica("preset")
        # Backend torch: l'elenco e' sul modello interno; GGUF: sull'oggetto.
        elenco = getattr(getattr(m, "model", None), "get_supported_speakers", None) \
            or getattr(m, "get_supported_speakers", None)
        nomi = {s.lower(): s for s in ((elenco() if callable(elenco) else None) or [])}
        speaker = nomi.get(voce.lower(), voce)
        if stream:
            for pezzo, sr, _t in m.generate_custom_voice_streaming(
                    text=testo, speaker=speaker, language=lang,
                    instruct=istruzione, chunk_size=passi,
                    max_new_tokens=tetto):
                yield pezzo, sr
        else:
            wavs, sr = m.generate_custom_voice(text=testo, speaker=speaker,
                                               language=lang, instruct=istruzione,
                                               max_new_tokens=tetto)
            yield wavs[0], sr
        return

    rif = _riferimento(voce)
    if rif is None:
        raise ValueError(f"voce '{voce}' sconosciuta: ne' preimpostata ne' clonata")
    m = _carica("clone")
    ref_text = _trascrizione(rif)
    kw = dict(text=testo, language=lang, ref_audio=rif, ref_text=ref_text,
              xvec_only=not ref_text, max_new_tokens=tetto)
    if stream:
        for pezzo, sr, _t in m.generate_voice_clone_streaming(chunk_size=passi, **kw):
            yield pezzo, sr
    else:
        wavs, sr = m.generate_voice_clone(**kw)
        yield wavs[0], sr


def main() -> int:
    for riga in sys.stdin:
        riga = riga.strip()
        if not riga:
            continue
        try:
            req = json.loads(riga)
        except json.JSONDecodeError:
            _errore()
            continue
        if req.get("cmd") == "quit":
            return 0
        if not (req.get("text") or "").strip():
            _errore()
            continue
        stream = bool(req.get("stream"))
        voce = (req.get("voice") or "").strip().lower()
        try:
            for pezzo, sr in _generatore(req):
                _scrivi(_livella(voce, pezzo), sr)
            if stream:
                _fine_stream()
        except Exception as e:  # noqa: BLE001
            log(f"sintesi fallita: {type(e).__name__}: {e}")
            _errore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
