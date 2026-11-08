"""Conversione audio per il modem GSM a file (SIM800C): PCM <-> AMR-NB.

Il SIM800C registra/riproduce audio in **AMR-NB** (8 kHz). La pipeline interna
usa PCM float32 mono (STT a 16 kHz, TTS a 24 kHz). Qui facciamo da ponte via
ffmpeg (encoder/decoder `libopencore_amrnb`):
  - TTS  : PCM f32 (qualsiasi sr) -> AMR-NB 8k  (da caricare nel modulo, CMEDPLAY)
  - CREC : AMR-NB (dal modulo)    -> PCM f32 16k (da dare allo STT Handy)
"""
from __future__ import annotations

import logging
import subprocess

import numpy as np

log = logging.getLogger("callbridge.gsm.audio")

FFMPEG = "ffmpeg"


def pcm_to_amr(samples: np.ndarray, sample_rate: int, bitrate: str = "12.2k") -> bytes:
    """Codifica PCM float32 mono in un file AMR-NB (8 kHz) in memoria."""
    raw = np.ascontiguousarray(samples, dtype="<f4").tobytes()
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "-",
        "-c:a", "libopencore_amrnb", "-ar", "8000", "-ac", "1", "-b:a", bitrate,
        "-f", "amr", "-",
    ]
    try:
        p = subprocess.run(cmd, input=raw, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("ffmpeg pcm->amr in timeout") from e
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg pcm->amr fallito: {p.stderr.decode('utf-8', 'replace')}")
    return p.stdout


def amr_to_pcm(amr: bytes, out_rate: int = 16000) -> np.ndarray:
    """Decodifica un file AMR-NB in PCM float32 mono a `out_rate` (default 16k STT)."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "amr", "-i", "-",
        "-f", "f32le", "-ar", str(out_rate), "-ac", "1", "-",
    ]
    try:
        p = subprocess.run(cmd, input=amr, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("ffmpeg amr->pcm in timeout") from e
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg amr->pcm fallito: {p.stderr.decode('utf-8', 'replace')}")
    return np.frombuffer(p.stdout, dtype="<f4").copy()


def dvi_to_pcm(dvi: bytes, in_rate: int = 8000, out_rate: int = 16000) -> np.ndarray:
    """Decodifica una registrazione CREC `C:\\REC\\<id>.DVI` in PCM float32 mono.

    Il SIM800C registra in **DVI/IMA ADPCM** 4-bit (8 kHz, headerless, stato
    continuo) — non AMR. `audioop.adpcm2lin` lo decodifica in PCM 16-bit; poi
    si ricampiona a `out_rate` (default 16k per lo STT Handy).
    """
    import audioop  # deprecato in 3.13+, presente in Python 3.10 dell'orchestratore

    lin16, _ = audioop.adpcm2lin(dvi, 2, None)          # -> PCM s16 @ in_rate
    if out_rate != in_rate:
        lin16, _ = audioop.ratecv(lin16, 2, 1, in_rate, out_rate, None)
    pcm = np.frombuffer(lin16, dtype="<i2").astype("f4") / 32768.0
    return pcm


def ffmpeg_available() -> bool:
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
        return True
    except Exception:  # noqa: BLE001
        return False
