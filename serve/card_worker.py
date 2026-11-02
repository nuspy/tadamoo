"""Writes the card of each conversation (harness/cards.py), in the background.

Two kinds of work, one card at a time. A conversation that has been quiet for `idle_s` seconds gets its card (or a new one if it grew). The
conversations already in the index without a card are back-filled: newest first, at most one every `gap_s` seconds, and only while the
server has nothing else to do (`free()`); the back-fill is derived from the index itself, so it resumes by itself after a restart, and the
person can switch it off and on (kept in the index). With no technical session, or with the use switched off, the cards are made by code, which
costs no model time; they are written again by the session later if it becomes available (a card made by code is tried again at most three times).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from harness import cards, digest, side_uses
from harness.side import side_context, side_extra

USE = "conversation_card"
POLL_S = 3.0
META_BACKFILL = "cards_backfill"
BACKFILL_QUIET_S = 30.0         # no request for this long before an old conversation is carded (serve/memory_worker.py gpu_free)


class CardWorker:
    def __init__(self, past: Any, client: Callable[[], Any], free: Callable[[], bool], poll_s: float = POLL_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.past, self.client, self.free, self.poll_s, self.clock = past, client, free, poll_s, clock
        self._due: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: Any = None
        self.last = 0.0                                   # when a card was last written
        self.last_source = ""
        self.error = ""

    # -- what the conversations tell it ----------------------------------------------------------------------------------

    def touch(self, session: str) -> None:
        """A turn of the conversation ended: its card is written once it has been quiet for `idle_s`."""
        with self._lock:
            self._due[session] = self.clock() + float(side_uses.param(USE, "idle_s"))

    def backfill_on(self) -> bool:
        return self.past.meta_get(META_BACKFILL, "on") != "off"

    def set_backfill(self, on: bool) -> None:
        self.past.meta_set(META_BACKFILL, "on" if on else "off")

    # -- one unit of work --------------------------------------------------------------------------------------------------

    def _writer(self) -> tuple:
        """(ask, window in characters) for the technical session, or (None, 0) when there is none, the use is off, or it cannot be asked."""
        if not side_uses.enabled(USE):
            return None, 0
        if self._client is None:
            self._client = self.client()
        window = side_context(self._client, USE)
        if window <= 0:
            return None, 0
        answer = int(side_uses.param(USE, "max_tokens"))
        client = self._client

        def ask(prompt: List[Dict[str, Any]]) -> str:
            reply = client.chat_completion(messages=prompt, temperature=0.0, max_tokens=answer, timeout=side_uses.param(USE, "timeout_s"),
                                           response_format={"type": "json_object"}, extra={"reasoning_effort": "none", **side_extra(client, USE, only=True)})
            return reply["choices"][0]["message"].get("content") or ""
        return ask, digest.part_chars(window, answer)

    def _write(self, session: str, ask: Any, window: int) -> bool:
        got = cards.write_card(self.past, session, ask, window)
        if got:
            self.last, self.last_source = self.clock(), got
        return bool(got)

    def step(self) -> bool:
        """The next card that is due or wanted; False when there was nothing to do now."""
        now = self.clock()
        with self._lock:
            ready = sorted((t, s) for s, t in self._due.items() if t <= now)
            if ready:
                del self._due[ready[0][1]]
        if ready:
            ask, window = self._writer()
            self._write(ready[0][1], ask, window)
            return True
        if not self.backfill_on() or not self.past.cards_wanted(1):
            return False
        ask, window = self._writer()
        if ask is not None:
            if now - self.last < float(side_uses.param(USE, "gap_s")) or not self.free():
                return False
        wanted = self.past.cards_wanted(1, model=ask is not None)
        if not wanted:
            return False
        self._write(wanted[0], ask, window)
        return True

    # -- the thread --------------------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="card-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            did = False
            try:
                did = self.step()
                self.error = ""
            except Exception as exc:                      # a damaged index or an unreachable server stops one round, not the server
                self.error = f"{type(exc).__name__}: {exc}"[:200]
            self._stop.wait(0.2 if did else self.poll_s)

    def status(self) -> Dict[str, Any]:
        """For the Memories page: how many conversations have a card of the total, who wrote them, and whether the back-fill is on."""
        counts = self.past.card_counts()
        with self._lock:
            waiting = len(self._due)
        return {**counts, "backfill": self.backfill_on(), "waiting": waiting, "error": self.error, "last": self.last or None, "last_source": self.last_source}
