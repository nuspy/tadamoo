"""The canvas of an overlay: its size, and the sizes of everything drawn on it given for a frame 530 pixels high and scaled with the frame, so that one design serves any format."""
from __future__ import annotations

from dataclasses import dataclass

REFERENCE_HEIGHT = 530


@dataclass(frozen=True)
class Frame:
    width: int
    height: int

    def px(self, n: float) -> int:
        return max(1, round(n * self.height / REFERENCE_HEIGHT))


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int
