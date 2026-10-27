"""Where the person is, as their phone says it, for the tasks that depend on it.

The phone sends its position with a message (coordinates and, when it could be worked out, the address). Nothing of it goes into the
prompt: a conversation that holds a position offers the tool `where_is_user`, and the model calls it only when the task needs the place
(what is nearby, local services, telling help where the person is). The position is held in memory only.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Callable, Dict, Optional

ADDRESS_FIELDS = ("street", "number", "postcode", "city", "region", "country")
FIELD_CHARS = 60
OLD_S = 1800                    # a position older than this is called old when it is handed over
COARSE_M = 200                  # a fix wider than this is called rough
TOOL = "where_is_user"

_WORDS = re.compile(r"[^\w\s.,'’/()&°ºª-]", re.UNICODE)


def _text(value: Any) -> str:
    """One short plain line: no markup, no control characters, no room for a sentence addressed to a model."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return " ".join(_WORDS.sub(" ", str(value)).split())[:FIELD_CHARS]


def _number(value: Any, low: float, high: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and low <= value <= high else None


def clean(raw: Any, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The position as it is kept, or None when it has no usable coordinates. Unknown keys and anything that is not a short plain
    field are dropped; `got` is the time this PC received it."""
    if not isinstance(raw, dict):
        return None
    lat, lon = _number(raw.get("lat"), -90, 90), _number(raw.get("lon"), -180, 180)
    if lat is None or lon is None:
        return None
    address = raw.get("address") if isinstance(raw.get("address"), dict) else {}
    code = str(address.get("country_code") or "").strip().upper()
    held = {
        "lat": round(lat, 5), "lon": round(lon, 5),
        "accuracy_m": _number(raw.get("accuracy_m"), 0, 1_000_000),
        "fix_age_s": _number(raw.get("fix_age_s"), 0, 31 * 86400) or 0.0,
        "got": now if now is not None else time.time(),
        "address": {k: v for k, v in ((k, _text(address.get(k))) for k in ADDRESS_FIELDS) if v},
    }
    if re.fullmatch(r"[A-Z]{2}", code):
        held["address"]["country_code"] = code
    return held


def describe(held: Dict[str, Any]) -> str:
    """The address in one line, the way it is read aloud to someone."""
    a = held.get("address", {})
    parts = [" ".join(x for x in (a.get("street"), a.get("number")) if x), " ".join(x for x in (a.get("postcode"), a.get("city")) if x),
             a.get("region", ""), a.get("country", "")]
    return ", ".join(p for p in parts if p)


def age_of(held: Dict[str, Any], now: Optional[float] = None) -> int:
    return max(0, int((now if now is not None else time.time()) - held["got"] + held.get("fix_age_s", 0)))


def _ago(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds ago"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 2 * 86400:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"


class Place:
    """The last position this conversation was given."""

    def __init__(self) -> None:
        self.held: Optional[Dict[str, Any]] = None

    def set(self, raw: Any) -> bool:
        held = clean(raw, now=_number(raw.get("got"), 1, 4e9) if isinstance(raw, dict) else None)       # the server's receipt time rides along
        if held is None:
            return False
        self.held = held
        return True

    def answer(self, local: bool = True, now: Optional[float] = None) -> Dict[str, Any]:
        held = self.held
        if held is None:
            return {"known": False, "note": "the person's phone has not shared a position"}
        if not local:
            return {"known": False, "note": "the position stays on this PC and this request goes elsewhere"}
        age = age_of(held, now)
        out: Dict[str, Any] = {"known": True, "address": describe(held), "lat": held["lat"], "lon": held["lon"], "age_s": age, "age": _ago(age)}
        for key in ("city", "region", "country", "country_code"):
            if held["address"].get(key):
                out[key] = held["address"][key]
        if held.get("accuracy_m") is not None:
            out["accuracy_m"] = round(held["accuracy_m"])
        notes = []
        if not out["address"]:
            notes.append("only coordinates are known: no address could be worked out, so give the coordinates")
        if age > OLD_S:
            notes.append(f"this position is {out['age']}: the person may have moved, and say so")
        if held.get("accuracy_m") is not None and held["accuracy_m"] > COARSE_M:
            notes.append("the fix is rough, so it is the neighbourhood and not the exact spot")
        if notes:
            out["note"] = "; ".join(notes)
        return out


def register_place_tool(registry: Any, place: Place, local: Callable[[], bool] = lambda: True) -> None:
    """Offers `where_is_user`; the conversation calls this once its first position arrives, so one without a phone carries no such tool."""
    from harness.tools.registry import EffectClass

    if registry.get(TOOL) is not None:
        return
    registry.register(
        name=TOOL,
        description=("Where the person is now, from their phone: address and coordinates, and how long ago it was taken. Call it only when the task "
                     "depends on their place: what is near them, local services, directions, local weather, or telling someone where to find them "
                     "in an emergency; if you have no way to call, give the person the address and the number to call. Otherwise do not call it, "
                     "and do not bring their address into an answer that does not need it."),
        parameters={"type": "object", "properties": {}, "required": []},
        effect=EffectClass.PURE, handler=lambda: place.answer(local=local()), untrusted=True)
