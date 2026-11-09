"""The loop of the planner, and the calendars it reads.

One thread wakes every `tick_s` and, when the person's master switch (`meetings.autojoin`) is on, reads the calendars at most every `poll_s` and
hands the events to the planner, which joins what is due (`planner.py`). The loop runs with no calendar connected too: it also carries the meetings
the person planned by hand. A provider without the person's own OAuth values is skipped: nothing is called."""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("callbridge.meetbot.calendar")


class CalendarWatcher:
    def __init__(self, providers: list, tick_fn, enabled, poll_s: float = 300.0, tick_s: float = 15.0, clock=time.time) -> None:
        """`tick_fn(now)` is the planner's tick; `enabled()` is the master switch, read at every turn."""
        self.providers, self.tick_fn, self.enabled, self.poll_s, self.tick_s, self._clock = providers, tick_fn, enabled, poll_s, tick_s, clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_events: list[dict] = []
        self.last_poll = float("-inf")

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="calendar-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def fetch_events(self, days: int = 7) -> list[dict]:
        events: list[dict] = []
        for provider in self.providers:
            if not provider.configured():
                continue
            try:
                events.extend(provider.events(days=days))
            except Exception as e:  # noqa: BLE001
                log.warning("reading a calendar failed: %s", e)
        events.sort(key=lambda e: e.get("start") or "")
        self.last_events, self.last_poll = events, self._clock()
        return events

    def cached_events(self) -> list[dict]:
        """The events of the last reading; read again when it is older than `poll_s`."""
        if self._clock() - self.last_poll >= self.poll_s:
            self.fetch_events()
        return self.last_events

    def tick(self) -> list[str]:
        """One turn: nothing while the master switch is off, else the calendars (when due) and the planner. Returns the ids joined."""
        if not self.enabled():
            return []
        self.cached_events()
        return self.tick_fn(self._clock())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.error("planner loop: %s", e)
            self._stop.wait(self.tick_s)
