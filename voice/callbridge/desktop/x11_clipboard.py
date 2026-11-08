"""X11: clipboard (xclip or xsel), foreground window (xprop, xdotool), held modifiers (Xlib)."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import threading
from collections.abc import Callable

from .base import DesktopUnsupported
from .x11_keys import KeySender
from .x11_tools import X11Error, run_tool

log = logging.getLogger("callbridge.desktop.x11")

# KDE's Klipper and CopyQ skip content that carries this target; password
# managers such as KeePassXC set it.
_HINT_TARGET = "x-kde-passwordManagerHint"
_TEXT_TARGETS = ("UTF8_STRING", "text/plain;charset=utf-8", "STRING", "text/plain")
_PNG_TARGET = "image/png"

_TERM_CLASSES = {
    "gnome-terminal", "gnome-terminal-server", "konsole", "xfce4-terminal",
    "terminator", "tilix", "kitty", "alacritty", "wezterm", "wezterm-gui",
    "foot", "lxterminal", "mate-terminal", "qterminal", "terminology",
    "sakura", "guake", "yakuake", "tabby", "hyper", "st", "st-256color",
}
_TERM_COPY = ("ctrl", "shift", "c")
_TERM_PASTE = ("ctrl", "shift", "v")

_MODIFIER_KEYSYMS = ("Control_L", "Control_R", "Alt_L", "Alt_R",
                     "Shift_L", "Shift_R", "Super_L", "Super_R")


class X11Foreground:
    """Window class of the focused window, from the window manager's active-window property."""

    def __init__(self, run: Callable[[list[str]], bytes] = run_tool) -> None:
        self._run = run

    def _active_window(self) -> str:
        try:
            out = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"]).decode("utf-8", "replace")
            m = re.search(r"0x[0-9a-fA-F]+", out)
            if m and int(m.group(0), 16) != 0:
                return m.group(0)
        except X11Error:
            pass
        try:
            return self._run(["xdotool", "getactivewindow"]).decode().strip()
        except X11Error:
            return ""

    def identity(self) -> tuple[str, str]:
        """(WM_CLASS class, WM_CLASS instance), lowercase."""
        win = self._active_window()
        if not win:
            return "", ""
        try:
            out = self._run(["xprop", "-id", win, "WM_CLASS"]).decode("utf-8", "replace")
        except X11Error:
            return "", ""
        names = re.findall(r'"([^"]*)"', out)
        if len(names) < 2:
            return "", ""
        return names[1].lower(), names[0].lower()

    def is_terminal(self) -> bool:
        return any(n in _TERM_CLASSES for n in self.identity())


