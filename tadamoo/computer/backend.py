"""What the computer tools need from the machine: the screens, a picture of an area, the windows, the accessibility tree of a window, the pointer and
the keyboard. One class per system (windows.py, x11.py); the tools (tools.py) and the session (session.py) know nothing of the system, so they are
tried with a backend that only records what it was asked (tests/test_computer.py).

Every position is in pixels of the whole desktop, the origin at the top left of the virtual screen (a second screen to the left has negative x).
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

MODIFIERS = ("ctrl", "shift", "alt", "win")
_MODIFIER_NAMES = {"control": "ctrl", "ctl": "ctrl", "cmd": "win", "command": "win", "super": "win", "meta": "win", "windows": "win", "option": "alt", "opt": "alt"}
_KEY_NAMES = {"esc": "escape", "return": "enter", "del": "delete", "ins": "insert", "pgup": "pageup", "pgdn": "pagedown", "pagedown": "pagedown", "spacebar": "space",
              "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right", "plus": "+"}
NAMED_KEYS = ("enter", "tab", "escape", "backspace", "delete", "space", "home", "end", "pageup", "pagedown", "up", "down", "left", "right", "insert",
              *(f"f{n}" for n in range(1, 13)))
PUNCTUATION = "-=[];',./`\\+"


class ComputerError(RuntimeError):
    """Something the computer tools could not do; the message is for the model and says what to change."""


@dataclass(frozen=True)
class Monitor:
    index: int                # 1, 2, ... from the left
    left: int
    top: int
    width: int
    height: int
    primary: bool = False

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass
class Window:
    id: int
    title: str
    app: str
    left: int
    top: int
    width: int
    height: int
    foreground: bool = False
    minimized: bool = False


@dataclass
class UiNode:
    id: int
    role: str
    name: str
    left: int
    top: int
    width: int
    height: int
    depth: int = 0
    enabled: bool = True
    value: str = ""


def parse_combo(text: str) -> List[str]:
    """`Ctrl+Shift+S` as ["ctrl", "shift", "s"]: the modifiers, then one key (a letter, a digit, a named key, F1-F12 or one punctuation mark). A lone key is fine."""
    parts = [p.strip().lower() for p in str(text).replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ComputerError("a key combination is a key or modifiers and a key, for example Enter, Ctrl+C or Alt+Tab")
    parts = [_MODIFIER_NAMES.get(p, p) for p in parts]
    parts = [_KEY_NAMES.get(p, p) for p in parts]
    *mods, key = parts
    for m in mods:
        if m not in MODIFIERS:
            raise ComputerError(f"'{m}' is not a modifier: use ctrl, shift, alt or win before the last key")
    if len(set(mods)) != len(mods):
        raise ComputerError("a modifier is named twice")
    valid = key in MODIFIERS or key in NAMED_KEYS or (len(key) == 1 and (key.isalnum() and key.isascii() or key in PUNCTUATION))
    if not valid:
        raise ComputerError(f"'{key}' is not a key the tool knows: a letter, a digit, {', '.join(NAMED_KEYS[:15])}... or F1-F12")
    return [*mods, key]


class Backend:
    """The machine as the computer tools see it. A method a system cannot do raises ComputerError saying so."""

    name = "none"

    def unavailable(self) -> str:
        """Why this backend cannot work here (what to install), or '' when it can."""
        return ""

    def monitors(self) -> List[Monitor]:
        raise ComputerError("this system gives no list of screens")

    def capture(self, left: int, top: int, width: int, height: int, out_width: int, out_height: int) -> bytes:
        """A PNG of the area, scaled to out_width x out_height."""
        raise ComputerError("this system gives no pictures of the screen")

    def windows(self) -> List[Window]:
        raise ComputerError("this system gives no list of windows")

    def focus(self, window_id: int) -> None:
        raise ComputerError("this system cannot bring a window to the front")

    def ui_tree(self, window_id: int, limit: int) -> List[UiNode]:
        raise ComputerError("this system gives no accessibility tree of a window: use a screenshot")

    def cursor(self) -> Tuple[int, int]:
        raise ComputerError("this system does not tell where the mouse is")

    def move_to(self, x: int, y: int) -> None:
        raise ComputerError("this system cannot move the mouse")

    def button(self, name: str, down: bool) -> None:
        raise ComputerError("this system cannot press a mouse button")

    def wheel(self, dx: int, dy: int) -> None:
        """One notch of the wheel per unit: dy > 0 scrolls down, dx > 0 scrolls right."""
        raise ComputerError("this system cannot turn the mouse wheel")

    def key(self, name: str, down: bool) -> None:
        raise ComputerError("this system cannot press a key")

    def char(self, ch: str) -> None:
        raise ComputerError("this system cannot type text")


class UnsupportedBackend(Backend):
    name = "unsupported"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def unavailable(self) -> str:
        return self.reason


def pick_backend() -> Backend:
    """The backend of this system: Windows; Linux with X11 and xdotool; anything else is said not to be supported."""
    if sys.platform == "win32":
        from tadamoo.computer.windows import WindowsBackend
        return WindowsBackend()
    if sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY"):
            wayland = os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
            return UnsupportedBackend("this Linux session has no X11 display" + (" (it is Wayland, which does not let a program move the mouse or read other windows)" if wayland else "")
                                      + ": computer control needs an X11 desktop")
        if not shutil.which("xdotool"):
            return UnsupportedBackend("xdotool is not installed (on Debian and Ubuntu: apt install xdotool)")
        from tadamoo.computer.x11 import X11Backend
        return X11Backend()
    return UnsupportedBackend(f"computer control is not supported on this system ({sys.platform}); it works on Windows and on Linux with X11")
