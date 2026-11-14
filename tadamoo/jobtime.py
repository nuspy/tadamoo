"""How long a job of each kind may take: proportional to the length of what it works on, from the times the jobs really took. One place, used by the media client (tadamoo/media/) and by the speech client (tadamoo/speech/).

Every job that ends well records its **ratio**: seconds of work for each second of audio or video it made (or was given). The longest a job may take is

    factor x (median of the recorded ratios of its kind) x duration            at least `floor_s`

and while nothing has been recorded for a kind, a provisional ratio stands in: 1.0 for audio, 42 for video. The factor is 3 for audio and 2 for video, and the floor 120 s (a model that loads takes that long
before it works): the three are in the media settings (`time_factor_audio`, `time_factor_video`, `time_min_s`). Going over the limit is an error of the step, and the process that was working is ended.

The ratios are in `job_times.json` in the user data folder: `{kind: [ratio, ...]}`, the last `KEEP` of each kind. Kinds: voice (audio), avatar, video (B-roll), modify, upscale, interpolate (video)."""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FILE = "job_times.json"
KEEP = 50
CLASS = {"voice": "audio", "avatar": "video", "video": "video", "modify": "video", "upscale": "video", "interpolate": "video"}
PROVISIONAL = {"audio": 1.0, "video": 42.0}
FACTORS = {"audio": 3.0, "video": 2.0}
FLOOR_S = 120.0
_lock = threading.Lock()


class Overrun(RuntimeError):
    """A job went over the longest it may take."""


def duration_of(path: Any) -> float:
    """The length in seconds of an audio or video file: a WAV by its header, anything else by ffprobe; 0.0 when it cannot be told."""
    try:
        p = Path(str(path))
        if p.suffix.lower() == ".wav":
            with wave.open(str(p)) as w:
                return w.getnframes() / float(w.getframerate())
        probe = shutil.which("ffprobe")
        if probe:
            done = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True, timeout=60)
            return max(0.0, float(done.stdout.strip() or 0.0))
    except (OSError, ValueError, wave.Error, subprocess.SubprocessError):
        pass
    return 0.0


def factors_from(settings: Optional[Dict[str, Any]]) -> Tuple[Dict[str, float], float]:
    """`({audio, video}, floor_s)` from a media-settings dict; what it does not hold is the default."""
    s = settings or {}
    return ({"audio": float(s.get("time_factor_audio") or FACTORS["audio"]), "video": float(s.get("time_factor_video") or FACTORS["video"])},
            float(s["time_min_s"]) if isinstance(s.get("time_min_s"), (int, float)) else FLOOR_S)


class Timing:
    def __init__(self, file: Optional[Path] = None, settings: Optional[Dict[str, Any]] = None) -> None:
        self.file = Path(file) if file else None
        self.factors, self.floor_s = factors_from(settings)

    def _path(self) -> Path:
        if self.file is None:
            from serve.userdata import user_data_dir
            self.file = user_data_dir() / FILE
        return self.file

    def _read(self) -> Dict[str, List[float]]:
        try:
            raw = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: [float(x) for x in v if isinstance(x, (int, float)) and x > 0] for k, v in raw.items() if isinstance(v, list)} if isinstance(raw, dict) else {}

    def record(self, kind: str, work_s: float, duration_s: float) -> Optional[float]:
        """Keeps the ratio of one job that ended well (seconds of work for each second of audio or video); returns it, or None when the numbers say nothing."""
        if kind not in CLASS or not (work_s > 0 and duration_s > 0):
            return None
        ratio = work_s / duration_s
        with _lock:
            data = self._read()
            data[kind] = (data.get(kind, []) + [round(ratio, 3)])[-KEEP:]
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        return ratio

    def ratio(self, kind: str) -> Tuple[float, int]:
        """`(ratio, how many jobs it comes from)`: the median of the recorded ones, or the provisional value and 0."""
        seen = self._read().get(kind, [])
        return (statistics.median(seen), len(seen)) if seen else (PROVISIONAL[CLASS[kind]], 0)

    def limit(self, kind: str, duration_s: float) -> float:
        """The longest, in seconds, a job of `kind` may take on `duration_s` of audio or video."""
        ratio, _ = self.ratio(kind)
        return max(self.floor_s, self.factors[CLASS[kind]] * ratio * duration_s)


class Deadline:
    """The clock of one job: `check()` raises `Overrun` with the numbers once it has run longer than the limit."""

    def __init__(self, timing: Timing, kind: str, duration_s: float) -> None:
        self.kind, self.duration_s = kind, duration_s
        self.limit_s = timing.limit(kind, duration_s)
        self.ratio, self.measured = timing.ratio(kind)
        self.began = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.began

    def expired(self) -> bool:
        return self.elapsed() > self.limit_s

    def check(self) -> None:
        if self.expired():
            basis = f"median of {self.measured} jobs" if self.measured else "provisional"
            raise Overrun(f"the {self.kind} took longer than {int(self.limit_s)} seconds ({self.ratio:g} s for each second of {CLASS[self.kind]}, {basis}, on {self.duration_s:.1f} s) and was ended")
