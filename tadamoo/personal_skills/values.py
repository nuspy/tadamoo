"""The parameters of a personal skill: their types, how a value the person writes is read, and how dates are said in words.

A parameter is `{"name", "label", "explanation", "type", "default", "required", ...}`. The types are text, number, date, datetime,
choice (with `choices`), list (with `item_type`: number, text or mixed), path and bool. A date or a date and time can be written as a
date or in words (yesterday, now, in 3 days, next monday); a default written in words is worked out each time the skill runs, so
a skill that is scheduled gets the day it runs on."""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

TYPES = ("text", "number", "date", "datetime", "choice", "list", "path", "bool")
ITEM_TYPES = ("number", "text", "mixed")
NAME = re.compile(r"^[a-z_][a-z0-9_]{0,39}$")
MAX_TEXT = 20000

_WEEKDAYS = {
    "lunedi": 0, "lunedì": 0, "monday": 0, "martedi": 1, "martedì": 1, "tuesday": 1, "mercoledi": 2, "mercoledì": 2, "wednesday": 2,
    "giovedi": 3, "giovedì": 3, "thursday": 3, "venerdi": 4, "venerdì": 4, "friday": 4, "sabato": 5, "saturday": 5,
    "domenica": 6, "sunday": 6,
}
_MONTHS = {
    "gennaio": 1, "january": 1, "jan": 1, "febbraio": 2, "february": 2, "feb": 2, "marzo": 3, "march": 3, "mar": 3,
    "aprile": 4, "april": 4, "apr": 4, "maggio": 5, "may": 5, "giugno": 6, "june": 6, "jun": 6, "luglio": 7, "july": 7, "jul": 7,
    "agosto": 8, "august": 8, "aug": 8, "settembre": 9, "september": 9, "sep": 9, "sept": 9, "ottobre": 10, "october": 10, "oct": 10,
    "novembre": 11, "november": 11, "nov": 11, "dicembre": 12, "december": 12, "dec": 12,
}
_UNITS = {"minut": "minutes", "minute": "minutes", "min": "minutes", "or": "hours", "hour": "hours", "ora": "hours", "giorn": "days", "day": "days",
          "settiman": "weeks", "week": "weeks", "mes": "months", "month": "months", "ann": "years", "year": "years"}


class ValueError_(ValueError):
    """A value that does not fit its parameter; the message says what is wrong."""


def _unit(word: str) -> Optional[str]:
    word = word.lower()
    for stem, unit in _UNITS.items():
        if word.startswith(stem):
            return unit
    return None


def _shift(base: datetime, n: int, unit: str) -> datetime:
    if unit == "minutes":
        return base + timedelta(minutes=n)
    if unit == "hours":
        return base + timedelta(hours=n)
    if unit == "days":
        return base + timedelta(days=n)
    if unit == "weeks":
        return base + timedelta(weeks=n)
    months = (base.year * 12 + base.month - 1) + (n if unit == "months" else 12 * n)
    year, month = divmod(months, 12)
    day = min(base.day, calendar.monthrange(year, month + 1)[1])
    return base.replace(year=year, month=month + 1, day=day)


