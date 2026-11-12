"""A guard for a passage that comes back again and again in a reply (a loop longer than one token)."""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Tuple


class NgramLoop:
    """Tells when the last `n` tokens have already been written `times` times, apart from each other, within the last `window`
    tokens, and the stretch since the previous copy repeats the stretch before it. A group of at most 3 different tokens (a table
    rule, a row of dashes) is never counted: only a passage is a loop. Rows of a table or of a JSON list share their keys but not
    their values, so the stretches between the copies differ and nothing is counted."""

    def __init__(self, n: int = 12, times: int = 4, window: int = 2000) -> None:
        self.n, self.times, self.window = n, times, window
        self.tail: Deque[int] = deque(maxlen=n)
        self.seen: Dict[Tuple[int, ...], Deque[int]] = {}      # a group -> the positions counted for it, oldest first
        self.order: Deque[Tuple[int, Tuple[int, ...]]] = deque()
        self.pos = 0
        self.buf: list[int] = []               # the last tokens, to compare the stretches between copies
        self.start = 0                         # the position just before buf[0]
        self.first_hit: int | None = None      # when a loop is found: the position at which the first full copy of the passage ended

    def push(self, token: int) -> bool:
        """Adds a token; True when it completes a group seen `times` times."""
        self.tail.append(token)
        self.pos += 1
        self.buf.append(token)
        if len(self.buf) > 4 * (self.window + self.n):
            cut = len(self.buf) - 2 * (self.window + self.n)
            del self.buf[:cut]
            self.start += cut
        if len(self.tail) < self.n:
            return False
        gram = tuple(self.tail)
        if len(set(gram)) <= 3:
            return False
        positions = self.seen.setdefault(gram, deque())
        fresh = not positions or self.pos - positions[-1] >= self.n
        if fresh:
            positions.append(self.pos)
            self.order.append((self.pos, gram))
        floor = self.pos - self.window
        while self.order and self.order[0][0] <= floor:
            old_pos, old = self.order.popleft()
            held = self.seen.get(old)
            if held and held[0] == old_pos:
                held.popleft()
                if not held:
                    del self.seen[old]
        if fresh and len(positions) >= self.times and self._repeats(positions[-1] - positions[-2]):
            self.first_hit = max(1, positions[1] - self.n)     # the second window begins where the first copy ends
            return True
        return False

    def _repeats(self, gap: int) -> bool:
        """Whether the last `gap` tokens are (nearly) the `gap` tokens before them: a passage written again, not a shared key."""
        end = self.pos - self.start
        if gap < 1 or end - 2 * gap < 0:
            return False
        a, b = self.buf[end - gap:end], self.buf[end - 2 * gap:end - gap]
        return sum(x == y for x, y in zip(a, b)) >= 0.8 * gap
