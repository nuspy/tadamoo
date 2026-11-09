"""Windows: the existing hook, clipboard and overlay code behind the desktop interfaces."""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable, Mapping

from . import win_clipboard
from .base import Binding, Clipboard, Desktop, ForegroundApp, KeyHook, Overlay
from .tk_overlay import OverlayStyle, TkOverlay
from .win_keyhook import LowLevelKeyHook
from .win_pynput_keys import PynputHotKeys

log = logging.getLogger("callbridge.overlay")

_STATE_ICONS = {"listening": "🧠", "reasoning": "💭", "speaking": "🗣️",
                "dictation": "🎤", "transcribing": "✍️"}
_CLIP_ICONS = {"copy": "📋", "prev": "⏪", "next": "⏩", "paste": "📥", "empty": "📭"}


def _no_activate(win) -> None:
    """The card must never take the focus: a Ctrl+V arriving while it is
    active would paste into the card instead of the app."""
    try:
        win.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        gwl_exstyle, ws_ex_noactivate, ws_ex_toolwindow = -20, 0x08000000, 0x80
        user32.SetWindowLongW(hwnd, gwl_exstyle, user32.GetWindowLongW(hwnd, gwl_exstyle)
                              | ws_ex_noactivate | ws_ex_toolwindow)
    except Exception as e:  # noqa: BLE001 - cosmetic, not functional
        log.debug("WS_EX_NOACTIVATE not applied: %s", e)


class WindowsForeground:
    def identity(self) -> tuple[str, str]:
        return win_clipboard._foreground()

    def is_terminal(self) -> bool:
        return win_clipboard.foreground_is_terminal()


class WindowsClipboard:
    def sequence(self) -> int:
        return win_clipboard.sequence()

    def read(self, honor_exclude: bool = True):
        return win_clipboard.read(honor_exclude)

    def write_text(self, text: str, exclude: bool = False) -> int:
        return win_clipboard.write_text(text, exclude)

    def write_image(self, img, exclude: bool = False) -> int:
        return win_clipboard.write_image(img, exclude)

    def send_copy(self) -> None:
        win_clipboard.send_copy()

    def send_paste(self) -> None:
        win_clipboard.send_paste()

    def modifiers_down(self) -> bool:
        return win_clipboard.modifiers_down()


class WindowsDesktop(Desktop):
    name = "windows"
    can_suppress = True

    def __init__(self) -> None:
        self._clipboard = WindowsClipboard()
        self._foreground = WindowsForeground()

    def available(self) -> tuple[bool, str]:
        return True, ""

    @property
    def clipboard(self) -> Clipboard:
        return self._clipboard

    @property
    def foreground(self) -> ForegroundApp:
        return self._foreground

    def key_hook(self, name: str, bindings: list[Binding]) -> KeyHook:
        return LowLevelKeyHook(name, bindings)

    def push_to_talk_keys(self, name: str, hotkeys: Mapping[str, Callable[[], None]]) -> KeyHook:
        return PynputHotKeys(name, hotkeys)

    def new_overlay(self) -> Overlay:
        return TkOverlay(OverlayStyle(
            emoji_font="Segoe UI Emoji", ui_font="Segoe UI",
            state_icons=_STATE_ICONS, clip_icons=_CLIP_ICONS,
            no_activate=_no_activate))

    def exclude_from_capture(self, handle: int, on: bool = True) -> bool:
        wda_exclude_from_capture, wda_none = 0x11, 0
        try:
            user32 = ctypes.windll.user32
            return bool(user32.SetWindowDisplayAffinity(user32.GetParent(handle) or handle, wda_exclude_from_capture if on else wda_none))
        except Exception as e:  # noqa: BLE001 (an old Windows without the call)
            log.warning("the window cannot be hidden from capture: %s", e)
            return False