def modifiers_pressed(open_display: Callable, string_to_keysym: Callable) -> bool:
    """True when a Ctrl, Alt, Shift or Super key is down on the X server."""
    d = open_display()
    try:
        keymap = d.query_keymap()
        for name in _MODIFIER_KEYSYMS:
            code = d.keysym_to_keycode(string_to_keysym(name))
            if code and keymap[code // 8] & (1 << (code % 8)):
                return True
    finally:
        d.close()
    return False


def _xlib_modifiers_pressed() -> bool:
    from Xlib import XK, display
    return modifiers_pressed(display.Display, XK.string_to_keysym)


class X11Clipboard:
    """The CLIPBOARD selection through xclip (text and PNG) or xsel (text only).

    X11 has no change counter, so `sequence()` fingerprints the content. Content
    written with `exclude=True` is remembered here and read back as "excluded"
    (no standard marker can be set next to the text with these tools).
    """

    def __init__(self, foreground: X11Foreground, keys: KeySender,
                 which: Callable[[str], str | None] = shutil.which,
                 run: Callable[..., bytes] = run_tool,
                 modifiers: Callable[[], bool] = _xlib_modifiers_pressed) -> None:
        self._foreground = foreground
        self._keys = keys
        self._which = which
        self._run = run
        self._modifiers = modifiers
        self._lock = threading.Lock()
        self._fingerprint = ""
        self._seq = 0
        self._excluded = ""        # digest of the content last written as excluded

    # --- tool ----------------------------------------------------------------

    def tool(self) -> str:
        for name in ("xclip", "xsel"):
            if self._which(name):
                return name
        raise DesktopUnsupported("the clipboard needs xclip or xsel installed")

    def _targets(self) -> list[str]:
        if self.tool() == "xsel":
            return ["UTF8_STRING"] if self._run(["xsel", "--clipboard", "--output"]) else []
        try:
            out = self._run(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"])
        except X11Error:                       # empty clipboard
            return []
        return out.decode("utf-8", "replace").split()

    def _fetch(self, target: str) -> bytes:
        if self.tool() == "xsel":
            return self._run(["xsel", "--clipboard", "--output"])
        return self._run(["xclip", "-selection", "clipboard", "-o", "-t", target])

    def _content(self, targets: list[str]) -> tuple[str, bytes]:
        """("text"|"image"|"", raw bytes) of the first target we understand."""
        for t in _TEXT_TARGETS:
            if t in targets:
                return "text", self._fetch(t)
        if _PNG_TARGET in targets and self.tool() == "xclip":
            return "image", self._fetch(_PNG_TARGET)
        return "", b""

    @staticmethod
    def _digest(raw: bytes) -> str:
        return hashlib.sha1(raw).hexdigest()

    def _snapshot(self) -> tuple[list[str], str, bytes]:
        try:
            targets = self._targets()
            kind, raw = self._content(targets)
        except X11Error as e:
            log.debug("clipboard not readable: %s", e)
            return [], "", b""
        return targets, kind, raw

    # --- Clipboard -----------------------------------------------------------

    def sequence(self) -> int:
        targets, _, raw = self._snapshot()
        fp = self._digest(raw) + "|" + " ".join(targets)
        with self._lock:
            if fp != self._fingerprint:
                self._fingerprint = fp
                self._seq += 1
            return self._seq

    def read(self, honor_exclude: bool = True):
        targets, kind, raw = self._snapshot()
        if honor_exclude and (_HINT_TARGET in targets
                              or (raw and self._digest(raw) == self._excluded)):
            return ("excluded", None)
        if kind == "text":
            return ("text", raw.decode("utf-8", "replace"))
        if kind == "image":
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(raw))
                img.load()
                return ("image", img)
            except Exception as e:  # noqa: BLE001 - not a decodable image
                log.debug("clipboard image not decodable: %s", e)
        return None

    def _write(self, raw: bytes, target: str, exclude: bool) -> int:
        if self.tool() == "xsel":
            cmd = ["xsel", "--clipboard", "--input"]
        else:
            cmd = ["xclip", "-selection", "clipboard", "-i", "-t", target]
        try:
            self._run(cmd, raw)
        except X11Error as e:
            log.error("clipboard write failed: %s", e)
            return 0
        with self._lock:
            self._excluded = self._digest(raw) if exclude else ""
        return self.sequence()

    def write_text(self, text: str, exclude: bool = False) -> int:
        return self._write(text.encode("utf-8"), "UTF8_STRING", exclude)

    def write_image(self, img, exclude: bool = False) -> int:
        if self.tool() == "xsel":
            log.error("clipboard images need xclip; xsel handles text only")
            return 0
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return self._write(buf.getvalue(), _PNG_TARGET, exclude)

    def send_copy(self) -> None:
        self._keys.chord(_TERM_COPY if self._foreground.is_terminal() else ("ctrl", "c"))

    def send_paste(self) -> None:
        self._keys.chord(_TERM_PASTE if self._foreground.is_terminal() else ("ctrl", "v"))

    def modifiers_down(self) -> bool:
        try:
            return self._modifiers()
        except Exception as e:  # noqa: BLE001 - no display or Xlib: nothing held
            log.debug("modifier state not readable: %s", e)
            return False
