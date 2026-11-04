"""Degenerate output: text that has fallen into a loop and only repeats itself.

`repetition_dominated` judges a finished fragment. `RepetitionWatch` is fed the pieces of a stream as they arrive, looks at the end of
the text at growing sizes, and tells the caller when to stop reading.
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
WATCH_FIRST = 16_384        # characters of a stream at which the first look is made; then at each doubling
WATCH_TAIL = 16_384         # how much of the end of the stream a look covers
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


class RepetitionWatch:
    """Feed it the pieces of a stream; `feed` returns True when the end of what has been written is a loop. A text is looked at when it
    first passes WATCH_FIRST characters and again each time its length doubles; the answer and the reasoning are watched apart."""

    def __init__(self, first: int = WATCH_FIRST, tail: int = WATCH_TAIL) -> None:
        self.first, self.tail = first, tail
        self.text: Dict[str, str] = {}
        self._next: Dict[str, int] = {}
        self.kind = ""                                # what was found looping: "content" or "reasoning"

    def feed(self, kind: str, piece: str) -> bool:
        text = self.text.get(kind, "") + piece
        self.text[kind] = text
        due = self._next.setdefault(kind, self.first)
        if len(text) < due:
            return False
        self._next[kind] = max(due, len(text)) * 2
        if repetition_dominated(text[-self.tail:]):
            self.kind = kind
            return True
        return False


def raised_penalty(current: object) -> float:
    """The repetition penalty for the one retry after a loop: a step above the session's own, or above 1 when it has none."""
    base = float(current) if isinstance(current, (int, float)) and not isinstance(current, bool) and current >= 1.0 else 1.0
    return round(min(base + PENALTY_STEP, PENALTY_MAX), 2)
