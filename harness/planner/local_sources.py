"""Sources of events that need nothing from outside this machine, and the shape of the ones that need a service this program does not ship.

* `SystemSource` reads the machine's own state (the free space of a disk) and reports it when it crosses a limit.
* `DeadlineSource` reads the plans' own dates and reports a plan that is about to start or end, once for each lead time.
* `SmsSource` and `CalendarSource` take their messages from a provider the owner supplies (a modem, a calendar account). They are the
  interface, with the format of the event each makes; nothing in this program connects them to a real modem or account, so they are
  not among the kinds a Settings entry can name.
"""
from __future__ import annotations

import datetime
import shutil
import time
from typing import Any, Callable, Dict, List, Optional

from .sources import SEEN_KEPT, Source, SourceError, SourceEvent, _clip, _interval

MAX_LEAD_DAYS = 366
LEAD_DAYS = (7, 3, 1, 0)


def _number(value: Any, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{what} is a number from {low:g} to {high:g}")
    return float(value)


def clean_local(kind: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The settings of a system or deadlines source, checked and complete."""
    if kind == "system":
        path = cfg.get("disk_path")
        if not isinstance(path, str) or not path.strip() or len(path) > 260 or "\0" in path:
            raise ValueError("disk_path is a folder or a drive on this PC")
        return {"disk_path": path.strip(), "min_free_gb": _number(cfg.get("min_free_gb", 5), "min_free_gb", 0.1, 1_000_000),
                "interval_seconds": _interval(cfg, 600.0)}
    leads = cfg.get("lead_days", list(LEAD_DAYS))
    if (not isinstance(leads, list) or not 1 <= len(leads) <= 8
            or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= MAX_LEAD_DAYS for d in leads)):
        raise ValueError(f"lead_days lists up to 8 whole numbers of days, from 0 to {MAX_LEAD_DAYS}")
    return {"lead_days": sorted(set(leads), reverse=True), "interval_seconds": _interval(cfg, 3600.0)}


def build_local(cfg: Dict[str, Any], state: Optional[Dict[str, Any]], plans: Optional[Callable[[], List[Dict[str, Any]]]]) -> Source:
    interval = cfg.get("interval_seconds", 600.0)
    if cfg["kind"] == "system":
        return SystemSource(cfg["id"], cfg["disk_path"], cfg["min_free_gb"], interval, state)
    return DeadlineSource(cfg["id"], plans or (lambda: []), cfg["lead_days"], interval, state)


class SystemSource(Source):
    """The free space of one disk. One event when it falls below the limit, and another only after it has been back above it."""
    kind = "system"

    def __init__(self, source_id: str, path: str, min_free_gb: float, interval: float = 600.0, state: Optional[Dict[str, Any]] = None,
                 usage: Callable[[str], Any] = shutil.disk_usage) -> None:
        super().__init__(source_id, interval, state)
        self.path, self.min_free_gb, self._usage = path, min_free_gb, usage
        self.label = "system:disk"

    def poll(self) -> List[SourceEvent]:
        try:
            usage = self._usage(self.path)
        except OSError as exc:
            raise SourceError(f"the disk could not be read ({type(exc).__name__})") from None
        free, total = usage.free / 2 ** 30, usage.total / 2 ** 30
        low = free < self.min_free_gb
        was = bool(self.state.get("low"))
        self.state["low"] = low
        if not low or was:
            return []
        return [SourceEvent(_clip(f"Warning: free space on {self.path} is low: {free:.1f} GB left of {total:.0f} GB"), self.label, time.time())]


class DeadlineSource(Source):
    """A plan about to start or end. `plans()` gives {"plan_id", "title", "starts_on", "ends_on"} for each plan (dates as 2026-10-24 or empty). A plan
    gets one event for the nearest lead time it has reached (7, 3, 1 and 0 days by default), also when the server was off on the day itself."""
    kind = "deadlines"

    def __init__(self, source_id: str, plans: Callable[[], List[Dict[str, Any]]], lead_days: Any = LEAD_DAYS, interval: float = 3600.0,
                 state: Optional[Dict[str, Any]] = None, today: Callable[[], datetime.date] = datetime.date.today) -> None:
        super().__init__(source_id, interval, state)
        self._plans, self.lead_days, self._today = plans, sorted(set(lead_days), reverse=True), today
        self.label = "plans:deadline"

    def poll(self) -> List[SourceEvent]:
        fired: List[str] = self.state.setdefault("fired", [])
        known = set(fired)
        today, out = self._today(), []
        for plan in self._plans():
            for field, verb in (("starts_on", "starts"), ("ends_on", "ends")):
                try:
                    day = datetime.date.fromisoformat(str(plan.get(field) or ""))
                except ValueError:
                    continue
                left = (day - today).days
                reached = [lead for lead in self.lead_days if 0 <= left <= lead]
                if not reached:
                    continue
                keys = [f"{plan['plan_id']}:{field}:{day.isoformat()}:{lead}" for lead in reached]
                if f"{plan['plan_id']}:{field}:{day.isoformat()}:{min(reached)}" in known:
                    continue
                fired.extend(keys)
                when = "today" if left == 0 else f"in {left} day{'s' if left != 1 else ''}"
                out.append(SourceEvent(_clip(f'The plan "{plan.get("title", "")}" {verb} {when} ({day.isoformat()})'), self.label, time.time(), plan["plan_id"]))
        self.state["fired"] = fired[-SEEN_KEPT:]
        return out


class ProviderSource(Source):
    """A source fed by a provider: `fetch()` returns the provider's current items, each a dict with an `id`. What was not seen before becomes an event."""
    kind = "provider"
    prefix = ""

    def __init__(self, source_id: str, fetch: Callable[[], List[Dict[str, Any]]], interval: float = 60.0, state: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(source_id, interval, state)
        self._fetch = fetch
        self.label = self.prefix

    def describe(self, item: Dict[str, Any]) -> Optional[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def items(self) -> List[Dict[str, Any]]:
        return list(self._fetch())

    def poll(self) -> List[SourceEvent]:
        try:
            items = self.items()
        except Exception as exc:
            raise SourceError(f"the {self.kind} provider failed ({type(exc).__name__})") from None
        seen: List[str] = self.state.setdefault("seen", [])
        known, out = set(seen), []
        for item in items:
            ident = str(item.get("id", "")) if isinstance(item, dict) else ""
            if not ident or ident in known:
                continue
            text = self.describe(item)
            seen.append(ident)
            known.add(ident)
            if text:
                out.append(SourceEvent(_clip(text), self.label, time.time()))
        self.state["seen"] = seen[-SEEN_KEPT:]
        return out


class SmsSource(ProviderSource):
    """Text messages that reach a modem or a phone, as "SMS from <sender>: <text>". The provider gives {"id", "from", "text"}."""
    kind = "sms"
    prefix = "sms"

    def describe(self, item: Dict[str, Any]) -> Optional[str]:
        text = str(item.get("text", "")).strip()
        return f"SMS from {str(item.get('from') or 'unknown sender')}: {text}" if text else None


class CalendarSource(ProviderSource):
    """Appointments of a calendar that begin within `lead_hours`, as "Calendar: <title> at <time>". The provider gives {"id", "title", "start"}
    with `start` in seconds since 1970; one that is later or already past is left for another look."""
    kind = "calendar"
    prefix = "calendar"

    def __init__(self, source_id: str, fetch: Callable[[], List[Dict[str, Any]]], interval: float = 900.0, state: Optional[Dict[str, Any]] = None,
                 lead_hours: float = 24.0, now: Callable[[], float] = time.time) -> None:
        super().__init__(source_id, fetch, interval, state)
        self.lead_hours, self._now = lead_hours, now

    def items(self) -> List[Dict[str, Any]]:
        now = self._now()
        return [i for i in self._fetch() if isinstance(i, dict) and isinstance(i.get("start"), (int, float)) and not isinstance(i.get("start"), bool)
                and 0 <= i["start"] - now <= self.lead_hours * 3600]

    def describe(self, item: Dict[str, Any]) -> Optional[str]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["start"]))
        return f"Calendar: {str(item.get('title') or '(no title)')} at {when}"
