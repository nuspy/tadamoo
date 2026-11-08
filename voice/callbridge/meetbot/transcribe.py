"""TranscriptStore: trascrizione diarizzata della riunione.

Ogni enunciato STT viene attribuito al parlante osservato dall'adapter
(poll dell'active-speaker) nella finestra temporale corrispondente —
best-effort: se la piattaforma non lo espone, "Sconosciuto".

Persistenza: JSONL append-only ({ts, start_s, speaker, text}) cosi' il
transcript sopravvive a crash a meta' riunione.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class SpeakerTimeline:
    """Registra (timestamp, speaker_attivo) dal poll dell'adapter."""

    def __init__(self, keep_s: float = 600.0) -> None:
        self._events: list[tuple[float, str]] = []
        self._lock = threading.Lock()
        self._keep_s = keep_s

    def record(self, speaker: str | None) -> None:
        if not speaker:
            return
        now = time.monotonic()
        with self._lock:
            self._events.append((now, speaker))
            cutoff = now - self._keep_s
            while self._events and self._events[0][0] < cutoff:
                self._events.pop(0)

    def dominant(self, t_from: float, t_to: float) -> str | None:
        """Parlante piu' presente nella finestra [t_from, t_to] (monotonic)."""
        counts: dict[str, int] = {}
        with self._lock:
            for ts, spk in self._events:
                if t_from <= ts <= t_to:
                    counts[spk] = counts.get(spk, 0) + 1
        if not counts:
            return None
        return max(counts, key=counts.get)


class TranscriptStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.segments: list[dict] = []
        self.participants: list[str] = []

    def note_participants(self, names: list[str]) -> None:
        with self._lock:
            for n in names:
                if n and n not in self.participants:
                    self.participants.append(n)

    def add(self, text: str, *, speaker: str | None = None,
            spoken_from: float | None = None,
            spoken_to: float | None = None) -> dict:
        seg = {
            "ts": time.strftime("%H:%M:%S"),
            "start_s": round((spoken_from or time.monotonic()) - self._t0, 1),
            "speaker": speaker or "Sconosciuto",
            "text": text,
        }
        with self._lock:
            self.segments.append(seg)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(seg, ensure_ascii=False) + "\n")
        return seg

    def tail(self, n: int = 10) -> list[dict]:
        with self._lock:
            return self.segments[-n:]
