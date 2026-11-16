"""Computer control on Linux with X11, through the programs xdotool (input, windows, pointer) and ImageMagick's `import` or scrot with Pillow (pictures), and
xrandr (screens). Each is found when used; what is missing is named in the error. There is no accessibility tree here (that needs AT-SPI): the model works
from pictures. Wayland sessions are not supported: Wayland does not let a program move the mouse or read other windows."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Callable, List, Optional, Tuple

from tadamoo.computer.backend import Backend, ComputerError, Monitor, UiNode, Window

KEYSYMS = {"enter": "Return", "tab": "Tab", "escape": "Escape", "backspace": "BackSpace", "delete": "Delete", "space": "space", "home": "Home", "end": "End", "pageup": "Prior",
           "pagedown": "Next", "up": "Up", "down": "Down", "left": "Left", "right": "Right", "insert": "Insert", "ctrl": "ctrl", "shift": "shift", "alt": "alt", "win": "super",
           "-": "minus", "=": "equal", "+": "plus", "[": "bracketleft", "]": "bracketright", ";": "semicolon", "'": "apostrophe", ",": "comma", ".": "period", "/": "slash",
           "`": "grave", "\\": "backslash"}
BUTTONS = {"left": "1", "middle": "2", "right": "3"}
WHEEL = {(0, -1): "4", (0, 1): "5", (-1, 0): "6", (1, 0): "7"}      # xdotool clicks buttons 4 to 7 for the wheel

Runner = Callable[[List[str]], "subprocess.CompletedProcess[bytes]"]


def _run(argv: List[str]) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(argv, capture_output=True, timeout=30)


class X11Backend(Backend):
    name = "x11"

    def __init__(self, run: Optional[Runner] = None) -> None:
        self.run = run or _run

    def _tool(self, argv: List[str]) -> str:
        try:
            done = self.run(argv)
        except FileNotFoundError:
            raise ComputerError(f"{argv[0]} is not installed") from None
        except subprocess.TimeoutExpired:
            raise ComputerError(f"{argv[0]} did not answer") from None
        if done.returncode != 0:
            raise ComputerError(f"{argv[0]} failed: {done.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return done.stdout.decode("utf-8", "replace")

    def unavailable(self) -> str:
        if not os.environ.get("DISPLAY"):
            return "there is no X11 display (DISPLAY is not set)"
        return "" if shutil.which("xdotool") else "xdotool is not installed (on Debian and Ubuntu: apt install xdotool)"

    def monitors(self) -> List[Monitor]:
        found: List[Tuple[int, int, int, int, bool]] = []
        try:
            for line in self._tool(["xrandr", "--query"]).splitlines():
                m = re.search(r"\bconnected( primary)?\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", line)
                if m:
                    found.append((int(m.group(4)), int(m.group(5)), int(m.group(2)), int(m.group(3)), bool(m.group(1))))
        except ComputerError:
            pass
        if not found:                                           # no xrandr: the one screen X reports
            width, height = (int(v) for v in self._tool(["xdotool", "getdisplaygeometry"]).split()[:2])
            found.append((0, 0, width, height, True))
        found.sort(key=lambda m: (m[0], m[1]))
        return [Monitor(i + 1, *m[:4], primary=m[4]) for i, m in enumerate(found)]

    def capture(self, left: int, top: int, width: int, height: int, out_width: int, out_height: int) -> bytes:
        if shutil.which("import"):
            argv = ["import", "-silent", "-window", "root", "-crop", f"{width}x{height}+{left}+{top}", "+repage", "-resize", f"{out_width}x{out_height}!", "png:-"]
            try:
                done = self.run(argv)
            except subprocess.TimeoutExpired:
                raise ComputerError("import did not answer") from None
            if done.returncode != 0 or not done.stdout:
                raise ComputerError("import (ImageMagick) could not take the picture: " + done.stderr.decode("utf-8", "replace").strip()[:200])
            return done.stdout
        if shutil.which("scrot"):
            try:
                from PIL import Image
            except ImportError:
                raise ComputerError("pictures need ImageMagick (apt install imagemagick) or scrot with Pillow (pip install pillow)") from None
            import io
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                path = os.path.join(folder, "shot.png")
                self._tool(["scrot", "-o", path])
                image = Image.open(path).crop((left, top, left + width, top + height)).resize((out_width, out_height))
                out = io.BytesIO()
                image.save(out, "PNG")
                return out.getvalue()
        raise ComputerError("no program to take pictures: install ImageMagick (apt install imagemagick) or scrot with Pillow")

    def windows(self) -> List[Window]:
        front = self._quiet(["xdotool", "getactivewindow"]).strip()
        out: List[Window] = []
        for raw in self._tool(["xdotool", "search", "--onlyvisible", "--name", ""]).split():
            wid = raw.strip()
            if not wid.isdigit():
                continue
            title = self._quiet(["xdotool", "getwindowname", wid]).strip()
            if not title:
                continue
            geometry = dict(re.findall(r"(\w+)=(-?\d+)", self._quiet(["xdotool", "getwindowgeometry", "--shell", wid])))
            if not {"X", "Y", "WIDTH", "HEIGHT"} <= set(geometry):
                continue
            pid = self._quiet(["xdotool", "getwindowpid", wid]).strip()
            app = ""
            if pid.isdigit():
                try:
                    with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                        app = f.read().strip()
                except OSError:
                    pass
            out.append(Window(int(wid), title, app, int(geometry["X"]), int(geometry["Y"]), int(geometry["WIDTH"]), int(geometry["HEIGHT"]), foreground=wid == front))
        return out

    def _quiet(self, argv: List[str]) -> str:
        """The output of a command that may fail for one window (it closed meanwhile): '' then."""
        try:
            return self._tool(argv)
        except ComputerError:
            return ""

    def focus(self, window_id: int) -> None:
        self._tool(["xdotool", "windowactivate", "--sync", str(window_id)])

    def ui_tree(self, window_id: int, limit: int) -> List[UiNode]:
        raise ComputerError("on Linux there is no accessibility tree here: use computer_screenshot and give positions")

    def cursor(self) -> Tuple[int, int]:
        values = dict(re.findall(r"([XY])=(-?\d+)", self._tool(["xdotool", "getmouselocation", "--shell"])))
        if "X" not in values or "Y" not in values:
            raise ComputerError("xdotool did not tell where the mouse is")
        return int(values["X"]), int(values["Y"])

    def move_to(self, x: int, y: int) -> None:
        self._tool(["xdotool", "mousemove", str(x), str(y)])

    def button(self, name: str, down: bool) -> None:
        self._tool(["xdotool", "mousedown" if down else "mouseup", BUTTONS[name]])

    def wheel(self, dx: int, dy: int) -> None:
        self._tool(["xdotool", "click", WHEEL[(dx, dy)]])

    def key(self, name: str, down: bool) -> None:
        sym = KEYSYMS.get(name) or (name.upper() if re.fullmatch(r"f\d+", name) else name)
        self._tool(["xdotool", "keydown" if down else "keyup", sym])

    def char(self, ch: str) -> None:
        self._tool(["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", ch])
