"""Interfaces of the desktop layer: global hotkeys, clipboard, foreground app, overlay.

Hotkeys are written as platform-neutral names ("ctrl+alt+up"); each platform
maps the names to its own key codes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol


log = logging.getLogger("callbridge.desktop")


class DesktopUnsupported(RuntimeError):
    """The desktop feature cannot work here; the message says why."""


_MOD_NAMES = {"alt": "alt", "ctrl": "ctrl", "control": "ctrl", "shift": "shift",
              "win": "win", "super": "win", "meta": "win"}
_KEY_ALIASES = {"spazio": "space", "escape": "esc", "return": "enter"}

KEY_NAMES = frozenset(
    ["space", "tab", "enter", "esc", "left", "up", "right", "down"]
    + list("abcdefghijklmnopqrstuvwxyz0123456789")
    + [f"f{i}" for i in range(1, 13)])


def parse_hotkey(hk: str) -> tuple[frozenset[str], str | None]:
    """"alt+space" -> ({"alt"}, "space"). The key is None when it is unknown."""
    mods: set[str] = set()
    key: str | None = None
    for tok in (hk or "").lower().replace(" ", "").split("+"):
        if not tok:
            continue
        if tok in _MOD_NAMES:
            mods.add(_MOD_NAMES[tok])
        else:
            tok = _KEY_ALIASES.get(tok, tok)
            key = tok if tok in KEY_NAMES else None
    return frozenset(mods), key


@dataclass
class Binding:
    """A key combination to watch.

    `exact`: the modifiers held must be exactly `mods`.
    `repeat`: the auto-repeat of the held key calls `on_press` again.
    `suppress`: the key must not reach the focused app (only where the
    platform can do it, see `Desktop.can_suppress`).
    """
    mods: frozenset[str]
    key: str
    on_press: Callable[[], None]
    on_release: Callable[[], None] | None = None
    exact: bool = False
    repeat: bool = False
    suppress: bool = True


class KeyHook(Protocol):
    """Global hotkeys with their own thread; callbacks run in a worker, in order."""

    def start(self) -> bool: ...

    def stop(self) -> None: ...


class ForegroundApp(Protocol):
    def identity(self) -> tuple[str, str]:
        """(window class, program name) of the focused window, lowercase; "" when unknown."""

    def is_terminal(self) -> bool: ...


class Clipboard(Protocol):
    def sequence(self) -> int:
        """Counter that changes whenever the clipboard content changes."""

    def read(self, honor_exclude: bool = True) -> tuple[str, object] | None:
        """("text", str) | ("image", PIL.Image) | ("excluded", None) | None."""

    def write_text(self, text: str, exclude: bool = False) -> int:
        """Returns the new sequence number, 0 when the write failed."""

    def write_image(self, img, exclude: bool = False) -> int: ...

    def send_copy(self) -> None:
        """Copies the selection of the focused app (terminal-safe keys)."""

    def send_paste(self) -> None:
        """Pastes into the focused app (terminal-safe keys)."""

    def modifiers_down(self) -> bool:
        """True while Ctrl, Alt, Shift or Win is physically held."""


class Overlay(Protocol):
    """Small always-on-top status and preview card; `mainloop` runs on the main thread."""

    def set_state_fn(self, fn: Callable[[], str]) -> None: ...

    def set_dictation(self, active: bool, level: float = 0.0,
                      phase: str = "recording") -> None: ...

    def show_clipboard(self, card: dict) -> None: ...

    def post(self, fn: Callable[[], None]) -> None: ...

    def get_root(self): ...

    def mainloop(self) -> None: ...

    def quit(self) -> None:
        """Ends `mainloop` (thread-safe; the loop returns within a fraction of a second)."""


class SuggestionPanel(Protocol):
    """A private window of suggestions, kept out of screen sharing and recording where the system can do it."""

    def open(self) -> tuple[bool, str]:
        """(opened, reason when not). With `hide_from_capture` it does not stay open on a system that cannot hide it."""

    def add_entry(self, entry) -> None: ...

    def update_entry(self, entry) -> None: ...

    def add_note(self, text: str) -> None: ...

    def close(self) -> None: ...


class Desktop(ABC):
    """The pieces of one platform."""

    name: str = ""
    # True when a Binding can keep its key away from the focused app.
    can_suppress: bool = False

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(usable, reason when not)."""

    @property
    @abstractmethod
    def clipboard(self) -> Clipboard: ...

    @property
    @abstractmethod
    def foreground(self) -> ForegroundApp: ...

    @abstractmethod
    def key_hook(self, name: str, bindings: list[Binding]) -> KeyHook: ...

    @abstractmethod
    def new_overlay(self) -> Overlay: ...

    def push_to_talk_keys(self, name: str, hotkeys: Mapping[str, Callable[[], None]]) -> KeyHook:
        """The global toggles of push-to-talk: `hotkeys` maps a neutral combination ("shift+f1") to what its press calls.

        A combination fires only with exactly its modifiers: Shift+F1 must not fire while Ctrl+Shift+F1 is held. Keys are not
        suppressed. The default is `key_hook` with exact Bindings; a platform overrides it when the toggles need their own
        listener. Raises `DesktopUnsupported` where there are no global hotkeys; the returned hook's `start()` is False when it
        could not listen."""
        bindings = []
        for combo, fn in hotkeys.items():
            mods, key = parse_hotkey(combo)
            if key is None:
                log.error("%s: %r has no key this system knows", name, combo)
                continue
            bindings.append(Binding(mods, key, fn, exact=True, suppress=False))
        return self.key_hook(name, bindings)

    def exclude_from_capture(self, handle: int, on: bool = True) -> bool:
        """Keep the window with this native handle out of screen sharing and recording. False where the system cannot."""
        return False

    def new_suggestions(self, hide_from_capture: bool = True, font_size: int = 22, on_speak=None, on_count=None, count: int = 3,
                        audio_reply: bool = False) -> SuggestionPanel:
        """The window of the copilot (Tk, in a thread of its own: it needs no main loop of the process)."""
        from .suggestions import TkSuggestions
        return TkSuggestions(self, hide_from_capture, font_size, on_speak, on_count, count, audio_reply)
