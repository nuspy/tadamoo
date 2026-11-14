"""Speech-to-text with word times that runs in this process, for a voice made without the TaDaMoo server (the read-back of the takes). The same words the voice service's Whisper gives
(`{text, start, end}`), from the `whisper` package (the model the service uses, medium), or from a transformers Whisper pipeline when that package is not installed. `pick_device` says where the
small models of the rating (this one, the CTC read-back, the speaker embedder) go: the GPU when it has room, else the CPU."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List

LANGS = {"hu": "hungarian", "it": "italian", "en": "english"}


def pick_device(want: str = "auto", need_gib: float = 4.0) -> str:
    """`cpu` or `cuda` as asked; `auto` is the GPU when there is one with `need_gib` free (the rating models are a few GB together), else the CPU."""
    if want in ("cpu", "cuda"):
        return want
    try:
        import torch
        if torch.cuda.is_available():
            free, _ = torch.cuda.mem_get_info()
            return "cuda" if free / 2 ** 30 >= need_gib else "cpu"
    except Exception:                                          # no torch or no driver: the CPU
        pass
    return "cpu"


class LocalWhisper:
    """One Whisper kept loaded; calls from several threads take turns on it. `words_many` reads several recordings in one decoding pass: a decoding step is bound by its launches, so four
    recordings cost about what one does (a recording of 8 s alone takes about 1.7 s, four together about 2)."""

    def __init__(self, name: str = "medium", device: str = "auto", folder: Path | None = None) -> None:
        self.name, self.device, self.folder = name, pick_device(device, 3.0), folder
        self.model: Any = None
        self.lock = threading.Lock()

    def warm(self) -> None:
        """Loads the model now, so that the first read-back does not wait for it."""
        with self.lock:
            if self.model is None:
                self._load()

    def words_many(self, wavs: List[bytes], lang: str = "hu") -> List[List[Dict[str, Any]]]:
        """The words of each recording. With the `whisper` package the recordings of up to 30 s are decoded together and aligned one by one; anything else is read one at a time."""
        from tadamoo.speech import asr
        if len(wavs) <= 1:
            return [self.words(w, lang) for w in wavs]
        pcms = [asr.pcm16k(w) for w in wavs]
        with self.lock:
            if self.model is None:
                self._load()
            kind, model = self.model
            if kind != "whisper" or any(len(p) > 30 * asr.TARGET_RATE for p in pcms):
                pass
            else:
                return self._batch(model, pcms, lang)
        return [self.words(w, lang) for w in wavs]

    def _batch(self, model: Any, pcms: List[Any], lang: str) -> List[List[Dict[str, Any]]]:
        import numpy as np
        import torch
        import whisper
        from whisper.audio import HOP_LENGTH, N_FRAMES, N_SAMPLES, SAMPLE_RATE, log_mel_spectrogram, pad_or_trim
        from whisper.timing import add_word_timestamps
        dtype = torch.float16 if self.device != "cpu" else torch.float32
        if any(p.dtype != torch.float32 for p in model.parameters()):       # Whisper's LayerNorm takes a float input and needs float weights: a model with some weights made half fails the decoding with "expected Float but found Half"
            model.float()
        tokenizer = whisper.tokenizer.get_tokenizer(model.is_multilingual, num_languages=model.num_languages, language=lang, task="transcribe")
        mels, frames = [], []
        for pcm in pcms:
            mel = log_mel_spectrogram(pcm.astype(np.float32), model.dims.n_mels, padding=N_SAMPLES)
            frames.append(min(N_FRAMES, mel.shape[-1] - N_FRAMES))
            mels.append(pad_or_trim(mel[:, :N_FRAMES], N_FRAMES).to(model.device).to(dtype))
        results = whisper.decode(model, torch.stack(mels), whisper.DecodingOptions(language=lang, fp16=self.device != "cpu", temperature=0.0))
        out: List[List[Dict[str, Any]]] = []
        for mel, n, result in zip(mels, frames, results):
            seg = {"seek": 0, "start": 0.0, "end": n * HOP_LENGTH / SAMPLE_RATE, "tokens": list(result.tokens), "text": result.text}
            add_word_timestamps(segments=[seg], model=model, tokenizer=tokenizer, mel=mel, num_frames=n, prepend_punctuations="\"'“¿([{-", append_punctuations="\"'.。,，!！?？:：”)]}、",
                                last_speech_timestamp=0.0)
            out.append([{"text": str(w["word"]).strip(), "start": float(w["start"]), "end": float(w["end"])} for w in seg.get("words", [])])
        return out

    def _load(self) -> None:
        try:
            import whisper
            self.model = ("whisper", whisper.load_model(self.name, device=self.device))
        except ImportError:
            import torch
            from transformers import pipeline
            if self.folder is None:
                from tadamoo.speech import asr
                self.folder = asr.MODELS / "whisper-large-v3"
            self.model = ("hf", pipeline("automatic-speech-recognition", model=str(self.folder), dtype=torch.float16 if self.device != "cpu" else torch.float32, device=self.device))

    def words(self, wav: bytes, lang: str = "hu") -> List[Dict[str, Any]]:
        from tadamoo.speech import asr
        pcm = asr.pcm16k(wav)
        with self.lock:
            if self.model is None:
                self._load()
            kind, model = self.model
            if kind == "whisper":
                done = model.transcribe(pcm, language=lang, word_timestamps=True, fp16=self.device != "cpu", temperature=0.0, condition_on_previous_text=False, verbose=None)
                return [{"text": str(w["word"]).strip(), "start": float(w["start"]), "end": float(w["end"])} for seg in done["segments"] for w in seg.get("words", [])]
            done = model({"raw": pcm, "sampling_rate": asr.TARGET_RATE}, return_timestamps="word", generate_kwargs={"language": LANGS.get(lang, lang), "task": "transcribe"})
            return [{"text": str(c["text"]).strip(), "start": float(c["timestamp"][0] or 0.0), "end": float(c["timestamp"][1] or c["timestamp"][0] or 0.0)} for c in done.get("chunks", [])]
