"""The stored plan of meetings: what the person planned, and the choices they made for each meeting.

`meetings_plan.json` sits next to the past meetings; it is replaced whole through a temporary file, so a crash leaves the old one. An item is
{id, title, platform, url, start (UTC, ISO), duration_min, source (manual | google | microsoft), participate, role (listen | minutes | speak),
persona (a personality's slug, or ""), camera, speak, consent_mode ("" for the person's setting), notes}, plus what the planner records:
`session_id` and `joined_at` once the bot joined, `last_error` and `last_note`. The address may hold a passcode: it is stored here, never
audited. Calendar items are stored only once the person made a choice for them; the calendar owns their title, address and time."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import consent
from .adapters import detect, platform_names
from .errors import Refused

FILE = "meetings_plan.json"
ROLES = ("listen", "minutes", "speak")
SOURCES = ("manual", "google", "microsoft")
MAX_ITEMS = 200
KEEP_S = 14 * 86400
URL_MAX = 2000
EDITABLE = ("title", "platform", "url", "start", "duration_min", "participate", "role", "persona", "camera", "speak", "consent_mode", "notes")
CALENDAR_OWNED = ("title", "platform", "url", "start", "duration_min")
RECORDED = ("session_id", "joined_at", "last_error", "last_note")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")


def calendar_id(event_id: str) -> str:
    """The plan id of a calendar event: its provider's id is not a plain word."""
    return "c-" + hashlib.sha1(event_id.encode("utf-8")).hexdigest()[:16]


