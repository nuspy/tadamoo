"""A schedule written in words, Italian or English, made into the schedule text the routines use (tadamoo/scheduler.py `Schedule`).

"ogni 6 ore" -> `every 6h`; "ogni primo lunedì alle 9" -> `cron 0 9 * * 1#1`; "every last Sunday of the month at 3pm" -> `cron 0 15 * * 0L`.
A schedule already in the routines' own form (`every 2h`, `daily 08:30`, `cron ...`) is kept as it is. With no time of day a fixed day is at 09:00.
Nothing is guessed: what is not understood is refused with an example."""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

from tadamoo.planner.dag import PlanError
from tadamoo.scheduler import RoutineError, Schedule

DEFAULT_TIME = (9, 0)
EXAMPLE = "for example 'every 6 hours', 'every 2 days', 'every first Monday at 9', 'every last Sunday of the month at 15:00', 'on the 15th of every month'"

NUMBERS = {"un": 1, "uno": 1, "una": 1, "one": 1, "due": 2, "two": 2, "tre": 3, "three": 3, "quattro": 4, "four": 4, "cinque": 5, "five": 5, "sei": 6, "six": 6,
           "sette": 7, "seven": 7, "otto": 8, "eight": 8, "nove": 9, "nine": 9, "dieci": 10, "ten": 10, "undici": 11, "eleven": 11, "dodici": 12, "twelve": 12,
           "quindici": 15, "fifteen": 15, "venti": 20, "twenty": 20, "trenta": 30, "thirty": 30}
ORDINALS = {"primo": 1, "prima": 1, "first": 1, "1st": 1, "secondo": 2, "seconda": 2, "second": 2, "2nd": 2, "terzo": 3, "terza": 3, "third": 3, "3rd": 3,
            "quarto": 4, "quarta": 4, "fourth": 4, "4th": 4, "quinto": 5, "quinta": 5, "fifth": 5, "5th": 5, "ultimo": -1, "ultima": -1, "last": -1}
WEEKDAYS = {"lunedi": 0, "monday": 0, "mon": 0, "martedi": 1, "tuesday": 1, "tue": 1, "mercoledi": 2, "wednesday": 2, "wed": 2, "giovedi": 3, "thursday": 3, "thu": 3,
            "venerdi": 4, "friday": 4, "fri": 4, "sabato": 5, "saturday": 5, "sat": 5, "domenica": 6, "sunday": 6, "sun": 6}
DAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
UNIT_SECONDS = {"secondo": "s", "secondi": "s", "second": "s", "seconds": "s", "sec": "s", "minuto": "m", "minuti": "m", "minute": "m", "minutes": "m", "min": "m",
                "ora": "h", "ore": "h", "hour": "h", "hours": "h", "giorno": "d", "giorni": "d", "day": "d", "days": "d",
                "settimana": "w", "settimane": "w", "week": "w", "weeks": "w"}
EVERY = {"ogni", "every", "each", "tutti", "tutte"}
FILLER = {"del", "dello", "della", "di", "a", "al", "alle", "ore", "il", "lo", "la", "le", "i", "gli", "l", "e", "ed", "and", "the", "of", "at", "on", "in", "nel", "un", "una", "uno", "an",
          "mese", "mesi", "month", "months", "giorno", "giorni", "day", "days", "feriali", "feriale", "lavorativi", "lavorativo", "weekdays", "weekday", "weekend", "weekends", "fine",
          "settimana", "settimane", "working", "mezz", "mezza", "half", "hour", "daily", "quotidiano", "quotidianamente", "ora", "mezzogiorno", "noon", "mezzanotte", "midnight"}
WORD = re.compile(r"[a-z0-9:.°]+")


def _plain(text: str) -> str:
    """Lower case, no accents, apostrophes as spaces."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[’'`,;]", " ", text).split())


def _canonical(text: str) -> str:
    """The routines' own spelling: lower case, with the last weekday of a month as `dL`."""
    return re.sub(r"(\d)l\b", r"\1L", " ".join(text.lower().split()))


def _number(word: str) -> Optional[int]:
    if word.isdigit():
        return int(word)
    return NUMBERS.get(word)


def _ordinal(word: str) -> Optional[int]:
    word = word.rstrip("°").replace("º", "")
    if word in ORDINALS:
        return ORDINALS[word]
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th|o|a)$", word)
    return int(m[1]) if m else None


def _time_of_day(text: str) -> Tuple[Optional[Tuple[int, int]], str]:
    """The time of day named in `text` and the text without it."""
    pm = bool(re.search(r"\b(pm|pomeriggio|sera|stasera|afternoon|evening)\b", text))
    am = bool(re.search(r"\b(am|mattina|mattino|morning)\b", text))
    for pattern, value in ((r"\b(mezzogiorno|noon)\b", (12, 0)), (r"\b(mezzanotte|midnight)\b", (0, 0))):
        m = re.search(pattern, text)
        if m:
            return value, text.replace(m[0], " ", 1)
    m = re.search(r"\b(?:alle|alle ore|ore|all|at|@)\s*(\d{1,2}|" + "|".join(NUMBERS) + r")(?:\s*[:.h]\s*(\d{2}))?\s*(am|pm)?\b", text)
    if not m:
        m = re.search(r"\b(\d{1,2})\s*[:.h]\s*(\d{2})\s*(am|pm)?\b", text)
    if not m:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", text)
        if m:
            m = re.match(r"(\d{1,2})()\s*(am|pm)", m[0])
    if not m:
        return None, text
    hour = _number(m[1]) if not m[1].isdigit() else int(m[1])
    minute = int(m[2]) if m[2] else 0
    suffix = m[3] if m.lastindex and m.lastindex >= 3 else None
    if hour is None or hour > 23 or minute > 59:
        raise PlanError(f"'{m[0].strip()}' is not a time of day: use HH:MM, from 00:00 to 23:59")
    if (suffix == "pm" or (pm and not suffix)) and hour < 12:
        hour += 12
    if (suffix == "am" or am) and hour == 12 and not pm:
        hour = 0
    cleaned = text.replace(m[0], " ", 1)
    cleaned = re.sub(r"\b(di|del|nel|in|the)?\s*(pomeriggio|sera|stasera|mattina|mattino|afternoon|evening|morning)\b", " ", cleaned) if (pm or am) else cleaned
    return (hour, minute), cleaned


