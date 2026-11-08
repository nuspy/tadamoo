"""TTS Piper (in-process): lingue che Kokoro non copre (es. Ungherese hu_HU).

Piper (rhasspy/OHF-Voice) e' un TTS neurale ONNX veloce su CPU con 35+ lingue.
Qui serve soprattutto per le lingue assenti da Kokoro (Ungherese, Polacco,
Ceco, ...). Ogni voce e' un file `<piper_dir>/<voce>.onnx` (+ `.onnx.json`)
scaricato da huggingface.co/rhasspy/piper-voices; il nome voce e' il basename
(es. `hu_HU-anna-medium`). La fonetizzazione usa espeak-ng (dati inclusi nel
pacchetto piper-tts).

`PiperTtsEngine.synth()` ha la STESSA firma di `KokoroTtsEngine.synth()`, cosi'
il `TtsRouter` lo smista in modo trasparente per /tts/test, /tts/stream e l'avatar.
"""

from __future__ import annotations

import glob
import logging
import os
import threading

import numpy as np

from .. import models_store

log = logging.getLogger("callbridge.engines.piper")


def _default_piper_dir(kokoro_dir: str) -> str:
    """Cartella voci Piper: quella della cartella dei modelli vocali."""
    return models_store.piper_voices()


class PiperTtsEngine:
    """Voci Piper caricate pigramente e tenute in cache (una per nome voce)."""

    def __init__(self, piper_dir: str, device: str = "cpu",
                 memory: str = "dedicated") -> None:
        self.model_dir = piper_dir
        self.device = device
        self.memory = memory
        self._voices: dict[str, object] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self._list_files())

    def _list_files(self) -> list[str]:
        try:
            return sorted(glob.glob(os.path.join(self.model_dir, "*.onnx")))
        except OSError:
            return []

    def list_voices(self) -> list[str]:
        """Nomi delle voci Piper presenti (basename senza .onnx)."""
        return [os.path.splitext(os.path.basename(p))[0] for p in self._list_files()]

    def has_voice(self, voice: str) -> bool:
        if not voice:
            return False
        return os.path.isfile(os.path.join(self.model_dir, voice + ".onnx"))

    def _get_voice(self, voice: str):
        with self._lock:
            v = self._voices.get(voice)
            if v is None:
                path = os.path.join(self.model_dir, voice + ".onnx")
                if not os.path.isfile(path):
                    raise models_store.ModelNotInstalled("piper", "tts", f"voice {voice}")
                from .device import ensure_ort_cuda, resolve_device
                from piper import PiperVoice
                eff = resolve_device(self.device, self.memory)
                if eff == "cuda":
                    ensure_ort_cuda()
                v = PiperVoice.load(path, use_cuda=(eff == "cuda"))
                self._voices[voice] = v
                log.info("Piper: voce caricata %s su %s (sr=%d)", voice,
                         eff.upper(), v.config.sample_rate)
            return v

    def load(self) -> None:  # simmetria con gli altri engine; il load e' pigro
        pass

    def synth(self, text: str, voice: str, lang: str, speed: float):
        """Sintetizza `text` con la voce Piper `voice` -> (float32 mono, sr).

        `lang` e' implicito nella voce (hu_HU-... e' ungherese). `speed` mappa su
        length_scale (durata): length_scale = 1/speed (piu' alto = piu' lento)."""
        from piper import SynthesisConfig
        text = (text or "").strip()
        v = self._get_voice(voice)
        sr = int(v.config.sample_rate)
        if not text:
            return np.zeros(0, dtype=np.float32), sr
        length_scale = 1.0 / float(speed) if speed and speed > 0 else 1.0
        cfg = SynthesisConfig(length_scale=length_scale)
        parts = [c.audio_int16_array for c in v.synthesize(text, cfg)]
        if not parts:
            return np.zeros(0, dtype=np.float32), sr
        audio = np.concatenate(parts).astype(np.float32) / 32768.0
        return audio, sr
