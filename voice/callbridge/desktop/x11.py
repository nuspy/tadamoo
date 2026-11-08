"""Linux with an X11 session: hotkeys (pynput), clipboard and paste (xclip/xsel, xdotool),
foreground window (xprop), overlay (Tk)."""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Callable

from .base import Binding, Clipboard, Desktop, ForegroundApp, KeyHook, Overlay
from .tk_overlay import OverlayStyle, TkOverlay
from .x11_clipboard import X11Clipboard, X11Foreground
from .x11_keys import KeySender, X11KeyHook

# Plain symbols: Tk on X11 can crash on colour emoji fonts.
_STATE_ICONS = {"listening": "◉", "reasoning": "…", "speaking": "♪",
                "dictation": "●", "transcribing": "✎"}
_CLIP_ICONS = {"copy": "⧉", "prev": "◀", "next": "▶",
               "paste": "▼", "empty": "∅"}


class X11Desktop(Desktop):
    name = "x11"
    # pynput on X11 only suppresses the whole keyboard, never single keys.
    can_suppress = False

    def __init__(self, which: Callable[[str], str | None] = shutil.which,
                 has_module: Callable[[str], bool] | None = None) -> None:
        self._which = which
        self._has_module = has_module or _has_module
        foreground = X11Foreground()
        self._foreground = foreground
        self._clipboard = X11Clipboard(foreground, KeySender(which), which)

    def available(self) -> tuple[bool, str]:
        missing = []
        if not (self._which("xclip") or self._which("xsel")):
            missing.append("xclip or xsel (clipboard)")
        if not self._which("xprop"):
            missing.append("xprop (foreground window)")
        if not self._has_module("pynput"):
            missing.append("the pynput package (global hotkeys)")
        if missing:
            return False, "missing: " + ", ".join(missing)
        return True, ""

    @property
    def clipboard(self) -> Clipboard:
        return self._clipboard

    @property
    def foreground(self) -> ForegroundApp:
        return self._foreground

    def key_hook(self, name: str, bindings: list[Binding]) -> KeyHook:
        return X11KeyHook(name, bindings)

    def new_overlay(self) -> Overlay:
        return TkOverlay(OverlayStyle(
            emoji_font="DejaVu Sans", ui_font="DejaVu Sans",
            state_icons=_STATE_ICONS, clip_icons=_CLIP_ICONS))


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None