def parse_when(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """A date or a moment given as a date or in words (Italian or English); None when it is neither."""
    now = now or datetime.now()
    t = " ".join(str(text).strip().lower().replace("’", "'").split())
    if not t:
        return None
    clock: Optional[Tuple[int, int]] = None
    m = re.search(r"(?:^|\s|T)(\d{1,2})[:.](\d{2})(?::\d{2})?$", t)
    if m and 0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60:
        clock, t = (int(m.group(1)), int(m.group(2))), t[:m.start()].strip()
        t = re.sub(r"\s*(alle|ore|at|@)$", "", t).strip()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def at(day: datetime) -> datetime:
        return day.replace(hour=clock[0], minute=clock[1]) if clock else day

    if t in ("", "oggi", "today"):
        return at(midnight) if (t or clock) else None
    if t in ("adesso", "ora", "ora attuale", "now", "right now", "subito"):
        return at(now.replace(second=0, microsecond=0)) if clock else now.replace(microsecond=0)
    fixed = {"ieri": -1, "yesterday": -1, "l'altro ieri": -2, "laltro ieri": -2, "altroieri": -2, "the day before yesterday": -2,
             "domani": 1, "tomorrow": 1, "dopodomani": 2, "the day after tomorrow": 2}
    if t in fixed:
        return at(midnight + timedelta(days=fixed[t]))
    m = re.fullmatch(r"(?:(tra|fra|in)\s+)?(\d+)\s+([a-zà-ù]+)(?:\s+(fa|ago|from now))?", t)
    if m and _unit(m.group(3)):
        n = int(m.group(2))
        if m.group(4) in ("fa", "ago"):
            n = -n
        elif not (m.group(1) or m.group(4)):
            return None
        unit = _unit(m.group(3))
        base = now.replace(microsecond=0) if unit in ("minutes", "hours") else midnight
        return at(_shift(base, n, unit)) if clock and unit not in ("minutes", "hours") else _shift(base, n, unit)
    m = re.fullmatch(r"([a-zì]+)\s+(prossimo|prossima|next|scorso|scorsa|last)", t)
    m2 = re.fullmatch(r"(next|last)\s+([a-zì]+)", t)
    if m2:
        m = None
    if (m and m.group(1) in _WEEKDAYS) or (m2 and m2.group(2) in _WEEKDAYS):
        word, which = (m.group(1), m.group(2)) if m else (m2.group(2), m2.group(1))
        target, forward = _WEEKDAYS[word], which in ("prossimo", "prossima", "next")
        ahead, back = (target - midnight.weekday()) % 7 or 7, (midnight.weekday() - target) % 7 or 7
        return at(midnight + timedelta(days=ahead) if forward else midnight - timedelta(days=back))
    if t in ("inizio mese", "start of month", "first of the month", "start of the month"):
        return at(midnight.replace(day=1))
    if t in ("fine mese", "end of month", "end of the month"):
        return at(midnight.replace(day=calendar.monthrange(midnight.year, midnight.month)[1]))
    if t in ("inizio settimana", "start of week", "start of the week"):
        return at(midnight - timedelta(days=midnight.weekday()))
    if t in ("inizio anno", "start of year", "start of the year"):
        return at(midnight.replace(month=1, day=1))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t) or re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", t)
    if m:
        a, b, c = (int(g) for g in m.groups())
        y, mo, d = (a, b, c) if a > 31 else (c, b, a)
        try:
            return at(datetime(y, mo, d))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})\s+([a-zà-ù]+)\.?(?:\s+(\d{4}))?", t) or re.fullmatch(r"([a-zà-ù]+)\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?", t)
    if m:
        first, second, year = m.groups()
        day, month = (int(first), _MONTHS.get(second)) if first.isdigit() else (int(second), _MONTHS.get(first))
        if month:
            try:
                return at(datetime(int(year) if year else now.year, month, day))
            except ValueError:
                return None
    return None


def _number(raw: str) -> Any:
    s = raw.strip().replace(" ", "")
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    if re.fullmatch(r"[+-]?\d+,\d+", s):
        s = s.replace(",", ".")
    elif re.fullmatch(r"[+-]?\d{1,3}(\.\d{3})+,\d+", s):
        s = s.replace(".", "").replace(",", ".")
    try:
        value = float(s)
    except ValueError:
        raise ValueError_(f"'{raw}' is not a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError_(f"'{raw}' is not a number")
    return value


def _items(raw: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"[;\n]+", raw)]
    if len([p for p in parts if p]) <= 1:
        parts = [p.strip() for p in re.split(r",(?!\d)|,\s", raw)]
    return [p for p in parts if p]


