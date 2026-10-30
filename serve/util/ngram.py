"""A guard for a passage that comes back again and again in a reply (a loop longer than one token)."""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Tuple


class NgramLoop:
    """Tells when the last `n` tokens have already been written `times` times, apart from each other, within the last `window`
    tokens. A group of at most 3 different tokens (a table rule, a row of dashes) is never counted: only a passage is a loop."""

    def __init__(self, n: int = 12, times: int = 4, window: int = 2000) -> None:
        self.n, self.times, self.window = n, times, window
        self.tail: Deque[int] = deque(maxlen=n)
        self.seen: Dict[Tuple[int, ...], Deque[int]] = {}      # a group -> the positions counted for it, oldest first
        self.order: Deque[Tuple[int, Tuple[int, ...]]] = deque()
        self.pos = 0

    def push(self, token: int) -> bool:
        """Adds a token; True when it completes a group seen `times` times."""
        self.tail.append(token)
        self.pos += 1
        if len(self.tail) < self.n:
            return False
        gram = tuple(self.tail)
        if len(set(gram)) <= 3:
            return False
        positions = self.seen.setdefault(gram, deque())
        if not positions or self.pos - positions[-1] >= self.n:
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
        return len(positions) >= self.times
