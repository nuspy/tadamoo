"""X11: global hotkeys and key sending, through pynput (and xdotool when present)."""

from __future__ import annotations

import logging
import queue
import shutil
import threading
from collections.abc import Callable

from .base import KEY_NAMES, Binding
from .x11_tools import X11Error, run_tool

log = logging.getLogger("callbridge.desktop.x11")

# A held key can arrive as release+press pairs (auto-repeat); a release is only
# real when no press of the same key follows within this time.
REPEAT_GAP_S = 0.06

_MOD_PREFIXES = (("ctrl", "ctrl"), ("alt", "alt"), ("shift", "shift"), ("cmd", "win"))


def classify(key) -> tuple[str, str] | None:
    """pynput key -> ("mod", name) | ("key", name) | None when not one we bind."""
    name = getattr(key, "name", None)
    if name:
        if name == "alt_gr":
            return None
        for prefix, mod in _MOD_PREFIXES:
            if name == prefix or name.startswith(prefix + "_"):
                return "mod", mod
        return ("key", name) if name in KEY_NAMES else None
    char = getattr(key, "char", None)
    if not char or len(char) != 1:
        return None
    if ord(char) < 32:                       # Ctrl+letter arrives as a control code
        char = chr(ord(char) + 96)
    char = char.lower()
    return ("key", char) if char in KEY_NAMES else None


class X11KeyHook:
    """Watches Bindings with a pynput listener. Keys are not suppressed:
    the focused app receives them as well."""

    def __init__(self, name: str, bindings: list[Binding],
                 listener_factory: Callable | None = None,
                 timer_factory: Callable = threading.Timer) -> None:
        self.name = name
        self._by_key: dict[str, list[Binding]] = {}
        for b in bindings:
            if b.key in KEY_NAMES:
                self._by_key.setdefault(b.key, []).append(b)
            else:
                log.error("%s: key %r is not valid", name, b.key)
        self._listener_factory = listener_factory or _pynput_listener
        self._timer_factory = timer_factory
        self._listener = None
        self._held: set[str] = set()
        self._active: dict[str, Binding] = {}
        self._pending: dict[str, object] = {}     # key -> release timer
        self._lock = threading.Lock()
        self._events: "queue.Queue[Callable[[], None] | None]" = queue.Queue()

    @property
    def valid(self) -> bool:
        return bool(self._by_key)

    def start(self) -> bool:
        if not self.valid:
            log.error("%s: no valid key combination to watch", self.name)
            return False
        try:
            self._listener = self._listener_factory(self.on_press, self.on_release)
            self._listener.start()
        except Exception as e:  # noqa: BLE001 - no X connection, missing module
            log.error("%s: key listener not started: %s", self.name, e)
            self._listener = None
            return False
        self._events = queue.Queue()
        threading.Thread(target=self._worker, args=(self._events,),
                         name=f"{self.name}-worker", daemon=True).start()
        return True

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        with self._lock:
            for timer in self._pending.values():
                timer.cancel()
            self._pending.clear()
            self._active.clear()
            self._held.clear()
        self._events.put(None)

    @staticmethod
    def _worker(events: "queue.Queue[Callable[[], None] | None]") -> None:
        while True:
            fn = events.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - one callback must not stop the rest
                log.error("hotkey callback failed: %s", e)

    def _match(self, key: str) -> Binding | None:
        for b in self._by_key.get(key, ()):
            mods = set(b.mods)
            if (self._held == mods) if b.exact else (mods <= self._held):
                return b
        return None

    def on_press(self, key) -> None:
        kind = classify(key)
        if kind is None:
            return
        what, name = kind
        with self._lock:
            if what == "mod":
                self._held.add(name)
                return
            timer = self._pending.pop(name, None)
            if timer is not None:
                timer.cancel()                  # auto-repeat, not a release
            active = self._active.get(name)
            if active is not None:
                if active.repeat:
                    self._events.put(active.on_press)
                return
            b = self._match(name)
            if b is not None:
                self._active[name] = b
                self._events.put(b.on_press)

    def on_release(self, key) -> None:
        kind = classify(key)
        if kind is None:
            return
        what, name = kind
        with self._lock:
            if what == "mod":
                self._held.discard(name)
                return
            if name not in self._active or name in self._pending:
                return
            timer = self._timer_factory(REPEAT_GAP_S, self._release, args=(name,))
            self._pending[name] = timer
        timer.start()

    def _release(self, name: str) -> None:
        with self._lock:
            if self._pending.pop(name, None) is None:
                return
            b = self._active.pop(name, None)
        if b is not None and b.on_release is not None:
            self._events.put(b.on_release)


def _pynput_listener(on_press, on_release):
    from pynput import keyboard
    return keyboard.Listener(on_press=on_press, on_release=on_release)


class KeySender:
    """Sends a key chord to the focused window: xdotool when installed, else pynput."""

    def __init__(self, which: Callable[[str], str | None] = shutil.which,
                 run: Callable[[list[str]], bytes] = run_tool,
                 keyboard_module=None) -> None:
        self._which = which
        self._run = run
        self._keyboard = keyboard_module

    def chord(self, keys: tuple[str, ...]) -> None:
        """`keys` = modifiers then the final key, e.g. ("ctrl", "shift", "v")."""
        if self._which("xdotool"):
            # --clearmodifiers: keys the user still holds must not mix in
            self._run(["xdotool", "key", "--clearmodifiers", "+".join(keys)])
            return
        kb = self._keyboard or _pynput_keyboard()
        controller = kb.Controller()
        mods = [getattr(kb.Key, m) for m in keys[:-1]]
        with controller.pressed(*mods):
            controller.press(keys[-1])
            controller.release(keys[-1])


def _pynput_keyboard():
    try:
        from pynput import keyboard
    except ImportError as e:
        raise X11Error(f"cannot send keys: install xdotool or pynput ({e})") from e
    return keyboard
