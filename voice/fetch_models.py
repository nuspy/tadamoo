"""Scarica i modelli che callAPIcall non porta con sé (~700 MB).

Stanno fuori dall'installazione — in `%LOCALAPPDATA%\\callAPIcall\\models` — per
due motivi: pesano troppo per un installer, e così sopravvivono ad
aggiornamenti e disinstallazioni (riscaricarne 700 MB per un aggiornamento da
20 MB sarebbe assurdo).

    python fetch_models.py                    # tutto, nella cartella standard
    python fetch_models.py --only diarization # solo un gruppo
    python fetch_models.py --whisper large-v3 # STT più accurato (~2,9 GB)
    python fetch_models.py --whisper none     # niente STT (lo scarica al 1o uso)
    python fetch_models.py --dest D:\\modelli  # altrove (poi aggiorna config.json)

Idempotente: ciò che c'è già non viene riscaricato. Un download interrotto
lascia un `.part` e viene ripreso da capo al giro dopo, mai lasciato a metà con
il nome definitivo.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

# Kokoro (TTS): release ufficiale di kokoro-onnx.
KOKORO_BASE = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
               "model-files-v1.0")
KOKORO_FILES = ("kokoro-v1.0.fp16.onnx", "voices-v1.0.bin")

# Diarizzazione (chi parla quando): segmentazione + impronta vocale, ONNX.
DIAR_SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                "speaker-segmentation-models/"
                "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
DIAR_EMB_BASE = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                 "speaker-recongition-models")     # il typo è nel nome della release
DIAR_EMB = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
DIAR_EMB_ALT = "nemo_en_titanet_large.onnx"        # solo con --titanet (101 MB)

# VAD: sta nel repo/installazione, non si scarica.
SILERO = "silero_vad_v4.onnx"

GRUPPI = ("kokoro", "diarization", "vad", "whisper")


def dest_default() -> Path:
    return Path(os.path.expandvars(r"%LOCALAPPDATA%\callAPIcall\models"))


def _scarica(url: str, dst: Path) -> bool:
    """True se ha scaricato, False se c'era già. Passa da un `.part`: un file
    troncato da una connessione caduta è peggio di un file assente, perché al
    caricamento sembra un modello corrotto."""
    if dst.exists():
        print(f"  già presente: {dst.name}")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    print(f"  scarico {dst.name} ...", flush=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)          # noqa: S310 - URL fissi, https
    tmp.replace(dst)
    print(f"  ok: {dst.name} ({dst.stat().st_size / 1e6:.0f} MB)")
    return True


def _da_handy(nome: str, dst: Path) -> bool:
    """Kokoro potrebbe essere già sul disco: Handy usa gli stessi file."""
    src = Path(os.path.expandvars(r"%APPDATA%\com.pais.handy\models")) / nome
    if dst.exists() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    print(f"  copiato da Handy: {nome}")
    return True


def kokoro(dest: Path) -> None:
    print("Kokoro (voce sintetica) — ~340 MB")
    for nome in KOKORO_FILES:
        dst = dest / nome
        if not _da_handy(nome, dst):
            _scarica(f"{KOKORO_BASE}/{nome}", dst)


def diarization(dest: Path, titanet: bool = False) -> None:
    print("Diarizzazione (chi parla quando) — ~35 MB")
    root = dest / "diarization"
    seg = root / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    if seg.is_file():
        print("  già presente: segmentazione pyannote")
    else:
        archivio = root / "seg.tar.bz2"
        _scarica(DIAR_SEG_URL, archivio)
        with tarfile.open(archivio, "r:bz2") as tar:
            tar.extractall(root)                   # noqa: S202 - archivio ufficiale
        archivio.unlink(missing_ok=True)
        print("  ok: segmentazione pyannote")
    _scarica(f"{DIAR_EMB_BASE}/{DIAR_EMB}", root / DIAR_EMB)
    if titanet:
        _scarica(f"{DIAR_EMB_BASE}/{DIAR_EMB_ALT}", root / DIAR_EMB_ALT)


def vad(dest: Path) -> None:
    """Il VAD viaggia con il codice: qui si copia soltanto dove lo cerca il
    motore STT, se non c'è già."""
    print("VAD (silero)")
    dst = dest / SILERO
    if dst.exists():
        print(f"  già presente: {SILERO}")
        return
    src = Path(__file__).resolve().parent / "models" / SILERO
    if not src.is_file():
        print(f"  ! {SILERO} non trovato accanto al codice: lo cercherà il motore "
              f"nei percorsi noti")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    print(f"  ok: {SILERO}")


def whisper(nome: str) -> None:
    """Pre-carica il modello STT nella cache di whisper (~/.cache/whisper).

    Non finisce in `dest`: la cache è di whisper e la condivide con qualunque
    altro programma lo usi. Farlo ora evita che la prima trascrizione si fermi
    per minuti a scaricare."""
    if nome == "none":
        print("STT: preload saltato (verrà scaricato al primo uso)")
        return
    print(f"Whisper '{nome}' (trascrizione) — può richiedere qualche minuto")
    try:
        import whisper as _w
    except ImportError:
        print("  ! pacchetto `openai-whisper` non installato: salto")
        return
    if nome not in _w._MODELS:
        print(f"  ! '{nome}' non è un modello whisper. Disponibili: "
              f"{', '.join(_w._MODELS)}")
        return
    _w._download(_w._MODELS[nome], os.path.expanduser("~/.cache/whisper"), False)
    print(f"  ok: whisper '{nome}' in cache")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, default=dest_default(),
                    help="cartella modelli (default: %%LOCALAPPDATA%%\\callAPIcall\\models)")
    ap.add_argument("--only", default="", help=f"sottoinsieme: {','.join(GRUPPI)}")
    ap.add_argument("--whisper", default="small",
                    help="modello STT da pre-caricare: small | medium | large-v3 | none")
    ap.add_argument("--titanet", action="store_true",
                    help="aggiunge il 2o modello di embedding (101 MB, solo per confronti)")
    args = ap.parse_args()

    gruppi = [g.strip() for g in args.only.split(",") if g.strip()] or list(GRUPPI)
    sconosciuti = [g for g in gruppi if g not in GRUPPI]
    if sconosciuti:
        print(f"gruppo sconosciuto: {', '.join(sconosciuti)} (validi: {', '.join(GRUPPI)})")
        return 2

    dest = args.dest.resolve()
    print(f"Modelli in {dest}\n")
    if "kokoro" in gruppi:
        kokoro(dest)
    if "diarization" in gruppi:
        diarization(dest, args.titanet)
    if "vad" in gruppi:
        vad(dest)
    if "whisper" in gruppi:
        whisper(args.whisper)
    if {"kokoro", "diarization", "vad"} & set(gruppi):
        print(f"\nFatto. In config.json `kokoro_model_dir` e "
              f"`diarization_model_dir` devono puntare qui:\n  {dest}")
    else:
        print("\nFatto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
