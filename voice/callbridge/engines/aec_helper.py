"""Cancellazione d'eco per il PC, in un processo a parte.

Sul PC non c'è nessun AEC: `audio_hfp.py` apre un `sd.InputStream` grezzo, e la
configurazione lo rende strutturale — microfono VGM20 in ingresso, casse ST200
in uscita, due dispositivi distinti, quindi nessun cancellatore di driver può
funzionare perché il microfono non sa cosa esce dalle casse. Misurato: 9,3 dB
di attenuazione naturale a tutti i volumi, e a ogni livello lo STT trascrive la
frase dell'avatar per intero. In vivavoce l'avatar si interrompe da solo.

Il cancellatore è AEC3 di WebRTC, via `aec-audio-processing`, che ha ruote
Windows solo per cp311/312/313 mentre il bridge gira su 3.10. Di qui questo
helper: un processo nel suo venv (`aec-venv`), parlato in binario su stdio,
sullo stesso modello già usato per XTTS in `venv_tts.py`. Se muore, il
chiamante degrada al comportamento di oggi invece di far cadere il bridge.

Protocollo, deliberatamente stupido perché sta in un punto caldo: ogni
messaggio è `tipo(1B) + lunghezza(4B LE) + payload`, con l'audio in int16 mono
a 16 kHz e in blocchi da 10 ms (160 campioni), che è l'unità che AEC3 accetta.

    'r'  riferimento  ->  quello che esce dalle casse (nessuna risposta)
    'm'  microfono    ->  quello che entra; risposta 'c' col blocco ripulito
    'd'  ritardo      ->  int32 ms: quanto ci mette l'uscita a rientrare
    'q'  fine

Il ritardo lo misura il chiamante una volta per sessione: senza, AEC3 cerca
l'allineamento da sé e ci mette più tempo a convergere.

    aec-venv/Scripts/python.exe call-bridge/callbridge/engines/aec_helper.py
"""
from __future__ import annotations

import struct
import sys

import numpy as np

RATE = 16000
#: 10 ms: è il blocco di AEC3, non una scelta nostra.
BLOCCO = RATE // 100


def _leggi(n: int) -> bytes:
    """Legge esattamente n byte, o ritorna vuoto se lo stdin si chiude."""
    pezzi, restano = [], n
    while restano > 0:
        b = sys.stdin.buffer.read(restano)
        if not b:
            return b""
        pezzi.append(b)
        restano -= len(b)
    return b"".join(pezzi)


def main() -> int:
    from aec_audio_processing import AudioProcessor

    # NS e AGC accesi insieme all'AEC: il rumore di fondo è ciò che resta
    # quando l'eco se ne va, e su un microfono da scrivania si sente.
    ap = AudioProcessor(enable_aec=True, enable_ns=True, ns_level=2,
                        enable_agc=True, agc_mode=1, enable_vad=True)
    ap.set_stream_format(RATE, 1)
    ap.set_reverse_stream_format(RATE, 1)
    sys.stderr.write("aec: pronto\n")
    sys.stderr.flush()

    while True:
        testa = _leggi(5)
        if not testa:
            return 0
        tipo, n = testa[0:1], struct.unpack("<I", testa[1:5])[0]
        if tipo == b"q":
            return 0
        payload = _leggi(n) if n else b""
        if n and not payload:
            return 0
        if tipo == b"d":
            ap.set_stream_delay(int(struct.unpack("<i", payload[:4])[0]))
            continue
        if tipo == b"r":
            # Il riferimento va dato PRIMA del microfono corrispondente:
            # AEC3 costruisce il modello dell'eco su ciò che sa essere uscito.
            for i in range(0, len(payload) - 1, BLOCCO * 2):
                ap.process_reverse_stream(payload[i:i + BLOCCO * 2])
            continue
        if tipo == b"m":
            fuori = []
            for i in range(0, len(payload) - 1, BLOCCO * 2):
                fuori.append(ap.process_stream(payload[i:i + BLOCCO * 2]))
            pulito = b"".join(fuori)
            sys.stdout.buffer.write(b"c" + struct.pack("<I", len(pulito))
                                    + pulito)
            sys.stdout.buffer.flush()
            continue
        # Tipo sconosciuto: si ignora invece di morire. Un helper che cade
        # porta con sé il microfono di una conversazione in corso.
        sys.stderr.write(f"aec: tipo ignoto {tipo!r}\n")
        sys.stderr.flush()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"aec: {type(e).__name__}: {e}\n")
        raise SystemExit(1)
