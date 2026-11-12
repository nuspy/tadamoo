"""Speech-to-text backends for scoring how a voice reads a word: the same interface for the voice service's Whisper, a local Whisper large-v3 and character-level CTC models of Hungarian.

They are used by the optimizer and by the listening page's measurements, not by a production (the production's read-back uses the voice service alone, voicecheck.py). Several
different models are used on purpose: a Whisper has a language model that can put the intended word in the transcript even when it was said badly, a CTC model has none and writes
what it heard, letter by letter. `load_all` gives whichever of them are installed. The models live outside the repository (`MODELS`)."""
from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

MODELS = Path(r"E:\Modelli\basalt\asr-hu")
TARGET_RATE = 16000


def pcm16k(wav: bytes) -> Any:
    """The recording as a float32 mono array at 16 kHz."""
    import numpy as np
    from scipy.signal import resample_poly
    with wave.open(io.BytesIO(wav)) as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype={1: np.int8, 2: np.int16, 4: np.int32}[width]).astype(np.float32) / float(2 ** (8 * width - 1))
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE:
        from math import gcd
        g = gcd(rate, TARGET_RATE)
        data = resample_poly(data, TARGET_RATE // g, rate // g).astype(np.float32)
    return data


class Asr:
    name = "asr"
    kind = "whisper"                               # "whisper" has a language model, "ctc" has none

    def transcribe(self, wav: bytes, lang: str = "hu") -> str:
        raise NotImplementedError


class ServiceWhisper(Asr):
    """The voice service's own Whisper through the server (`/transcribe`)."""
    kind = "whisper"

    def __init__(self, url: str = "http://127.0.0.1:7232", label: str = "whisper-medium (voice service)") -> None:
        self.url, self.name = url, label

    def transcribe(self, wav: bytes, lang: str = "hu") -> str:
        from tadamoo.speech import voicecheck
        return " ".join(str(w.get("text", "")).strip() for w in voicecheck.transcribe(self.url, wav, lang)).strip()


class WhisperLocal(Asr):
    """A Whisper from a local folder, on the GPU."""
    kind = "whisper"

    def __init__(self, folder: Path, label: str, device: str = "cuda:0") -> None:
        import torch
        from transformers import pipeline
        self.name = label
        self.pipe = pipeline("automatic-speech-recognition", model=str(folder), dtype=torch.float16 if device != "cpu" else torch.float32, device=device)

    def transcribe(self, wav: bytes, lang: str = "hu") -> str:
        out = self.pipe({"raw": pcm16k(wav), "sampling_rate": TARGET_RATE}, generate_kwargs={"language": {"hu": "hungarian", "it": "italian", "en": "english"}.get(lang, lang), "task": "transcribe"})
        return str(out["text"]).strip()


class Ctc(Asr):
    """A wav2vec2 CTC model of Hungarian: greedy letters, no language model."""
    kind = "ctc"

    def __init__(self, folder: Path, label: str, device: str = "cuda:0") -> None:
        import torch
        from transformers import AutoModelForCTC, AutoProcessor
        self.name, self._torch, self.device = label, torch, device
        self.dtype = torch.float16 if device != "cpu" else torch.float32
        self.processor = AutoProcessor.from_pretrained(str(folder))
        self.model = AutoModelForCTC.from_pretrained(str(folder), dtype=self.dtype).to(device).eval()

    def transcribe(self, wav: bytes, lang: str = "hu") -> str:
        torch = self._torch
        inputs = self.processor(pcm16k(wav), sampling_rate=TARGET_RATE, return_tensors="pt")
        with torch.no_grad():
            logits = self.model(inputs.input_values.to(self.device, self.dtype)).logits
        return str(self.processor.batch_decode(torch.argmax(logits, dim=-1))[0]).strip()


def load_all(url: str = "http://127.0.0.1:7232", root: Path = MODELS, device: str = "cuda:0") -> List[Asr]:
    """Every backend that is installed: the voice service's Whisper, Whisper large-v3, and the Hungarian CTC models found under `root`."""
    out: List[Asr] = [ServiceWhisper(url)]
    spec: Dict[str, Any] = {"whisper-large-v3": (WhisperLocal, "whisper-large-v3"), "mms-1b-hungarian": (Ctc, "mms-1b-hungarian (CTC)"), "xlsr53-hungarian": (Ctc, "xlsr53-hungarian (CTC)")}
    for folder, (cls, label) in spec.items():
        path = root / folder
        if (path / "config.json").is_file():
            try:
                out.append(cls(path, label, device))
            except Exception as exc:                  # a model that does not load is reported, the others still score
                print(f"asr: {label} not loaded ({type(exc).__name__}: {str(exc)[:120]})")
    return out
