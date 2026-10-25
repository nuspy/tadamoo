"""The mailbox of events from the world: one EventBuffer per plan, and a pump on its own thread that judges what the buffers collect.

Events arrive from the server's route and from the read-only adapters. A plan's buffer is due after 10 events or 5 minutes from the
first; the pump then hands the batch to the gatekeeper (System 1) and the result to whoever wakes System 2. It starts only when the
server starts it and stops cleanly when told to.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from .triage import EventBuffer, EventPump, JevTriageGatekeeper, TriageResult


class EventInbox:
    def __init__(self, batch: int = 10, window_seconds: float = 300.0, max_pending: int = 1000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.batch, self.window, self.max_pending, self.clock = batch, window_seconds, max_pending, clock
        self._buffers: Dict[str, EventBuffer] = {}
        self._lock = threading.Lock()
        self.accepted = 0

    def buffer(self, plan_id: str) -> EventBuffer:
        with self._lock:
            if plan_id not in self._buffers:
                self._buffers[plan_id] = EventBuffer(self.batch, self.window, self.max_pending, self.clock)
            return self._buffers[plan_id]

    def add(self, plan_id: str, text: str) -> bool:
        """Puts an event in the plan's buffer; True when a batch is now due."""
        with self._lock:
            self.accepted += 1
        return self.buffer(plan_id).add_event(text)

    def forget(self, plan_id: str) -> None:
        with self._lock:
            self._buffers.pop(plan_id, None)

    def plans(self) -> List[str]:
        with self._lock:
            return list(self._buffers)

    def pending(self) -> Dict[str, int]:
        with self._lock:
            return {pid: len(b) for pid, b in self._buffers.items() if len(b)}

    def dropped(self) -> int:
        with self._lock:
            return sum(b.dropped for b in self._buffers.values())


class InboxPump:
    """One thread for all plans: each tick, every plan whose batch is due is judged and its result handed on.

    `card_for(plan_id)` gives the card the events are judged against, or None when the plan is gone (its events are dropped);
    `on_result(plan_id, result, events)` is told each verdict."""

    def __init__(self, inbox: EventInbox, gatekeeper: Callable[[], JevTriageGatekeeper], card_for: Callable[[str], Any],
                 on_result: Callable[[str, TriageResult, List[str]], None], poll_seconds: float = 1.0) -> None:
        self.inbox, self.gatekeeper, self.card_for, self.on_result, self.poll_seconds = inbox, gatekeeper, card_for, on_result, poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.batches = 0
        self.errors: Deque[str] = deque(maxlen=20)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def process_once(self) -> int:
        """Judges every due batch; returns how many."""
        done = 0
        for plan_id in self.inbox.plans():
            buf = self.inbox.buffer(plan_id)
            if not buf.due():
                continue
            try:
                card = self.card_for(plan_id)
                if card is None:
                    buf.flush()
                    self.inbox.forget(plan_id)
                    continue
                pump = EventPump(buf, self.gatekeeper(), lambda c=card: c, lambda r, ev, p=plan_id: self.on_result(p, r, ev))
                if pump.process_once() is not None:
                    done += 1
                    self.batches += 1
                self.errors.extend(pump.errors)
            except Exception as exc:
                self.errors.append(f"{plan_id}: {type(exc).__name__}: {exc}")
        return done

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.process_once()
            except Exception as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")

    def start(self) -> None:
        if not self.running:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="inbox-pump")
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Ends the thread: a batch being judged finishes first (its network call is bounded), then the thread returns."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None
