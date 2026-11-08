#!/usr/bin/env python3
"""Helper TTS Orpheus 3B (unsloth/orpheus-3b-0.1-ft) — solo inglese.

Protocollo stdio identico agli altri helper del bridge:
  stdin:  {"text","voice","lang","speed"}\\n  |  {"cmd":"quit"}\\n
  stdout: [u32 LE count][u32 LE sample_rate][count * f32 LE]  (count==0 = errore)
  stderr: log

Orpheus e' un LLM llama che emette token audio SNAC (24 kHz): si genera col
prompt "<voce>: <testo>" fra i token speciali del modello e si decodifica coi 3
livelli del codec. E' l'unico motore del parco con TAG DI ESPRESSIVITA'
conversazionale — <laugh>, <chuckle>, <sigh>, <gasp>, <yawn> — che vanno
lasciati nel testo cosi' come sono.

Voci: tara/leah/jess/mia/zoe (femminili), leo/dan/zac (maschili).
`speed` non e' supportato: viene ignorato.

Il repo ufficiale canopylabs e' "gated" (403 senza accettare i termini con un
account HF): si usa il mirror di unsloth, stesso modello, aperto.
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

_BINARY_OUT = sys.stdout.buffer
sys.stdout = sys.stderr


def log(*a) -> None:
    print("[orpheus_helper]", *a, file=sys.stderr, flush=True)


# Pesi: unsloth/orpheus-3b-0.1-ft e il codec hubertsiuzdak/snac_24khz (cartella
# snac_24khz/), in <models folder>/tts/<id>/ (id del catalogo vocale).
MODELLO = "orpheus-3b"
SR = 24000
VOCI = {"tara", "leah", "jess", "mia", "zoe", "leo", "dan", "zac"}

# Token speciali del formato Orpheus (dal repo CanopyAI/Orpheus-TTS).
INIZIO_UMANO, FINE_TESTO, FINE_UMANO = 128259, 128009, 128260
INIZIO_AUDIO, FINE_GENERAZIONE = 128257, 128258
OFFSET_AUDIO = 128266          # i token audio partono da qui

_tok = _modello = _snac = _device = None


def _carica():
    global _tok, _modello, _snac, _device
    if _modello is not None:
        return
    cartella = helper_models.require(MODELLO)
    cartella_snac = helper_models.require(MODELLO, "snac_24khz")
    import torch
    from snac import SNAC
    from transformers import AutoModelForCausalLM, AutoTokenizer

    _device = os.environ.get("TTS_DEVICE", "cuda")
    if _device == "cuda" and not torch.cuda.is_available():
        log("CUDA non disponibile: si ripiega su CPU")
        _device = "cpu"
    _tok = AutoTokenizer.from_pretrained(cartella, local_files_only=True)
    # device_map: i pesi (safetensors) vanno dal file al device un tensore alla
    # volta, senza costruire prima il modello da 3B in RAM (~6 GB in bf16).
    try:
        _modello = AutoModelForCausalLM.from_pretrained(
            cartella, dtype=torch.bfloat16, device_map=_device,
            local_files_only=True)
    except Exception as e:  # noqa: BLE001 - percorso classico
        log(f"device_map non riuscito ({type(e).__name__}: {e}): carico e poi sposto")
        _modello = AutoModelForCausalLM.from_pretrained(
            cartella, dtype=torch.bfloat16, local_files_only=True).to(_device)
    _snac = SNAC.from_pretrained(cartella_snac).eval().to(_device)
    log(f"modelli caricati su {_device}")


def _decodifica(codici):
    """Token audio Orpheus -> forma d'onda, via i 3 livelli del codec SNAC."""
    import torch
    if len(codici) < 7:
        return None
    codici = codici[: (len(codici) // 7) * 7]
    l1, l2, l3 = [], [], []
    for i in range(len(codici) // 7):
        b = codici[7 * i: 7 * i + 7]
        l1.append(b[0])
        l2.extend([b[1] - 4096, b[4] - 4 * 4096])
        l3.extend([b[2] - 2 * 4096, b[3] - 3 * 4096,
                   b[5] - 5 * 4096, b[6] - 6 * 4096])
    livelli = [torch.tensor(x, device=_device).unsqueeze(0) for x in (l1, l2, l3)]
    if any((lv < 0).any() or (lv > 4095).any() for lv in livelli):
        return None            # token fuori range: generazione degenerata
    with torch.inference_mode():
        return _snac.decode(livelli)


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
            log(f"Orpheus e' solo inglese: lingua '{lang}' rifiutata")
            _errore()
            continue
        try:
            import torch
            _carica()
            voce = (req.get("voice") or "tara").strip().lower()
            if voce not in VOCI:
                log(f"voce '{voce}' sconosciuta: uso 'tara'")
                voce = "tara"
            ids = _tok(f"{voce}: {testo}", return_tensors="pt").input_ids
            ids = torch.cat([
                torch.tensor([[INIZIO_UMANO]]), ids,
                torch.tensor([[FINE_TESTO, FINE_UMANO]])], dim=1).to(_device)
            with torch.inference_mode():
                out = _modello.generate(
                    ids, max_new_tokens=3500, do_sample=True,
                    temperature=0.6, top_p=0.95, repetition_penalty=1.1,
                    eos_token_id=FINE_GENERAZIONE,
                    attention_mask=torch.ones_like(ids))
            gen = out[0][ids.shape[1]:].tolist()
            if INIZIO_AUDIO in gen:
                gen = gen[gen.index(INIZIO_AUDIO) + 1:]
            audio = _decodifica([t - OFFSET_AUDIO for t in gen
                                 if t >= OFFSET_AUDIO])
            if audio is None:
                log("nessun audio valido nella generazione")
                _errore()
                continue
            _scrivi(audio.squeeze().float().cpu().numpy(), SR)
        except Exception as e:  # noqa: BLE001
            log(f"sintesi fallita: {type(e).__name__}: {e}")
            _errore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