def parse_value(spec: Dict[str, Any], raw: Any, now: Optional[datetime] = None) -> Any:
    """The value of a parameter, in the form it is kept and handed to the script: text, a number, an ISO date or date and time, a
    choice, a list, a path or true/false. Raises ValueError_ with what is wrong."""
    kind = spec.get("type", "text")
    if kind not in TYPES:
        raise ValueError_(f"unknown type '{kind}'")
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "yes", "si", "sì", "y", "1", "on"):
            return True
        if s in ("false", "no", "n", "0", "off"):
            return False
        raise ValueError_(f"'{raw}' is not yes or no")
    if kind == "list":
        if isinstance(raw, (list, tuple)):
            parts = [x if isinstance(x, (int, float)) and not isinstance(x, bool) else str(x).strip() for x in raw]
        else:
            parts = _items(str(raw))
        item = spec.get("item_type", "text")
        if item not in ITEM_TYPES:
            raise ValueError_(f"a list holds numbers, texts or both, not '{item}'")
        out: List[Any] = []
        for p in parts:
            if item == "number":
                out.append(_number(str(p)))
            elif item == "mixed":
                try:
                    out.append(_number(str(p)))
                except ValueError_:
                    out.append(str(p))
            else:
                out.append(str(p))
        return out
    text = raw if isinstance(raw, str) else str(raw)
    if kind in ("text", "path"):
        if len(text) > MAX_TEXT:
            raise ValueError_(f"too long ({len(text)} characters; at most {MAX_TEXT})")
        return text
    text = text.strip()
    if kind == "number":
        return _number(text)
    if kind == "choice":
        choices = [str(c) for c in spec.get("choices") or []]
        for c in choices:
            if c == text or c.lower() == text.lower():
                return c
        raise ValueError_(f"'{text}' is not one of: {', '.join(choices)}")
    when = parse_when(text, now)
    if when is None:
        raise ValueError_(f"'{text}' is not a date" + ("" if kind == "date" else " or a moment") + " (write it as 2026-10-26, 26/10/2026 or in words: yesterday, now, in 3 days)")
    return when.date().isoformat() if kind == "date" else when.replace(microsecond=0).isoformat(timespec="minutes" if not when.second else "seconds")


def check_spec(spec: Any) -> List[str]:
    """What is wrong with the definition of a parameter (empty when it is fine)."""
    if not isinstance(spec, dict):
        return ["a parameter is an object"]
    problems = []
    if not NAME.match(str(spec.get("name", ""))):
        problems.append("the name of a parameter is lower-case letters, digits and _, from a letter, up to 40")
    if spec.get("type", "text") not in TYPES:
        problems.append(f"{spec.get('name')}: the type is one of {', '.join(TYPES)}")
    if spec.get("type") == "choice" and not [c for c in spec.get("choices") or [] if str(c).strip()]:
        problems.append(f"{spec.get('name')}: a choice needs the values to choose from")
    if spec.get("type") == "list" and spec.get("item_type", "text") not in ITEM_TYPES:
        problems.append(f"{spec.get('name')}: a list holds {', '.join(ITEM_TYPES)}")
    if "default" in spec and spec["default"] not in (None, ""):
        try:
            parse_value(spec, spec["default"])
        except ValueError_ as exc:
            problems.append(f"{spec.get('name')}: the default does not fit ({exc})")
    return problems


def resolve(specs: List[Dict[str, Any]], given: Dict[str, Any], now: Optional[datetime] = None,
            last: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """The values for a run: what the person gave, else the default (a date written in words is worked out now), else the value
    used last time (`last`). Returns ({name: {"type", "value"}}, {name: what is wrong}). A required parameter with no value at all does
    not stop the run: it comes back with `value` None and `missing` True, for the run to tell the model."""
    now = now or datetime.now()
    values: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    def blank(v: Any) -> bool:
        return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, tuple)) and not v)

    for spec in specs:
        name, kind = spec["name"], spec.get("type", "text")
        raw = given.get(name)
        if blank(raw):
            raw = spec.get("default")
        if blank(raw) and last:
            raw = last.get(name)
        if blank(raw):
            values[name] = {"type": kind, "value": None, **({"missing": True} if spec.get("required", True) else {})}
            continue
        try:
            values[name] = {"type": kind, "value": parse_value(spec, raw, now)}
        except ValueError_ as exc:
            if raw is not given.get(name) and last and raw is last.get(name):         # a value kept from before that no longer fits is dropped
                values[name] = {"type": kind, "value": None, **({"missing": True} if spec.get("required", True) else {})}
            else:
                errors[name] = str(exc)
    extra = sorted(set(given) - {s["name"] for s in specs})
    for name in extra:
        errors[name] = "is not a parameter of this skill"
    return values, errors