def _interval(words: List[str]) -> Optional[str]:
    """`every 6h` from "ogni 6 ore" / "every two days"; None when the words are not an interval."""
    rest = [w for w in words if w not in EVERY and w not in ("a", "an")]
    if rest and rest[0] in ("mezz", "mezza", "half"):
        if len(rest) <= 2 and (len(rest) == 1 or rest[1] in ("ora", "hour")):
            return "every 30m"
        return None
    count, unit = 1, None
    if len(rest) == 1 and rest[0] in UNIT_SECONDS:
        unit = rest[0]
    elif len(rest) == 2 and _number(rest[0]) is not None and rest[1] in UNIT_SECONDS:
        count, unit = _number(rest[0]), rest[1]
    if unit is None:
        return None
    letter = UNIT_SECONDS[unit]
    if letter == "w":
        count, letter = count * 7, "d"
    if count < 1:
        raise PlanError("an interval is at least one second")
    return f"every {count}{letter}"


def to_schedule(words: str) -> Tuple[str, str]:
    """(schedule text for `Schedule.parse`, the same schedule said plainly). Raises PlanError with what to write when the words are not understood."""
    if not isinstance(words, str) or not words.strip() or len(words) > 200:
        raise PlanError(f"say when it starts in up to 200 characters, {EXAMPLE}")
    given = " ".join(words.split())
    try:
        Schedule.parse(given)
        return _canonical(given), _canonical(given)
    except RoutineError:
        pass
    if len(given.split()) == 5 and re.match(r"^[\d*/,\-#lL]+( [\d*/,\-#lLjanfebmrpyulgsoctvd]+){4}$", given):
        try:
            Schedule.parse("cron " + given)
            return _canonical("cron " + given), _canonical("cron " + given)
        except RoutineError:
            pass
    text = _plain(given)
    when, text = _time_of_day(text)
    words_ = WORD.findall(text)
    interval = _interval(words_)
    if interval == "every 1d":                                  # "every day" is a day, at a time
        interval = None
    if interval is not None:
        if when is not None:
            raise PlanError("an interval has no time of day: 'every 2 days' starts 2 days after the last start; for a fixed time say the day, like 'every Monday at 9'")
        return interval, interval
    for w in words_:                                            # nothing is guessed: a word that means nothing here is refused
        if not (_number(w) is not None or _ordinal(w) is not None or w in WEEKDAYS or w in FILLER or w in EVERY or w in UNIT_SECONDS or re.match(r"^\d{1,2}(?:st|nd|rd|th|o|a|°)?$", w)):
            raise PlanError(f"'{w}' is not a word of a schedule I understand: write it {EXAMPLE}")
    hour, minute = when or DEFAULT_TIME
    clock = f"{hour:02d}:{minute:02d}"
    names = [WEEKDAYS[w] for w in words_ if w in WEEKDAYS]
    ordinal = next((_ordinal(w) for w in words_ if _ordinal(w) is not None and not w.isdigit()), None)
    month = bool(re.search(r"\b(mese|month)\b", text))
    day_of_month = re.search(r"\b(?:il|day|on the|the|ogni|di ogni)?\s*(\d{1,2})(?:°|o|st|nd|rd|th)?\s+(?:di ogni|del|di|of every|of each|of the|every|ogni)\s+(?:mese|month)\b", text)
    if names and ordinal is not None:
        if ordinal == 0 or ordinal > 5:
            raise PlanError(f"there is no {ordinal}th weekday in a month; write first to fifth, or last")
        token = "L" if ordinal == -1 else f"#{ordinal}"
        days = ",".join(f"{(d + 1) % 7}{token}" for d in sorted(set(names)))
        text_ = f"cron {minute} {hour} * * {days}"
        return text_, text_
    if day_of_month and not names:
        day = int(day_of_month[1])
        if not 1 <= day <= 31:
            raise PlanError("a day of the month is from 1 to 31")
        text_ = f"cron {minute} {hour} {day} * *"
        return text_, text_
    if ordinal == 1 and month and not names:
        text_ = f"cron {minute} {hour} 1 * *"
        return text_, text_
    if names:
        text_ = f"{','.join(DAY_ABBR[d] for d in sorted(set(names)))} {clock}"
        return text_, text_
    if re.search(r"\b(feriali|feriale|lavorativi|lavorativo|weekdays|weekday|working days)\b", text):
        return f"weekdays {clock}", f"weekdays {clock}"
    if re.search(r"\b(weekend|weekends|fine settimana)\b", text):
        return f"weekends {clock}", f"weekends {clock}"
    if re.search(r"\b(giorno|giorni|day|days|daily|quotidiano|quotidianamente)\b", text) or any(w in EVERY for w in words_):
        if when is not None or re.search(r"\b(ogni giorno|tutti i giorni|every day|daily|quotidian)", text):
            return f"daily {clock}", f"daily {clock}"
    raise PlanError(f"'{given[:80]}' is not a schedule I understand: write it {EXAMPLE}")
