#!/usr/bin/env python3
"""Helper TTS Dia 1.6B (Nari Labs) — dialogo, solo inglese.

Protocollo stdio identico agli altri helper del bridge:
  stdin:  {"text","voice","lang","speed"}\\n  |  {"cmd":"quit"}\\n
  stdout: [u32 LE count][u32 LE sample_rate][count * f32 LE]  (count==0 = errore)
  stderr: log

Dia e' un modello di DIALOGO: il testo va marcato coi turni `[S1]`/`[S2]` e
supporta marcatori non verbali fra parentesi — `(laughs)`, `(sighs)`,
`(clears throat)`. Se il testo non porta gia' un marcatore di turno, lo si
antepone.

Non ha voci predefinite: senza condizionamento il timbro cambia a ogni
generazione. Con una voce clonata in $KOKORO_MODEL_DIR/cloned_voices/ si usa il
riferimento come audio prompt, cosi' il timbro resta stabile.

Ambiente: venv `tts-venvs/dia` (torch cu128 + transformers + soundfile).
`speed` non e' supportato: viene ignorato.
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

_BINARY_OUT = sys.stdout.buffer
sys.stdout = sys.stderr


def log(*a) -> None:
    print("[dia_helper]", *a, file=sys.stderr, flush=True)


MODELLO = "nari-labs/Dia-1.6B-0626"
SR = 44100
CLONE_EXTS = (".wav", ".mp3", ".flac", ".ogg")
_TURNO = re.compile(r"^\s*\[S\d\]")

_proc = _modello = _device = None


def _riferimento(voce: str) -> str | None:
    if not voce:
        return None
    d = os.path.join(os.environ.get("KOKORO_MODEL_DIR", ""), "cloned_voices")
    for ext in CLONE_EXTS:
        p = os.path.join(d, voce + ext)
        if os.path.isfile(p):
            return p
    return None


def _carica():
    global _proc, _modello, _device
    if _modello is not None:
        return
    import torch
    from transformers import AutoProcessor, DiaForConditionalGeneration

    _device = os.environ.get("TTS_DEVICE", "cuda")
    if _device == "cuda" and not torch.cuda.is_available():
        log("CUDA non disponibile: si ripiega su CPU")
        _device = "cpu"
    _proc = AutoProcessor.from_pretrained(MODELLO)
    # device_map: i pesi vanno dal file al device un tensore alla volta, senza
    # costruire prima il modello da 1,6B in RAM.
    try:
        _modello = DiaForConditionalGeneration.from_pretrained(
            MODELLO, dtype=torch.float16, device_map=_device)
    except Exception as e:  # noqa: BLE001 - percorso classico
        log(f"device_map non riuscito ({type(e).__name__}: {e}): carico e poi sposto")
        _modello = DiaForConditionalGeneration.from_pretrained(
            MODELLO, dtype=torch.float16).to(_device)
    log(f"modello caricato su {_device}")


def _scrivi(samples, sr: int) -> None:
    import numpy as np
    a = np.asarray(samples, dtype="<f4").ravel()
    _BINARY_OUT.write(struct.pack("<II", a.size, int(sr)))
    _BINARY_OUT.write(a.tobytes())
    _BINARY_OUT.flush()


def _errore() -> None:
    _BINARY_OUT.write(struct.pack("<II", 0, 0))
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
        lang = (req.get("lang") or "en").lower().split("-")[0]
        if lang != "en":
            log(f"Dia e' solo inglese: lingua '{lang}' rifiutata")
            _errore()
            continue
        try:
            import torch
            _carica()
            if not _TURNO.match(testo):
                testo = f"[S1] {testo}"
            kwargs = {"text": [testo], "padding": True, "return_tensors": "pt"}
            rif = _riferimento(req.get("voice") or "")
            if rif is not None:
                # Audio prompt: tiene fermo il timbro fra una frase e l'altra.
                import soundfile as sf
                dati, sr_rif = sf.read(rif, dtype="float32", always_2d=False)
                if dati.ndim > 1:
                    dati = dati.mean(axis=1)
                if sr_rif != SR:
                    # Il feature extractor di Dia NON ricampiona: con un
                    # riferimento a 16 kHz (come Pluto.wav) solleva ValueError
                    # e ogni frase con voce clonata falliva.
                    from math import gcd

                    from scipy.signal import resample_poly
                    g = gcd(int(sr_rif), SR)
                    dati = resample_poly(dati, SR // g, int(sr_rif) // g).astype("float32")
                    sr_rif = SR
                kwargs["audio"] = [dati]
                kwargs["sampling_rate"] = sr_rif
            inputs = _proc(**kwargs).to(_device)
            with torch.inference_mode():
                out = _modello.generate(**inputs, max_new_tokens=3072,
                                        guidance_scale=3.0, temperature=1.8,
                                        top_p=0.90, top_k=45)
            onda = _proc.batch_decode(out)[0]
            _scrivi(onda, SR)
        except Exception as e:  # noqa: BLE001
            log(f"sintesi fallita: {type(e).__name__}: {e}")
            _errore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
