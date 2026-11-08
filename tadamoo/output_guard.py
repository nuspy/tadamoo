"""Degenerate output: text that has fallen into a loop and only repeats itself.

`repetition_dominated` judges a finished fragment. `trailing_period` finds a unit that the end of a text repeats exactly. `RepetitionWatch` is
fed the pieces of a stream as they arrive, looks for an exact loop every few hundred characters from the first few thousand, and for a loose one
(`repetition_dominated`) at growing sizes, and tells the caller when to stop reading.
"""
from __future__ import annotations

from collections import Counter
from typing import Dict

WINDOW = 60                 # characters in the unit that is counted
MIN_REPEATS = 5             # how many times a unit must come back to count as a repeat
MIN_FRAGMENT = 400          # shorter text is never judged
MIN_WORD_CHARS = 15         # a unit with fewer letters and digits (a rule line, a gap of spaces) is not a repeat
DISTINCT_LINES = 0.6        # a text of at least MIN_LINES lines of which this share differ is a list or a table, not a loop
MIN_LINES = 6
LOOK_BACK = 200_000         # the longest end of a text that is looked at
WATCH_FIRST = 16_384        # characters of a stream at which the first loose look is made; then at each doubling
WATCH_TAIL = 16_384         # how much of the end of the stream a loose look covers
PERIOD_FIRST = 3_000        # characters of a stream at which the first look for an exact loop is made
PERIOD_STEP = 1_500         # then after this many new characters
PERIOD_TAIL = 6_000         # the end of the stream those looks cover
PERIOD_UNIT = 40            # the shortest unit that counts as repeated (characters)
PERIOD_REPEATS = {"reasoning": 3, "content": 4}      # copies in a row that make a loop; an answer may legitimately say a block twice or thrice
PERIOD_MAX_UNIT = 2_000
PERIOD_MIN_ALNUM = 20       # letters and digits in the unit: a rule line or a gap is not a loop
PENALTY_STEP = 0.1
PENALTY_MAX = 1.5


class RepetitionAbort(Exception):
    """Raised by a stream callback that has seen the text fall into a loop; the connection is closed and the partial answer dropped."""


def repetition_dominated(text: str) -> bool:
    """Whether at least half of `text` (400 characters or more) is made of 60-character units that each occur five times or more.
    A text whose lines mostly differ is never flagged."""
    text = text[-LOOK_BACK:].strip()
    if len(text) < MIN_FRAGMENT:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= MIN_LINES and len(set(lines)) / len(lines) >= DISTINCT_LINES:
        return False
    last = len(text) - WINDOW
    counts = Counter(text[i:i + WINDOW] for i in range(last + 1))
    covered = bytearray(len(text))
    reaches = 0                                   # the end of the stretch marked so far
    for i in range(last + 1):
        unit = text[i:i + WINDOW]
        if counts[unit] >= MIN_REPEATS and sum(c.isalnum() for c in unit) >= MIN_WORD_CHARS:
            start = max(i, reaches)
            covered[start:i + WINDOW] = b"\x01" * (i + WINDOW - start)
            reaches = i + WINDOW
    return sum(covered) * 2 >= len(text)


def trailing_period(text: str, min_unit: int = PERIOD_UNIT, repeats: int = 3, max_unit: int = PERIOD_MAX_UNIT) -> int:
    """The length of a unit that the end of `text` repeats exactly `repeats` times or more in a row (at least `min_unit` characters, with
    letters in it), or 0. Linear: the prefix function of the reversed end."""
    s = text[::-1]
    n = len(s)
    if n < min_unit * repeats:
        return 0
    pi = [0] * n
    k = 0
    for i in range(1, n):
        while k and s[i] != s[k]:
            k = pi[k - 1]
        if s[i] == s[k]:
            k += 1
        pi[i] = k
    best = 0
    for length in range(min_unit * repeats, n + 1):
        period = length - pi[length - 1]
        if min_unit <= period <= max_unit and length >= repeats * period and sum(c.isalnum() for c in s[:period]) >= PERIOD_MIN_ALNUM:
            best = max(best, period)
    return best


class RepetitionWatch:
    """Feed it the pieces of a stream; `feed` returns True when the end of what has been written is a loop. An exact loop (a unit of at least
    `unit` characters copied `repeats` times in a row) is looked for from `period_first` characters on, every `step` new characters; a loose one
    (`repetition_dominated`) when the text first passes `first` characters and again each time its length doubles. The answer and the reasoning are
    watched apart. `found` says what was seen, for the log."""

    def __init__(self, first: int = WATCH_FIRST, tail: int = WATCH_TAIL, period_first: int = PERIOD_FIRST, step: int = PERIOD_STEP,
                 unit: int = PERIOD_UNIT, repeats: Dict[str, int] | None = None, enabled: bool = True) -> None:
        self.first, self.tail, self.period_first, self.step, self.unit = first, tail, period_first, max(1, step), unit
        self.repeats = {**PERIOD_REPEATS, **(repeats or {})}
        self.enabled = enabled
        self.text: Dict[str, str] = {}
        self._next: Dict[str, int] = {}
        self._looked: Dict[str, int] = {}
        self.kind = ""                                # what was found looping: "content" or "reasoning"
        self.found: Dict[str, object] = {}

    def feed(self, kind: str, piece: str) -> bool:
        if not self.enabled:
            return False
        text = self.text.get(kind, "") + piece
        self.text[kind] = text
        if len(text) >= self.period_first and len(text) - self._looked.get(kind, 0) >= self.step:
            self._looked[kind] = len(text)
            period = trailing_period(text[-PERIOD_TAIL:], self.unit, self.repeats.get(kind, 3))
            if period:
                self.kind, self.found = kind, {"by": "exact", "kind": kind, "unit_chars": period, "chars": len(text)}
                return True
        due = self._next.setdefault(kind, self.first)
        if len(text) < due:
            return False
        self._next[kind] = max(due, len(text)) * 2
        if repetition_dominated(text[-self.tail:]):
            self.kind, self.found = kind, {"by": "loose", "kind": kind, "chars": len(text)}
            return True
        return False


def raised_penalty(current: object) -> float:
    """The repetition penalty for the one retry after a loop: a step above the session's own, or above 1 when it has none."""
    base = float(current) if isinstance(current, (int, float)) and not isinstance(current, bool) and current >= 1.0 else 1.0
    return round(min(base + PENALTY_STEP, PENALTY_MAX), 2)
