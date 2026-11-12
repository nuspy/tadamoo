"""What is done to a voice recording with ffmpeg: slowed without changing its pitch, brought to one level and one loudness, and tamed for the talking-head model.

The talking head moves its mouth, head and hands after the recording it is given. The tamed copy (`tame`: quieter, its dynamic range squeezed, the high band that drives sharp mouth shapes
taken down) goes to the model only; the final soundtrack is always the untouched recording, levelled. Every function takes WAV bytes and gives WAV bytes."""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import List, Optional


class AudioError(RuntimeError):
    pass


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise AudioError(f"{name} is not on the PATH")
    return found


def _filters() -> str:
    return subprocess.run([_tool("ffmpeg"), "-hide_banner", "-filters"], capture_output=True, text=True).stdout


def has_rubberband() -> bool:
    return " rubberband " in _filters()


def _run(wav: bytes, filters: str, capture_stderr: bool = False) -> bytes:
    """`wav` through an ffmpeg audio filter chain, as 16-bit PCM at its own rate."""
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.wav"
        src.write_bytes(wav)
        with wave.open(io.BytesIO(wav)) as w:
            rate = w.getframerate()
        done = subprocess.run([_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-af", filters, "-ar", str(rate), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0:
            raise AudioError("ffmpeg failed: " + (done.stderr.strip().splitlines() or ["no message"])[-1][:300])
        return dst.read_bytes()


def stretch(wav: bytes, tempo: float) -> bytes:
    """The recording at `tempo` times its speed (0.9: ten per cent slower) with the pitch and the voice's colour unchanged. A tempo of 1 gives it back as it is."""
    if abs(tempo - 1.0) < 1e-3:
        return wav
    if not 0.5 <= tempo <= 2.0:
        raise AudioError("the tempo must be between 0.5 and 2")
    if has_rubberband():
        return _run(wav, f"rubberband=tempo={tempo:.4f}:pitchq=quality:formant=preserved:transients=smooth")
    return _run(wav, f"atempo={tempo:.4f}")


def level(wav: bytes, target_dbfs: float = -22.0) -> bytes:
    """The recording with a gain so that the RMS of its speech (the frames within 35 dB of the peak) is `target_dbfs`: pieces read at different loudness come out alike."""
    import numpy as np
    with wave.open(io.BytesIO(wav)) as w:
        n, width = w.getnframes(), w.getsampwidth()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768 if width == 2 else None
    if x is None or not len(x):
        return wav
    win = 320
    frames = x[: len(x) // win * win].reshape(-1, win)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    speech = rms[rms > rms.max() * 10 ** (-35 / 20)]
    if not len(speech):
        return wav
    gain_db = target_dbfs - 20 * float(np.log10(np.sqrt((speech ** 2).mean())))
    peak_db = 20 * float(np.log10(np.abs(x).max() + 1e-9))
    gain_db = min(gain_db, -1.0 - peak_db)                       # never into clipping
    return _run(wav, f"volume={gain_db:.3f}dB")


def loudnorm(wav: bytes, lufs: float = -16.0, true_peak: float = -1.5, lra: float = 11.0) -> bytes:
    """The recording at the integrated loudness `lufs` by one linear gain (two passes, so the dynamics and the timbre are not touched)."""
    measure = f"loudnorm=I={lufs}:TP={true_peak}:LRA={lra}:print_format=json"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.wav"
        src.write_bytes(wav)
        done = subprocess.run([_tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(src), "-af", measure, "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    found = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", done.stderr, re.S)
    if not found:
        raise AudioError("the loudness could not be measured")
    m = json.loads(found.group(0))
    chain = (f"loudnorm=I={lufs}:TP={true_peak}:LRA={lra}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
             f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    return _run(wav, chain)


def measure_lufs(wav: bytes) -> float:
    """The integrated loudness of the recording in LUFS."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.wav"
        src.write_bytes(wav)
        done = subprocess.run([_tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(src), "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", done.stderr)
    return float(found[-1]) if found else float("nan")


def tame(wav: bytes, gain_db: float = -9.0, ratio: float = 3.0, lowpass_hz: int = 4200, highpass_hz: int = 90) -> bytes:
    """A copy for the talking-head model: band-limited (the highs that drive sharp mouth shapes taken down), its dynamic range compressed (loud syllables do not open the mouth much wider
    than quiet ones) and quieter by `gain_db`. The model never hears the real soundtrack, only this."""
    chain: List[str] = [f"highpass=f={highpass_hz}", f"lowpass=f={lowpass_hz}", f"acompressor=threshold=-26dB:ratio={ratio}:attack=8:release=120:makeup=1", f"volume={gain_db}dB"]
    return _run(wav, ",".join(chain))
