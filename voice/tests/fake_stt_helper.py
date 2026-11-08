"""Finto `dictation_stt_helper` per i test: stesso protocollo, nessun modello.

Risponde "ready" con cio' che ha ricevuto in env (modello/device/precisione), e
trascrive restituendo `testo-<n campioni>` dopo un ritardo opzionale
(FAKE_STT_DELAY, in secondi), cosi' i test vedono la serializzazione.
"""
import json
import os
import sys
import time

OUT = sys.stdout.buffer
IN = sys.stdin.buffer


def reply(o):
    OUT.write((json.dumps(o) + "\n").encode())
    OUT.flush()


def precisione():
    c = os.environ.get("DICT_STT_COMPUTE", "auto").lower()
    cuda = os.environ.get("DICT_STT_DEVICE", "cuda") == "cuda"
    return "float16" if cuda and "float32" not in c else "float32"


while True:
    riga = IN.readline()
    if not riga:
        break
    req = json.loads(riga)
    cmd = req.get("cmd")
    if cmd == "quit":
        break
    if cmd == "load":
        reply({"ok": True, "event": "ready", "model": os.environ.get("DICT_STT_MODEL"),
               "device": os.environ.get("DICT_STT_DEVICE"), "precision": precisione(),
               "vram_mb": 1, "load_s": 0.0})
    elif cmd in ("transcribe", "transcribe_detailed"):
        n = int(req["n"])
        got = 0
        while got < n * 4:
            b = IN.read(n * 4 - got)
            if not b:
                sys.exit(0)
            got += len(b)
        time.sleep(float(os.environ.get("FAKE_STT_DELAY", "0")))
        out = {"ok": True, "text": f" testo-{n}", "ms": 1}
        if cmd == "transcribe_detailed":
            out["segments"] = [{"start_s": 0.0, "end_s": 1.0, "text": f"testo-{n}",
                                **({"words": [{"start_s": 0.0, "end_s": 1.0, "text": "w"}]}
                                   if req.get("words") else {})}]
        reply(out)
    else:
        reply({"ok": False, "error": f"comando sconosciuto: {cmd!r}"})
