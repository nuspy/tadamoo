"""Windows: the push-to-talk toggles, the listener the push-to-talk always used (pynput's GlobalHotKeys).

pynput matches a combination without checking that no other modifier is held: with Ctrl+Shift+F1 both <ctrl>+<shift>+<f1> and
<shift>+<f1> fire. A combination without Ctrl is therefore ignored while Ctrl is physically down; the others are not guarded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

log = logging.getLogger("callbridge.desktop.pttkeys")

_SPECIAL = frozenset(
    {"ctrl", "alt", "shift", "cmd", "space", "tab", "esc", "enter", "up", "down", "left", "right"}
    | {f"f{i}" for i in range(1, 25)})


def to_pynput(hotkey: str) -> str:
    """"shift+f1" -> "<shift>+<f1>" (the format of pynput)."""
    parts = []
    for raw in hotkey.lower().split("+"):
        key = raw.strip()
        parts.append(f"<{key}>" if key in _SPECIAL else key)
    return "+".join(parts)


def _ctrl_down() -> bool:
    import ctypes
    vk_control = 0x11
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk_control) & 0x8000)


def _global_hotkeys(hotkeys):
    from pynput import keyboard
    return keyboard.GlobalHotKeys(hotkeys)


class PynputHotKeys:
    """KeyHook over `pynput.keyboard.GlobalHotKeys`; the callbacks run on its listener thread."""

    def __init__(self, name: str, hotkeys: Mapping[str, Callable[[], None]],
                 factory: Callable | None = None,
                 ctrl_down: Callable[[], bool] | None = None) -> None:
        self.name = name
        self._hotkeys = dict(hotkeys)
        self._factory = factory or _global_hotkeys
        self._ctrl_down = ctrl_down or _ctrl_down
        self._listener = None

    def _guarded(self, combo: str, fn: Callable[[], None]) -> Callable[[], None]:
        if "ctrl" in (p.strip() for p in combo.lower().split("+")):
            return fn

        def call() -> None:
            if not self._ctrl_down():
                fn()
        return call

    def start(self) -> bool:
        if self._listener is not None:
            return True
        try:
            wrapped = {to_pynput(c): self._guarded(c, fn) for c, fn in self._hotkeys.items()}
            listener = self._factory(wrapped)
            listener.start()
        except Exception as e:  # noqa: BLE001 - an invalid hotkey or no backend
            log.error("%s: hotkeys not started: %s", self.name, e)
            return False
        self._listener = listener
        return True

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
