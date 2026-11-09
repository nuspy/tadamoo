"""The transcript of a meeting, with the speaker the platform showed for each line (best effort: "Unknown" when it does not say).

Nothing touches the disk until `commit()`, which the session calls once consent is given; `discard()` drops what is held and deletes the file.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class SpeakerTimeline:
    """Records (time, active speaker) from the polls of the adapter."""

    def __init__(self, keep_s: float = 600.0, clock=time.monotonic) -> None:
        self._events: list[tuple[float, str]] = []
        self._lock = threading.Lock()
        self._keep_s = keep_s
        self._clock = clock

    def record(self, speaker: str | None) -> None:
        if not speaker:
            return
        now = self._clock()
        with self._lock:
            self._events.append((now, speaker))
            cutoff = now - self._keep_s
            while self._events and self._events[0][0] < cutoff:
                self._events.pop(0)

    def dominant(self, t_from: float, t_to: float) -> str | None:
        """The speaker seen most often in the window [t_from, t_to]."""
        counts: dict[str, int] = {}
        with self._lock:
            for ts, spk in self._events:
                if t_from <= ts <= t_to:
                    counts[spk] = counts.get(spk, 0) + 1
        return max(counts, key=counts.get) if counts else None


class TranscriptStore:
    """The lines of a meeting: in memory until `commit(path)`, then also appended to that file (JSON lines, so a crash loses nothing)."""

    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._t0 = clock()
        self.path: Path | None = None
        self.segments: list[dict] = []
        self.participants: list[str] = []

    def note_participants(self, names: list[str]) -> None:
        with self._lock:
            for n in names:
                if n and n not in self.participants:
                    self.participants.append(n)

    def _append(self, seg: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(seg, ensure_ascii=False) + "\n")

    def add(self, text: str, *, speaker: str | None = None, at: float | None = None) -> dict:
        seg = {"ts": time.strftime("%H:%M:%S"), "start_s": round((self._clock() if at is None else at) - self._t0, 1),
               "speaker": speaker or "Unknown", "text": text}
        with self._lock:
            self.segments.append(seg)
            if self.path is not None:
                self._append(seg)
        return seg

    def commit(self, path: Path) -> None:
        """Consent is given: write what is held to `path` and keep writing there."""
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path = path
            path.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in self.segments), encoding="utf-8")

    def discard(self) -> None:
        """Forget every line and delete the file."""
        with self._lock:
            self.segments = []
            self.participants = []
            if self.path is not None:
                self.path.unlink(missing_ok=True)
                self.path = None

    def tail(self, n: int = 10) -> list[dict]:
        with self._lock:
            return self.segments[-n:]
