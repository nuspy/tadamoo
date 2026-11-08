"""Systems without a desktop implementation: every call says why it cannot work."""

from __future__ import annotations

import time
from collections.abc import Callable

from .base import (Binding, Clipboard, Desktop, DesktopUnsupported, ForegroundApp,
                   KeyHook, Overlay)


WAYLAND = "global shortcuts and paste need X11; Wayland does not allow them"
MACOS = "global shortcuts and paste are not implemented for macOS"


class _Refuse:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _no(self, *args, **kwargs):
        raise DesktopUnsupported(self.reason)


class UnsupportedClipboard(_Refuse):
    def sequence(self) -> int:
        self._no()

    def read(self, honor_exclude: bool = True):
        self._no()

    def write_text(self, text: str, exclude: bool = False) -> int:
        self._no()

    def write_image(self, img, exclude: bool = False) -> int:
        self._no()

    def send_copy(self) -> None:
        self._no()

    def send_paste(self) -> None:
        self._no()

    def modifiers_down(self) -> bool:
        self._no()


class UnsupportedForeground(_Refuse):
    def identity(self) -> tuple[str, str]:
        self._no()

    def is_terminal(self) -> bool:
        self._no()


class NullOverlay:
    """No card is drawn; `mainloop` only keeps the main thread alive."""

    def set_state_fn(self, fn: Callable[[], str]) -> None:
        pass

    def set_dictation(self, active: bool, level: float = 0.0,
                      phase: str = "recording") -> None:
        pass

    def show_clipboard(self, card: dict) -> None:
        pass

    def post(self, fn: Callable[[], None]) -> None:
        pass

    def get_root(self):
        return None

    def mainloop(self) -> None:
        while True:
            time.sleep(1.0)


class UnsupportedDesktop(Desktop):
    name = "unsupported"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self._clipboard = UnsupportedClipboard(reason)
        self._foreground = UnsupportedForeground(reason)

    def available(self) -> tuple[bool, str]:
        return False, self.reason

    @property
    def clipboard(self) -> Clipboard:
        return self._clipboard

    @property
    def foreground(self) -> ForegroundApp:
        return self._foreground

    def key_hook(self, name: str, bindings: list[Binding]) -> KeyHook:
        raise DesktopUnsupported(self.reason)

    def new_overlay(self) -> Overlay:
        # The overlay is cosmetic: refusing it would stop the service.
        return NullOverlay()
