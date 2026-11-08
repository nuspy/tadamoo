#!/usr/bin/env python3
"""Helper TTS CosyVoice 3 (Fun-CosyVoice3-0.5B) — cloning zero-shot.

Protocollo stdio identico agli altri helper del bridge (con "stream": true
l'audio esce a blocchi mentre genera, chiuso da [0xFFFFFFFF][0]):
  stdin:  {"text","voice","lang","speed"}\\n  |  {"cmd":"quit"}\\n
  stdout: [u32 LE count][u32 LE sample_rate][count * f32 LE]  (count==0 = errore)
  stderr: log

`voice` e' il nome di un riferimento in <models folder>/tts/voices/. La
modalita' zero-shot vuole anche la TRASCRIZIONE del riferimento: si legge da un
file `<voce>.txt` accanto al wav; se manca, la si ricava una volta con Whisper
e la si salva li'. Con una trascrizione sbagliata la sintesi degenera in
frammenti di pochi decimi di secondo, quindi non si inventa nulla.

Due trappole gia' pagate:
  - CosyVoice3 pretende il token <|endofprompt|> in coda al prompt_text: senza,
    l'LLM non emette token vocali e il vocoder fallisce con un oscuro
    "Kernel size can't be greater than actual input size";
  - torchaudio (load E save) passa da torchcodec, che su Windows non si carica:
    si sostituisce `torchaudio.load` con soundfile prima di importare cosyvoice.

Ambiente: venv `tts-venvs/cosyvoice` + repo `tts-venvs/cosyvoice-repo`.
`speed` non e' supportato: viene ignorato.
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

# Pesi: FunAudioLLM/Fun-CosyVoice3-0.5B-2512, in <models folder>/tts/cosyvoice-3/.
_BINARY_OUT = sys.stdout.buffer
sys.stdout = sys.stderr


def log(*a) -> None:
    print("[cosyvoice_helper]", *a, file=sys.stderr, flush=True)


TOKEN_FINE_PROMPT = "<|endofprompt|>"
# Lingue dichiarate: zh/en/ja/ko/de/es/fr/it/ru. L'ungherese NON c'e'.
LINGUE = {"zh", "en", "ja", "ko", "de", "es", "fr", "it", "ru"}

_modello = None
_sr = 24000


def _repo_dir() -> str:
    """Il codice di CosyVoice (non i pesi) sta accanto agli ambienti."""
    return os.path.join(os.environ.get("TTS_VENVS_DIR", ""), "cosyvoice-repo")


def _usa_soundfile() -> None:
    """torchaudio.load -> soundfile (torchcodec non si carica su Windows)."""
    import soundfile as sf
    import torch
    import torchaudio

    def load(uri, *a, **k):
        dati, sr = sf.read(str(uri), dtype="float32", always_2d=True)
        return torch.from_numpy(dati.T.copy()), sr

    torchaudio.load = load


def _riferimento(voce: str) -> str | None:
    return helper_models.voice_sample(voce)


def _trascrizione(rif: str) -> str:
    """Testo del riferimento: da `<rif>.txt`, altrimenti trascritto e salvato."""
    txt = os.path.splitext(rif)[0] + ".txt"
    if os.path.isfile(txt):
        with open(txt, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    log(f"trascrizione assente per {os.path.basename(rif)}: la ricavo con Whisper")
    import whisper
    m = whisper.load_model(helper_models.whisper_small())
    t = (m.transcribe(rif).get("text") or "").strip()
    if t:
        with open(txt, "w", encoding="utf-8") as f:
            f.write(t + "\n")
    return t


def _carica():
    global _modello, _sr
    if _modello is not None:
        return _modello
    dir_modello = helper_models.require("cosyvoice-3")
    repo = _repo_dir()
    sys.path.insert(0, repo)
    sys.path.insert(0, os.path.join(repo, "third_party", "Matcha-TTS"))
    _usa_soundfile()
    from cosyvoice.cli.cosyvoice import AutoModel
    _modello = AutoModel(model_dir=dir_modello, load_trt=False, fp16=False)
    _sr = _modello.sample_rate
    log(f"modello caricato (sr={_sr})")
    return _modello


def _scrivi(samples, sr: int) -> None:
    import numpy as np
    a = np.asarray(samples, dtype="<f4").ravel()
    _BINARY_OUT.write(struct.pack("<II", a.size, int(sr)))
    _BINARY_OUT.write(a.tobytes())
    _BINARY_OUT.flush()


def _errore() -> None:
    _BINARY_OUT.write(struct.pack("<II", 0, 0))
    _BINARY_OUT.flush()


# Streaming (richiesta con "stream": true): i blocchi di `inference_zero_shot(
# stream=True)` escono man mano, poi [0xFFFFFFFF][0].
_FINE_STREAM = 0xFFFFFFFF


def _fine_stream() -> None:
    _BINARY_OUT.write(struct.pack("<II", _FINE_STREAM, 0))
    _BINARY_OUT.flush()


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
        testo = (req.get("text") or "").strip()
        if not testo:
            _errore()
            continue
        lang = (req.get("lang") or "it").lower().split("-")[0]
        if lang not in LINGUE:
            log(f"lingua '{lang}' non supportata da CosyVoice")
            _errore()
            continue
        try:
            import torch
            m = _carica()
            rif = _riferimento(req.get("voice") or "")
            if rif is None:
                log(f"voce '{req.get('voice')}' senza riferimento clonato")
                _errore()
                continue
            prompt = _trascrizione(rif)
            if not prompt:
                log("trascrizione del riferimento vuota: sintesi non affidabile")
                _errore()
                continue
            if not prompt.endswith(TOKEN_FINE_PROMPT):
                prompt += TOKEN_FINE_PROMPT
            if req.get("stream"):
                # In streaming il controllo "durata plausibile" non si puo'
                # fare (l'audio e' gia' uscito): si accetta il primo tentativo.
                for o in m.inference_zero_shot(testo, prompt, rif, stream=True):
                    _scrivi(o["tts_speech"].squeeze().cpu().numpy(), _sr)
                _fine_stream()
                continue
            # Il campionamento ogni tanto degenera: si ritenta finche' la durata
            # non e' plausibile per la lunghezza del testo.
            minimo = len(testo) / 30.0
            audio = None
            for _ in range(3):
                pezzi = [o["tts_speech"] for o in
                         m.inference_zero_shot(testo, prompt, rif, stream=False)]
                audio = torch.cat(pezzi, dim=1) if pezzi else None
                if audio is not None and audio.shape[1] / _sr >= minimo:
                    break
                log("generazione degenere, ritento")
            if audio is None:
                _errore()
                continue
            _scrivi(audio.squeeze().cpu().numpy(), _sr)
        except Exception as e:  # noqa: BLE001
            log(f"sintesi fallita: {type(e).__name__}: {e}")
            _errore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
