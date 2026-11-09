"""The planner: one list of meetings, the ones the person planned and the ones in their calendar, and the one clock that joins them.

The list (`view`) is the stored plan plus the calendar's events that an adapter can join; a calendar event becomes a stored item when the person
first changes something about it (Participate, role, personality, camera). `tick` is one turn of the loop that `CalendarWatcher` runs: when the
person's master switch (`meetings.autojoin`) is on, an item with Participate ticked is joined `lead_s` before it starts and up to `late_s` after,
once, through the same path as a join from the page (`join_cb`), so the announcement and the consent rule apply in full. A refused join is
tried again every RETRY_S inside that window; after it the item shows as missed. Nothing joins by itself while the master switch is off."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .adapters import detect
from .errors import Refused
from .plan_store import PlanStore, calendar_id, clean, parse_start, start_of

log = logging.getLogger("callbridge.meetbot.planner")

JOIN_EARLY_S = 60
JOIN_LATE_TOLERANCE_S = 900
RETRY_S = 30
RECENT_S = 2 * 86400                    # what started longer ago is not listed (it is forgotten after plan_store.KEEP_S)
SNAPSHOT = ("title", "url", "platform", "start", "duration_min", "organizer")
DEFAULTS = {"participate": False, "role": "minutes", "speak": False, "persona": "", "camera": False, "consent_mode": "", "notes": ""}


class Planner:
    def __init__(self, store: PlanStore, events: Callable[[], list[dict]], join_cb: Callable[[dict], dict], enabled: Callable[[], bool],
                 active: Callable[[], str] = lambda: "", clock: Callable[[], float] = time.time, lead_s: int = JOIN_EARLY_S,
                 late_s: int = JOIN_LATE_TOLERANCE_S) -> None:
        self.store, self._events, self.join_cb, self.enabled, self.active = store, events, join_cb, enabled, active
        self._clock, self.lead_s, self.late_s = clock, lead_s, late_s
        self._retry_at: dict[str, float] = {}

    # ---- the calendar's events as items
    @staticmethod
    def _event_item(ev: dict) -> dict | None:
        """The item an event makes, or None when it has no address an adapter takes or no usable start."""
        url, start = ev.get("meeting_url"), parse_start(ev.get("start"), assume_utc=True)
        if not isinstance(url, str) or not isinstance(ev.get("id"), str) or start is None or detect(url) is None:
            return None
        end = parse_start(ev.get("end"), assume_utc=True)
        minutes = round((end[1] - start[1]) / 60) if end else None
        provider = ev.get("provider") if ev.get("provider") in ("google", "microsoft") else "google"
        return {"id": calendar_id(ev["id"]), "source": provider, "event_id": ev["id"], "title": str(ev.get("title") or "")[:120] or detect(url),
                "url": url, "platform": detect(url), "start": start[0], "duration_min": minutes if minutes and 1 <= minutes <= 1440 else None,
                "organizer": str(ev.get("organizer") or "")[:120]}

    def _calendar_items(self) -> dict[str, dict]:
        out = {}
        for ev in self._events():
            item = self._event_item(ev) if isinstance(ev, dict) else None
            if item is not None:
                out[item["id"]] = item
        return out

    # ---- the list
    def _status(self, item: dict, start: float, now: float) -> str:
        if item.get("session_id"):
            return "running" if item["session_id"] == self.active() else "done"
        if now > start + self.late_s:
            return "missed" if item.get("participate") else "past"
        return "upcoming"

    def _rows(self) -> dict[str, dict]:
        live = self._calendar_items()
        rows = {i["id"]: i for i in self.store.all()}
        for item_id, ev in live.items():
            snapshot = {k: ev[k] for k in SNAPSHOT}
            if item_id in rows:
                if any(rows[item_id].get(k) != v for k, v in snapshot.items()):
                    self.store.refresh_snapshot(item_id, snapshot)
                    rows[item_id].update(snapshot)
                rows[item_id]["on_calendar"] = True
            else:
                rows[item_id] = {**DEFAULTS, **ev, "synthetic": True, "on_calendar": True}
        return rows

    def view(self, now: float | None = None) -> list[dict]:
        """The items that are coming or started in the last two days: coming ones first by start, then the recent ones, the latest first."""
        now = self._clock() if now is None else now
        coming, past = [], []
        for item in self._rows().values():
            start = start_of(item)
            if start is None:
                continue
            item["status"] = self._status(item, start, now)
            if item["status"] in ("upcoming", "running"):
                coming.append((start, item))
            elif start >= now - RECENT_S:
                past.append((start, item))
        coming.sort(key=lambda p: (p[0], p[1]["id"]))
        past.sort(key=lambda p: (-p[0], p[1]["id"]))
        return [i for _, i in coming + past]

    def get(self, item_id: str) -> dict | None:
        item = self._rows().get(item_id)
        if item is not None:
            start = start_of(item)
            item["status"] = self._status(item, start, self._clock()) if start is not None else "past"
        return item

    # ---- the person's changes
    def add(self, data) -> dict:
        return self.store.add(data)

    def update(self, item_id: str, changes) -> dict:
        item = self.get(item_id)
        if item is None:
            raise Refused(404, "not_found", "No such meeting in the plan.")
        if item.get("synthetic"):
            item = {k: v for k, v in item.items() if k not in ("synthetic", "status", "on_calendar")}
            return self.store.put({**clean(changes, item), "id": item_id})
        return self.store.update(item_id, changes)

    def delete(self, item_id: str) -> None:
        item = self.get(item_id)
        if item is not None and item.get("source") in ("google", "microsoft"):
            raise Refused(400, "calendar_owned", "A meeting from the calendar is removed in the calendar; here, turn Participate off.")
        self.store.delete(item_id)

    def set_participate(self, event_id: str, enabled: bool) -> dict:
        return self.update(calendar_id(event_id), {"participate": enabled})

    def participating(self, event_id: str) -> bool:
        item = self.store.get(calendar_id(event_id))
        return bool(item and item.get("participate"))

    def mark_joined(self, item_id: str, session_id: str, note: str = "") -> None:
        if self.store.get(item_id) is None and self.get(item_id) is not None:
            self.update(item_id, {})                                    # a calendar event joined with Join now: it becomes an item
        self.store.mark(item_id, session_id=session_id, joined_at=datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        last_error="", last_note=note)

    # ---- the clock
    def tick(self, now: float | None = None) -> list[str]:
        """One turn: join what is due. Returns the ids joined."""
        if not self.enabled():
            return []
        now = self._clock() if now is None else now
        self.store.prune()
        done = []
        for item_id, item in self._rows().items():
            start = start_of(item)
            if (start is None or not item.get("participate") or item.get("session_id") or item.get("synthetic")
                    or (item.get("source") != "manual" and not item.get("on_calendar"))):
                continue                                                # a calendar item joins only while the calendar still lists it
            if not (start - self.lead_s) <= now <= (start + self.late_s) or self._retry_at.get(item_id, 0.0) > now:
                continue
            try:
                joined = self.join_cb(item)
            except Refused as e:
                error = e.message
            except Exception as e:  # noqa: BLE001 (one meeting that fails must not stop the others)
                log.error("planned join failed: %s", e)
                error = str(e)
            else:
                self.mark_joined(item_id, joined["session_id"], joined.get("camera_blocked", ""))
                self._retry_at.pop(item_id, None)
                done.append(item_id)
                continue
            self._retry_at[item_id] = now + RETRY_S
            self.store.mark(item_id, last_error=error[:200])
        return done
