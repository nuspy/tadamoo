"""Learning for the person's memories, in the background, only while the GPU has nothing else to do.

The conversations hand over what the person wrote; this keeps it (encrypted) in a queue and, when no request has been served for a few
seconds and the card is not busy with anything else, asks the loaded model what is worth remembering. It never loads a model and never
waits in front of a conversation: it looks again a little later.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from harness import transcript
from harness.memory_extract import Extractor
from harness.personal_memory import PersonalMemory

QUIET_S = 4.0                   # no request for this long: the GPU is free
BUSY_UTIL = 50                  # a card this busy (percent) is doing something else
QUEUED_CHARS = 3000             # most of one message that is kept to be learned from
MIN_CHARS = 10                  # "ok" and "yes" tell nothing
POLL_S = 3.0
RETRY_S = 30.0                  # after a failed call
SEARCH_TOOLS = ("search_web",)


def gpu_free(svc: Any, quiet_s: float = QUIET_S, mine: float = 0.0, now: Optional[float] = None) -> bool:
    """True when the model is loaded, nothing is being served or waits, the last request is not recent (but ours) and no card is busy."""
    now = time.time() if now is None else now
    if svc.stopping.is_set() or getattr(svc, "loading", False) or not svc.loaded():
        return False
    with svc.status_lock:
        busy, queued = bool(svc.status.get("busy")), int(svc.status.get("queued") or 0)
    if busy or queued or svc.live_reqs:
        return False
    last = getattr(svc, "last_request_at", None)
    if last and now - last < quiet_s and abs(last - mine) > 2.0:
        return False
    hardware = svc.hardware_now() or {}
    return not any((g.get("util_pct") or 0) > BUSY_UTIL for g in hardware.get("gpus") or [])


class MemoryWorker:
    def __init__(self, memory: PersonalMemory, client: Callable[[], Any], free: Callable[[], bool], poll_s: float = POLL_S,
                 retry_s: float = RETRY_S) -> None:
        self.memory, self.poll_s, self.retry_s = memory, poll_s, retry_s
        self.extractor = Extractor(memory, client, free)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def offer(self, job: Dict[str, Any]) -> bool:
        """A message of the person (and what was searched for it), kept for learning if either memory learns."""
        cfg = self.memory.settings()
        text = str(job.get("text") or "").strip()
        if not (cfg["learn_long"] or cfg["learn_short"]) or len(text) < MIN_CHARS:
            return False
        text, omitted = transcript.clip(text, QUEUED_CHARS)
        return self.memory.enqueue({"text": text, "omitted": omitted, "project": str(job.get("project") or "")[:80], "conversation": str(job.get("conversation") or "")[:80],
                                    "searches": [str(q)[:200] for q in (job.get("searches") or [])[:10]]})

    def start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="memory-worker", daemon=True)
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.extractor.pending() and self.extractor.step():
                    self._stop.wait(0.5)
                    continue
            except Exception as exc:                      # a damaged file or a bug stops one round, not the server
                self.extractor.error = f"{type(exc).__name__}: {exc}"[:200]
            self._stop.wait(self.retry_s if self.extractor.error else self.poll_s)

    def status(self) -> Dict[str, Any]:
        return {"waiting": self.memory.queue_len(), "error": self.extractor.error, "last": self.extractor.last or None,
                "last_run": self.extractor.last_run}
