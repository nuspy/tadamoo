#!/usr/bin/env python3
"""Helper TTS Kokoro in SUBPROCESS — serve solo a poter usare la GPU.

Perche' esiste: in-process Kokoro non puo' girare su CUDA. `torch` (Whisper/STT)
carica le PROPRIE cuDNN e Windows tiene una sola `cudnn64_9.dll` per processo:
onnxruntime-gpu, arrivando secondo, trova quelle sbagliate e o ripiega su CPU
(~0,5x realtime) o fallisce con CUDNN_STATUS_..._SUBLIBRARY_UNAVAILABLE. Qui
non c'e' torch, quindi onnxruntime ha le sue DLL e la sintesi va davvero su GPU
(misurato ~16x realtime contro ~0,5x su CPU).

Protocollo stdio identico agli altri helper (chatterbox/piper):
  stdin:  {"text": "...", "voice": "if_sara", "lang": "it", "speed": 1.0}\n
          {"cmd": "quit"}\n
  stdout: [u32 LE count][u32 LE sample_rate][count * f32 LE]
          count == 0  -> errore (dettagli su stderr)
  stderr: log ed errori

Env:
  KOKORO_MODEL_DIR   cartella con kokoro*.onnx + voices-v1.0.bin (obbligatoria)
  KOKORO_PRECISION   fp16 | fp32 | int8      (default fp16; su GPU usare fp16)
  KOKORO_DEVICE      cuda | cpu              (default cuda)

ATTENZIONE (gotcha gia' pagato con l'helper Chatterbox): qualunque `print()` di
una libreria finirebbe su stdout e CORROMPEREBBE il flusso binario. Qui stdout
viene protetto subito: il vero stdout e' tenuto da parte per il protocollo e
`sys.stdout` viene rimappato su stderr.
"""
from __future__ import annotations

import glob
import json
import os
import struct
import sys

# --- protezione dello stdout binario (PRIMA di importare qualunque libreria) ---
_BINARY_OUT = sys.stdout.buffer
sys.stdout = sys.stderr


def log(*a) -> None:
    print("[kokoro_tts_helper]", *a, file=sys.stderr, flush=True)


def _pick_onnx(model_dir: str, precision: str) -> str | None:
    """File .onnx per la precisione richiesta; ripiega su fp16/fp32/int8."""
    avail: dict[str, str] = {}
    for p in glob.glob(os.path.join(model_dir, "kokoro*.onnx")):
        low = os.path.basename(p).lower()
        if "fp16" in low:
            avail["fp16"] = p
        elif "int8" in low or "quant" in low:
            avail["int8"] = p
        else:
            avail["fp32"] = p
    for p in (precision, "fp16", "fp32", "int8"):
        if p in avail:
            return avail[p]
    return next(iter(avail.values()), None)


def _add_nvidia_dll_dirs() -> None:
    """Cartelle `bin` dei wheel NVIDIA nel PATH: cuDNN risolve le proprie
    sotto-librerie con LoadLibrary(nome), che guarda solo il PATH. Senza questo
    manca `cudnn_engines_tensor_ir64_9.dll` e onnxruntime ripiega su CPU."""
    import site
    basi = list(site.getsitepackages())
    try:
        basi.append(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    for base in basi:
        nvidia = os.path.join(base, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for pacchetto in os.listdir(nvidia):
            d = os.path.join(nvidia, pacchetto, "bin")
            if os.path.isdir(d) and d not in os.environ.get("PATH", ""):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(d)
                except (OSError, AttributeError):
                    pass


def _write(samples, sample_rate: int) -> None:
    """[u32 count][u32 sr][count*f32] sul vero stdout."""
    import numpy as np
    a = np.asarray(samples, dtype="<f4")
    _BINARY_OUT.write(struct.pack("<II", a.size, int(sample_rate)))
    if a.size:
        _BINARY_OUT.write(a.tobytes())
    _BINARY_OUT.flush()


def _write_error() -> None:
    _BINARY_OUT.write(struct.pack("<II", 0, 0))
    _BINARY_OUT.flush()


def main() -> int:
    model_dir = os.environ.get("KOKORO_MODEL_DIR", "").strip()
    precision = os.environ.get("KOKORO_PRECISION", "fp16").strip().lower()
    device = os.environ.get("KOKORO_DEVICE", "cuda").strip().lower()
    if not model_dir or not os.path.isdir(model_dir):
        log(f"KOKORO_MODEL_DIR non valida: {model_dir!r}")
        return 2

    onnx = _pick_onnx(model_dir, precision)
    voices = os.path.join(model_dir, "voices-v1.0.bin")
    if not onnx or not os.path.isfile(voices):
        log(f"modelli non trovati in {model_dir} (kokoro*.onnx + voices-v1.0.bin)")
        return 2

    if device == "cuda":
        _add_nvidia_dll_dirs()
        os.environ["ONNX_PROVIDER"] = "CUDAExecutionProvider"
        try:
            import onnxruntime as ort
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()
        except Exception as e:  # noqa: BLE001
            log(f"preload DLL CUDA fallito: {e}")
    else:
        os.environ["ONNX_PROVIDER"] = "CPUExecutionProvider"

    import time
    from kokoro_onnx import Kokoro
    t0 = time.perf_counter()
    kok = Kokoro(onnx, voices)
    # Provider REALI: se onnxruntime e' ripiegato su CPU va detto subito, non
    # scoperto dai tempi (e' il motivo per cui questo helper esiste).
    prov = []
    for attr in ("sess", "session", "_sess"):
        s = getattr(kok, attr, None)
        if s is not None and hasattr(s, "get_providers"):
            try:
                prov = list(s.get_providers())
            except Exception:  # noqa: BLE001
                pass
            break
    log(f"pronto: {os.path.basename(onnx)} in {time.perf_counter() - t0:.1f}s "
        f"provider={prov or '?'}")
    if device == "cuda" and prov and not prov[0].startswith("CUDA"):
        log("ATTENZIONE: CUDA richiesta ma onnxruntime usa " + prov[0])

    for riga in sys.stdin:
        riga = riga.strip()
        if not riga:
            continue
        try:
            req = json.loads(riga)
        except ValueError:
            log(f"richiesta non JSON: {riga[:80]!r}")
            _write_error()
            continue
        if req.get("cmd") == "quit":
            log("quit")
            return 0
        testo = (req.get("text") or "").strip()
        if not testo:
            _write(b"", 24000)
            continue
        try:
            samples, sr = kok.create(
                testo, voice=req.get("voice") or "if_sara",
                speed=float(req.get("speed") or 1.0),
                lang=req.get("lang") or "it")
            _write(samples, sr)
        except Exception as e:  # noqa: BLE001 - una sintesi fallita non uccide l'helper
            log(f"sintesi fallita per {testo[:60]!r}: {e}")
            _write_error()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