def parse_start(value, assume_utc: bool = False) -> tuple[str, float] | None:
    """(UTC text, timestamp) of an ISO time that names its zone; None for anything else. `assume_utc`: a time without a zone is UTC (what a
    calendar's all-day and Graph events give)."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        if not assume_utc:
            return None
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), dt.timestamp()


def start_of(item: dict) -> float | None:
    parsed = parse_start(item.get("start"))
    return parsed[1] if parsed else None


def _bad(message: str, code: str = "bad_request") -> Refused:
    return Refused(400, code, message)


def _bool(data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise _bad(f"{key} is true or false.")
    return value


def _text(value, key: str, limit: int, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value.strip()) > limit or any(ord(c) < 32 and not (multiline and c == "\n") for c in value):
        raise _bad(f"{key} is a text of at most {limit} characters.")
    return value.strip()


def clean(data, base: dict | None = None, source: str = "manual") -> dict:
    """The item after `data` is applied over `base` (nothing for a new one), each field checked; what is not named stays."""
    if not isinstance(data, dict):
        raise _bad("Send a JSON object.")
    unknown = sorted(str(k) for k in data if k not in EDITABLE)
    if unknown:
        raise _bad("Unknown field: " + ", ".join(unknown[:5]) + ".")
    if base is not None and base.get("source") in ("google", "microsoft"):
        owned = [k for k in CALENDAR_OWNED if k in data and data[k] != base.get(k)]
        if owned:
            raise _bad("The calendar owns " + ", ".join(owned) + ": change it in the calendar.", "calendar_owned")
    out = dict(base) if base is not None else {"source": source, "persona": "", "consent_mode": "", "notes": "", "duration_min": None}
    if "url" in data or base is None:
        url = data.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or len(url) > URL_MAX or any(ord(c) < 33 for c in url):
            raise _bad("Give the address of the meeting (http or https).", "bad_url")
        found = detect(url)
        if found is None:
            raise _bad("No adapter takes this address. Platforms: " + ", ".join(platform_names()) + ".", "unsupported_platform")
        out["url"], out["platform"] = url, found
    if "platform" in data and data["platform"] != out.get("platform"):
        if data["platform"] not in platform_names():
            raise _bad("No adapter for that platform. Platforms: " + ", ".join(platform_names()) + ".", "unsupported_platform")
        raise _bad(f"The address is a {out.get('platform')} meeting, not {data['platform']}.", "platform_mismatch")
    if "start" in data or base is None:
        parsed = parse_start(data.get("start"))
        if parsed is None:
            raise _bad("start is a date and time with its time zone, for example 2026-11-10T15:00:00+01:00.", "bad_start")
        out["start"] = parsed[0]
    if "title" in data or base is None:
        out["title"] = _text(data.get("title", ""), "title", 120) or out["platform"]
    if "duration_min" in data:
        d = data["duration_min"]
        if d is not None and not (isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 1440):
            raise _bad("duration_min is a whole number of minutes from 1 to 1440, or empty.")
        out["duration_min"] = d
    if "role" in data or base is None:
        role = data.get("role", out.get("role", "minutes"))
        if role not in ROLES:
            raise _bad("role is listen, minutes or speak.")
        out["role"] = role
    if "speak" in data and data["speak"] is not (out["role"] == "speak"):
        raise _bad("speak follows the role: it is on only for the role speak.")
    out["speak"] = out["role"] == "speak"
    out["participate"] = _bool(data, "participate", out.get("participate", False))
    out["camera"] = _bool(data, "camera", out.get("camera", False))
    if "persona" in data:
        if not isinstance(data["persona"], str) or (data["persona"] and not SLUG.fullmatch(data["persona"])):
            raise _bad("persona is the name (slug) of a personality, or empty.")
        out["persona"] = data["persona"]
    if "consent_mode" in data:
        if data["consent_mode"] not in ("", *consent.MODES):
            raise _bad("consent_mode is window, explicit, or empty for the setting.")
        out["consent_mode"] = data["consent_mode"]
    if "notes" in data:
        out["notes"] = _text(data["notes"], "notes", 500, multiline=True)
    return out


class PlanStore:
    def __init__(self, path: Path, clock=time.time) -> None:
        self.path, self._clock = Path(path), clock
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rows = data.get("items") if isinstance(data, dict) else None
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and isinstance(row.get("id"), str) and ID.fullmatch(row["id"]) and parse_start(row.get("start")):
                    self._items[row["id"]] = row
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "items": list(self._items.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._items.values()]

    def get(self, item_id: str) -> dict | None:
        with self._lock:
            item = self._items.get(item_id)
            return dict(item) if item else None

    def add(self, data, source: str = "manual", item_id: str = "", extra: dict | None = None) -> dict:
        with self._lock:
            if len(self._items) >= MAX_ITEMS:
                raise Refused(409, "plan_full", f"The plan holds at most {MAX_ITEMS} meetings: delete old ones.")
            item = clean(data, None, source)
            item.update(extra or {})
            item["id"] = item_id or "p" + uuid.uuid4().hex[:12]
            item["created"] = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._items[item["id"]] = item
            self._save()
            return dict(item)

    def put(self, item: dict) -> dict:
        """Store a complete, already checked item (a calendar item the person made a first choice for)."""
        with self._lock:
            if item["id"] not in self._items and len(self._items) >= MAX_ITEMS:
                raise Refused(409, "plan_full", f"The plan holds at most {MAX_ITEMS} meetings: delete old ones.")
            item.setdefault("created", datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            self._items[item["id"]] = item
            self._save()
            return dict(item)

    def update(self, item_id: str, changes) -> dict:
        with self._lock:
            base = self._items.get(item_id)
            if base is None:
                raise Refused(404, "not_found", "No such meeting in the plan.")
            item = clean(changes, base)
            item.pop("last_error", None)                        # a changed item is tried again from a clean slate
            self._items[item_id] = item
            self._save()
            return dict(item)

    def mark(self, item_id: str, **fields) -> None:
        """Record what the planner did (RECORDED fields only)."""
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return
            before = {k: item.get(k) for k in fields}
            for key, value in fields.items():
                if key not in RECORDED:
                    raise KeyError(key)
                if value in ("", None):
                    item.pop(key, None)
                else:
                    item[key] = value
            if before != {k: item.get(k) for k in fields}:
                self._save()

    def refresh_snapshot(self, item_id: str, fields: dict) -> None:
        """A calendar item's title, address, time and platform as the calendar says them now."""
        with self._lock:
            item = self._items.get(item_id)
            if item is not None and any(item.get(k) != v for k, v in fields.items()):
                item.update(fields)
                self._save()

    def delete(self, item_id: str) -> None:
        with self._lock:
            if self._items.pop(item_id, None) is None:
                raise Refused(404, "not_found", "No such meeting in the plan.")
            self._save()

    def prune(self) -> int:
        """Forget what started more than KEEP_S ago."""
        with self._lock:
            old = [i for i, item in self._items.items() if (start_of(item) or 0) < self._clock() - KEEP_S]
            for i in old:
                del self._items[i]
            if old:
                self._save()
            return len(old)
