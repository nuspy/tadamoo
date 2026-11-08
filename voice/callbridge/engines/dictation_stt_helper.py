#!/usr/bin/env python3
"""L'UNICO STT del sistema, in un processo suo (whisper).

Lo usano la dettatura (Alt+Spazio), le sessioni voce, il push-to-talk e
/stt/stream del bridge, tutti attraverso `engines/stt_shared.py`: un solo
modello in memoria, una richiesta alla volta.

Perche' un processo a parte: in-process whisper divideva il GIL con tutto il
bridge (server HTTP, discovery del telefono, avatar, copilota...). Il decoding
di whisper e' un ciclo Python che rilascia e riprende il GIL a ogni operazione
sulla GPU: basta UN thread Python occupato nello stesso processo per farlo
crollare. Misurato: 30 s di audio in 2,96 s da solo, oltre 8 MINUTI con un
solo thread CPU-bound accanto. In un processo proprio la trascrizione non
dipende da cosa sta facendo il resto del sistema.

Protocollo (stdin -> stdout, una risposta JSON per riga per ogni richiesta):
  {"cmd": "load"}\\n                          -> {"ok", "event": "ready", ...}
  {"cmd": "transcribe", "lang": "it", "n": N}\\n + N float32 LE (16 kHz mono)
                                              -> {"ok", "text", "ms"}
  {"cmd": "transcribe_detailed", "lang", "n", "words": bool}\\n + N float32 LE
                                              -> {"ok", "text", "segments", "ms"}
  {"cmd": "quit"}\\n  oppure EOF (bridge morto) -> uscita
Errori: {"ok": false, "error": "..."}. Log su stderr.

Env: DICT_STT_MODEL, DICT_STT_DEVICE, DICT_STT_MEMORY, DICT_STT_COMPUTE.
"""
from __future__ import annotations

import json
import os
import sys
import time

# Prima di importare torch: la memoria liberata torna subito al sistema (vedi
# callbridge/memoria.py). Il processo parte da `-m callbridge...`, quindi il
# pacchetto la imposta gia'; qui resta per chi lancia l'helper da solo.
os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")

# Protezione dello stdout del protocollo PRIMA di importare librerie: un loro
# print() finirebbe nel flusso e lo corromperebbe (gotcha gia' pagato con
# l'helper Chatterbox).
_OUT = sys.stdout.buffer
sys.stdout = sys.stderr

import logging  # noqa: E402

import numpy as np  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("dictation_stt_helper")


def _reply(obj: dict) -> None:
    _OUT.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    _OUT.flush()


def _read_exact(pipe, n: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got < n:
        b = pipe.read(n - got)
        if not b:
            break
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


def _vram_mb() -> int | None:
    try:
        import torch
        if torch.cuda.is_available():
            return int(torch.cuda.memory_reserved() / (1024 * 1024))
    except Exception:  # noqa: BLE001
        pass
    return None


def _nuovo_motore(model, device, memory, compute):
    from callbridge.engines.whisper_stt import WhisperSttEngine

    # single_thread=False: qui c'e' un solo thread, quindi le cache cuDNN
    # (thread-local) restano calde senza bisogno del thread dedicato.
    return WhisperSttEngine(model or "medium", device or "cuda",
                            memory or "dedicated", compute or "auto",
                            single_thread=False)


def _carica_e_riscalda(engine) -> dict:
    t0 = time.perf_counter()
    engine.load()
    # Riscaldamento: la PRIMA trascrizione costruisce i piani cuDNN
    # e costava 17 s per 22 s di audio; le successive 11-13x
    # realtime. Lo si paga qui, non alla prima dettatura vera.
    engine.transcribe(np.zeros(16000, dtype=np.float32), "it")
    return {"ok": True, "event": "ready", "model": engine.model_name,
            "device": engine.device, "precision": engine.precision,
            "vram_mb": _vram_mb(),
            "load_s": round(time.perf_counter() - t0, 1)}


def main() -> int:
    engine = _nuovo_motore(
        os.environ.get("DICT_STT_MODEL"),
        os.environ.get("DICT_STT_DEVICE"),
        os.environ.get("DICT_STT_MEMORY"),
        os.environ.get("DICT_STT_COMPUTE"))
    stdin = sys.stdin.buffer

    while True:
        line = stdin.readline()
        if not line:
            return 0                       # EOF: il bridge e' andato via
        try:
            req = json.loads(line.decode("utf-8"))
        except ValueError:
            _reply({"ok": False, "error": "richiesta non JSON"})
            continue
        cmd = req.get("cmd")
        if cmd == "quit":
            return 0
        if cmd == "load":
            try:
                _reply(_carica_e_riscalda(engine))
            except Exception as e:  # noqa: BLE001
                log.exception("caricamento fallito")
                _reply({"ok": False, "error": f"caricamento fallito: {e}"})
            continue
        if cmd in ("transcribe", "transcribe_detailed"):
            n = int(req.get("n") or 0)
            data = _read_exact(stdin, n * 4)
            if len(data) < n * 4:
                return 0                   # pipe chiusa a meta': il bridge e' morto
            try:
                audio = np.frombuffer(data, dtype="<f4").astype(np.float32)
                t0 = time.perf_counter()
                if cmd == "transcribe":
                    out = {"text": engine.transcribe(audio, req.get("lang") or None)}
                else:
                    out = engine.transcribe_detailed(
                        audio, req.get("lang") or None, bool(req.get("words")))
                _reply({"ok": True, **out,
                        "ms": int((time.perf_counter() - t0) * 1000)})
            except Exception as e:  # noqa: BLE001
                log.exception("trascrizione fallita")
                _reply({"ok": False, "error": f"trascrizione fallita: {e}"})
            continue
        _reply({"ok": False, "error": f"comando sconosciuto: {cmd!r}"})


if __name__ == "__main__":
    sys.exit(main())
