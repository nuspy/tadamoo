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
  - qualunque altra -> riferimento in $KOKORO_MODEL_DIR/cloned_voices/ ->
    modello Base (cloning). La trascrizione del riferimento migliora molto il
    clone: si legge da `<voce>.txt`, altrimenti si ricava una volta con
    Whisper e si salva li'; se nemmeno quella riesce si clona dal solo timbro
    (x-vector), che e' meno fedele.
Si tiene in memoria un modello alla volta: passare da una famiglia all'altra
scarica quello vecchio.

Env:
  QWEN3TTS_SIZE           0.6B | 1.7B           (default 0.6B)
  QWEN3TTS_CLONE_MODEL    repo HF o cartella del modello Base   (vuoto = ufficiale)
  QWEN3TTS_PRESET_MODEL   repo HF o cartella del CustomVoice    (vuoto = ufficiale)
  QWEN3TTS_BACKEND        auto | torch | ggml   (default auto)
  QWEN3TTS_QUANT          BF16 | Q8_0 | Q4_K_M  (solo ggml; default Q8_0)
  QWEN3TTS_CHUNK          passi del codec per blocco in streaming (12,5/s; default 8)
  TTS_DEVICE              cuda | cpu

Backend:
  - torch: pesi BF16 Hugging Face, CUDA graphs (~3,7x realtime, ~4 GB per 0.6B);
  - ggml:  GGUF quantizzati con qwentts.cpp (wheel Windows costruita da
           tools/qwentts-cpp-win, upstream non la pubblica). Misurato su 0.6B
           Q8_0: primo audio in 0,05 s, ~16x realtime, ~2,5 GB. Conosce solo i
           modelli UFFICIALI: con una variante custom si usa torch;
  - auto:  ggml Q8_0 se la wheel e' installata e il modello e' ufficiale,
           altrimenti torch.
`speed` non e' supportato: viene ignorato.

Modelli: si scaricano da Hugging Face nella cache indicata da HF_HUB_CACHE (il
bridge passa quella dell'app, %LOCALAPPDATA%\\callAPIcall\\models\\huggingface).
`python qwen3tts_tts_helper.py --prefetch` scarica SUBITO esattamente cio' che
il motore caricherebbe con l'env corrente (taglia, backend, quantizzazione,
entrambe le famiglie): lo usa l'installer, cosi' la prima frase non aspetta
minuti di download. Con --size 1.7B si sceglie la taglia da riga di comando.
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

# Protezione dello stdout binario PRIMA di importare librerie: faster-qwen3-tts
# stampa "Warming up predictor..." su stdout e corromperebbe il protocollo.
_BINARY_OUT = sys.stdout.buffer
sys.stdout = sys.stderr

import numpy as np  # noqa: E402


def log(*a) -> None:
    print("[qwen3tts_helper]", *a, file=sys.stderr, flush=True)


CLONE_EXTS = (".wav", ".mp3", ".flac", ".ogg")
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


def _repo(famiglia: str) -> str:
    taglia = (os.environ.get("QWEN3TTS_SIZE") or "0.6B").strip()
    if famiglia == "preset":
        return (os.environ.get("QWEN3TTS_PRESET_MODEL") or "").strip() or \
            f"Qwen/Qwen3-TTS-12Hz-{taglia}-CustomVoice"
    return (os.environ.get("QWEN3TTS_CLONE_MODEL") or "").strip() or \
        f"Qwen/Qwen3-TTS-12Hz-{taglia}-Base"


def _carica(famiglia: str):
    """Modello della famiglia richiesta; scarica l'altro se era in memoria."""
    global _modello, _famiglia
    if _modello is not None and _famiglia == famiglia:
        return _modello
    import torch
    if _modello is not None:
        log(f"passo da '{_famiglia}' a '{famiglia}': scarico il modello precedente")
        _modello = None
        import gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    from faster_qwen3_tts import FasterQwen3TTS
    repo = _repo(famiglia)
    device = os.environ.get("TTS_DEVICE") or "cuda"
    backend = _backend(repo, device)
    kw = {"device": device, "dtype": torch.bfloat16 if device == "cuda" else torch.float32}
    if backend == "ggml":
        kw = {"backend": "ggml",
              "quant": (os.environ.get("QWEN3TTS_QUANT") or "Q8_0").strip()}
    log(f"carico {repo} ({backend}{'/' + kw['quant'] if backend == 'ggml' else ''}, {device})...")
    _modello = FasterQwen3TTS.from_pretrained(repo, **kw)
    _famiglia = famiglia
    log("modello caricato")
    return _modello


def _ggml_disponibile() -> bool:
    """La wheel qwentts-cpp-python e' installata (la DLL si carica dopo)."""
    import importlib.util
    return importlib.util.find_spec("qwentts_cpp") is not None


def _backend(repo: str, device: str) -> str:
    """torch | ggml secondo QWEN3TTS_BACKEND, il modello e cio' che e' installato."""
    scelto = (os.environ.get("QWEN3TTS_BACKEND") or "auto").strip().lower()
    ufficiale = repo.startswith("Qwen/Qwen3-TTS-12Hz-")
    if scelto == "torch":
        return "torch"
    if not ufficiale:
        if scelto == "ggml":
            log(f"{repo}: variante non ufficiale, il runtime GGUF non la conosce -> torch")
        return "torch"
    if not _ggml_disponibile():
        if scelto == "ggml":
            log("backend ggml richiesto ma qwentts-cpp-python non e' installato -> torch")
        return "torch"
    if device != "cuda" and scelto == "auto":
        return "torch"
    return "ggml"


def _riferimento(voce: str) -> str | None:
    d = os.path.join(os.environ.get("KOKORO_MODEL_DIR", ""), "cloned_voices")
    for ext in CLONE_EXTS:
        p = os.path.join(d, voce + ext)
        if os.path.isfile(p):
            return p
    return None


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
        t = (whisper.load_model("small").transcribe(rif).get("text") or "").strip()
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


def prefetch() -> int:
    """Scarica (o verifica in cache) i pesi di entrambe le famiglie."""
    from huggingface_hub import snapshot_download
    device = os.environ.get("TTS_DEVICE") or "cuda"
    quant = (os.environ.get("QWEN3TTS_QUANT") or "Q8_0").strip()
    log(f"cache: {os.environ.get('HF_HUB_CACHE') or '(predefinita di Hugging Face)'}")
    for famiglia in ("clone", "preset"):
        repo = _repo(famiglia)
        if os.path.isdir(repo):
            log(f"{famiglia}: cartella locale {repo}, niente da scaricare")
            continue
        backend = _backend(repo, device)
        if backend == "ggml":
            from qwentts_cpp.models import resolve_gguf_paths
            talker, codec = resolve_gguf_paths(repo, quant=quant)
            mb = (talker.stat().st_size + codec.stat().st_size) / 2**20
            log(f"{famiglia}: {repo} GGUF {quant} pronto ({mb:,.0f} MB)")
        else:
            cartella = snapshot_download(repo)
            mb = sum(f.stat().st_size for f in __import__("pathlib").Path(cartella).rglob("*")
                     if f.is_file()) / 2**20
            log(f"{famiglia}: {repo} BF16 pronto ({mb:,.0f} MB)")
    return 0


def main() -> int:
    if "--prefetch" in sys.argv:
        if "--size" in sys.argv:
            os.environ["QWEN3TTS_SIZE"] = sys.argv[sys.argv.index("--size") + 1]
        return prefetch()
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
