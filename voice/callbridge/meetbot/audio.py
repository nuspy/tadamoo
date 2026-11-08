"""Backend audio del meetbot.

v1 — WindowsLoopbackAudio: il browser della riunione suona sulle casse del
PC; si cattura il loopback del device di uscita di default (stesso pattern
del copilota) e lo si manda allo STT a 16 kHz. Limite strutturale: UNA
riunione alla volta e niente altro audio sul PC durante la cattura.

Futuro — PulseAudioBot (WSL): sink/sorgenti PulseAudio dedicati per bot,
piu' riunioni in parallelo e iniezione TTS su microfono virtuale (vedi
MeetsAPI.md fase 2): per ora stub.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from ..audio_hfp import resample
from ..stt_client import STT_RATE

log = logging.getLogger("callbridge.meetbot.audio")


class WindowsLoopbackAudio:
    """Cattura il loopback dell'uscita di default -> stt.send_audio (16k)."""

    def __init__(self, samplerate: int, stt) -> None:
        self._rate = samplerate
        self._stt = stt
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._capture, name="meetbot-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _capture(self) -> None:
        import soundcard as sc

        try:
            spk = sc.default_speaker()
            mic = sc.get_microphone(str(spk.name), include_loopback=True)
        except Exception as e:  # noqa: BLE001
            log.error("loopback non disponibile: %s", e)
            return
        block = int(self._rate * 0.1)
        log.info("cattura loopback da '%s' (%d Hz)", spk.name, self._rate)
        try:
            with mic.recorder(samplerate=self._rate, channels=1) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=block)
                    mono = data[:, 0] if data.ndim > 1 else data
                    pcm = mono.astype(np.float32)
                    if self._rate != STT_RATE:
                        pcm = resample(pcm, self._rate, STT_RATE)
                    self._stt.send_audio(pcm)
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                log.error("cattura loopback interrotta: %s", e)


class PulseAudioBot:
    """Stub del backend WSL/PulseAudio multi-bot (fase futura)."""

    def __init__(self, *a, **kw) -> None:
        raise NotImplementedError(
            "backend 'wsl' non ancora implementato: usa meetbot_backend='windows'")
