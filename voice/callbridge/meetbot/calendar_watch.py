"""The calendar watcher: joins the events the person marked for auto-join, a minute before they start.

Off unless the person turned `meetings.calendar_autojoin` on, and each event needs its own switch (saved in `autojoin.json`, which survives a
restart). The join goes through the same path as a join from the page, so the announcement and the consent rule apply in full. A provider
without the person's own OAuth values is skipped: nothing is called."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("callbridge.meetbot.calendar")

JOIN_EARLY_S = 60
JOIN_LATE_TOLERANCE_S = 900


def _parse_dt(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


class AutojoinStore:
    """The per-event switches, kept on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            self._data: dict[str, bool] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, event_id: str) -> bool:
        return bool(self._data.get(event_id))

    def set(self, event_id: str, enabled: bool) -> None:
        with self._lock:
            if enabled:
                self._data[event_id] = True
            else:
                self._data.pop(event_id, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")


class CalendarWatcher:
    def __init__(self, providers: list, store: AutojoinStore, join_cb, poll_s: float = 300.0, clock=time.time) -> None:
        self.providers, self.store, self.join_cb, self.poll_s, self._clock = providers, store, join_cb, poll_s, clock
        self._stop = threading.Event()
        self._joined: set[str] = set()
        self.last_events: list[dict] = []
        self.last_poll = 0.0

    def start(self) -> None:
        threading.Thread(target=self._loop, name="calendar-watch", daemon=True).start()

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
        for ev in events:
            ev["autojoin"] = self.store.get(ev["id"])
        self.last_events, self.last_poll = events, self._clock()
        return events

    def tick(self) -> list[str]:
        """One poll: join what is due. Returns the ids joined."""
        events = self.fetch_events()
        now, done = self._clock(), []
        for ev in events:
            if not ev.get("autojoin") or not ev.get("meeting_url") or ev["id"] in self._joined:
                continue
            start = _parse_dt(ev.get("start"))
            if start is None or not (start - JOIN_EARLY_S) <= now <= (start + JOIN_LATE_TOLERANCE_S):
                continue
            try:
                ok = bool(self.join_cb(ev["meeting_url"], ev.get("title", ""), ev.get("organizer") or ""))
            except Exception as e:  # noqa: BLE001
                log.error("auto-join failed: %s", e)
                ok = False
            if ok:                                                   # a refusal is tried again on the next poll, inside the 15 minutes
                self._joined.add(ev["id"])
                done.append(ev["id"])
        self._joined &= {ev["id"] for ev in events}
        return done

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.error("calendar watch: %s", e)
            self._stop.wait(self.poll_s)
