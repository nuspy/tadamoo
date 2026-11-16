"""The computer, as one conversation uses it: which screen it works on, what the last picture showed, the numbers of the last accessibility tree,
and the two ways the person stops it at once.

Coordinates. A picture of the screen is scaled down to at most MAX_SIDE pixels on its long side, and the model gives positions in pixels of the
last picture; the session turns them into real pixels. So a click needs a picture (or the numbers of a ui tree) first. An element number stands
for the centre of the rectangle the tree gave it.

The screen. The person chooses which screen the agent works on (0 is all of them). Pictures, lists of windows, trees and every click, drag and scroll
stay inside it; a second screen can be the agent's while the person works on the first.

Stopping. The person's Stop of the turn ends a call at its next step and skips the calls queued behind it (the agent loop does the second part; `cancelled`
is how a running call sees it). Moving the mouse into a corner of the screens stops everything: the next calls, for HALT_SECONDS after the pointer is last
seen there, refuse with a message that tells the model to stop and wait for the person. The agent itself never goes into a corner: a target there is refused.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from tadamoo.computer.backend import Backend, ComputerError, Monitor, UiNode, Window, parse_combo
from tadamoo.sandbox import cancelled

MAX_SIDE = 1568           # the long side of a picture, in pixels
CORNER_PX = 4             # how close to a corner the pointer is "in" it
HALT_SECONDS = 20.0       # how long the calls refuse after the pointer was last seen in a corner
MAX_TEXT = 2000
MAX_WAIT = 30.0
MAX_NOTCHES = 30
WINDOWS_SHOWN = 40
TREE_LIMIT = 150
PAUSE = 0.05
BUTTONS = ("left", "right", "middle")

STOPPED = ("Stopped: the person moved the mouse into a corner of the screen. Do nothing more on the computer, tell the person you stopped and wait until they ask you to go on.")
PRESSED_STOP = "Stopped: the person pressed Stop. Do nothing more on the computer for this request."


@dataclass
class View:
    """What the last picture showed: the real rectangle and the size the model saw."""
    left: int
    top: int
    width: int
    height: int
    shown_width: int
    shown_height: int
    label: str

    def to_real(self, x: int, y: int) -> Tuple[int, int]:
        return self.left + round(x * self.width / self.shown_width), self.top + round(y * self.height / self.shown_height)


def png_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Width and height from a PNG's header, or None when it is not one."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def corners(monitors: List[Monitor]) -> List[Tuple[int, int]]:
    """The corners the pointer can really be pushed into: a corner of a screen with no other screen beside it in either direction."""
    out: List[Tuple[int, int]] = []
    for m in monitors:
        for cx, sx in ((m.left, -1), (m.right - 1, 1)):
            for cy, sy in ((m.top, -1), (m.bottom - 1, 1)):
                sideways, vertical = (cx + sx * 2, cy), (cx, cy + sy * 2)
                if not any(o.contains(*sideways) for o in monitors) and not any(o.contains(*vertical) for o in monitors):
                    out.append((cx, cy))
    return out


def _intersect(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> Optional[Tuple[int, int, int, int]]:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (left, top, right - left, bottom - top) if right > left and bottom > top else None


class Computer:
    def __init__(self, backend: Backend, monitor: Callable[[], int] = lambda: 0, audit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 approve: Optional[Callable[[str, Dict[str, Any]], Optional[bool]]] = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.backend, self.monitor, self.audit, self.approve, self.sleep, self.clock = backend, monitor, audit, approve, sleep, clock
        self.view: Optional[View] = None
        self.elements: Dict[int, UiNode] = {}
        self.typed_secret = False                  # a value from the vault was typed here: a picture could show it
        self.halted_until = 0.0

    def record(self, kind: str, details: Dict[str, Any]) -> None:
        if self.audit is not None:
            try:
                self.audit(kind, details)
            except Exception:
                pass

    # -- the screen and the stop ----------------------------------------------------------------------------------

    def ready(self) -> None:
        why = self.backend.unavailable()
        if why:
            raise ComputerError(f"computer control is not available here: {why}")

    def area(self) -> Tuple[List[Monitor], str]:
        """The screens the agent works on, and a word for them."""
        every = self.backend.monitors()
        if not every:
            raise ComputerError("no screen was found")
        wanted = self.monitor()
        if wanted <= 0:
            return every, "all screens"
        chosen = [m for m in every if m.index == wanted]
        if not chosen:
            raise ComputerError(f"screen {wanted} is not there: this computer has {len(every)} (1 to {len(every)}); the person chooses the screen in Settings")
        return chosen, f"screen {wanted}"

    def checkpoint(self) -> None:
        """Before each step: the person's Stop, and the pointer in a corner."""
        self.ready()
        if cancelled():
            raise ComputerError(PRESSED_STOP)
        now = self.clock()
        if self.backend.can_see_cursor():                      # a system that cannot tell where the pointer is has no corner stop
            x, y = self.backend.cursor()
            if any(abs(x - cx) < CORNER_PX and abs(y - cy) < CORNER_PX for cx, cy in corners(self.backend.monitors())):
                if now >= self.halted_until:
                    self.record("corner_stop", {"x": x, "y": y})
                self.halted_until = now + HALT_SECONDS
        if now < self.halted_until:
            raise ComputerError(STOPPED)

    def _target(self, x: int, y: int, mons: List[Monitor]) -> None:
        if not any(m.contains(x, y) for m in mons):
            raise ComputerError("that point is not on the screen the agent works on")
        if any(abs(x - cx) < CORNER_PX * 2 and abs(y - cy) < CORNER_PX * 2 for cx, cy in corners(self.backend.monitors())):
            raise ComputerError("the corners of the screen are how the person stops the agent: do not aim there")

    def point(self, x: Optional[int] = None, y: Optional[int] = None, element: Optional[int] = None) -> Tuple[int, int, str, str]:
        """A real position from picture pixels or an element number, a few words for it, and the element's ref when the backend gave one (else '');
        refused when it is off the agent's screen or in a corner."""
        mons, _ = self.area()
        ref = ""
        if element is not None:
            node = self.elements.get(int(element))
            if node is None:
                raise ComputerError(f"there is no element {element}: the numbers belong to the last computer_ui_tree and end when something is clicked, typed or scrolled; read the tree again")
            rx, ry = node.left + node.width // 2, node.top + node.height // 2
            label = f"element {element} ({node.role} '{node.name[:40]}')"
            ref = node.ref
        else:
            if x is None or y is None:
                raise ComputerError("say where: x and y (pixels of the last picture) or an element number")
            if self.view is None:
                raise ComputerError("take a picture first with computer_screenshot: x and y are pixels of the last picture")
            if not (0 <= x < self.view.shown_width and 0 <= y < self.view.shown_height):
                raise ComputerError(f"({x}, {y}) is outside the last picture, which is {self.view.shown_width}x{self.view.shown_height}")
            rx, ry = self.view.to_real(x, y)
            label = f"({x}, {y})"
        self._target(rx, ry, mons)
        return rx, ry, f"{label} at screen position {rx},{ry}", ref

    # -- looking ---------------------------------------------------------------------------------------------------

    def screenshot(self, window: Optional[int] = None, monitor: Optional[int] = None) -> Tuple[bytes, str]:
        """A PNG of the agent's screen, one screen (when the person left all of them to the agent) or a window, scaled to MAX_SIDE, and what it shows."""
        self.checkpoint()
        mons, label = self.area()
        if monitor is not None:
            if self.monitor() > 0:
                raise ComputerError(f"the person limited the agent to screen {self.monitor()}: it cannot look at another one")
            pick = [m for m in mons if m.index == int(monitor)]
            if not pick:
                raise ComputerError(f"screen {monitor} is not there: this computer has {len(mons)}")
            mons, label = pick, f"screen {monitor}"
        bounds = (min(m.left for m in mons), min(m.top for m in mons), max(m.right for m in mons) - min(m.left for m in mons), max(m.bottom for m in mons) - min(m.top for m in mons))
        rect = bounds
        if window is not None:
            w = self.window(window)
            if w.minimized:
                raise ComputerError(f"the window {window} is minimized: bring it up with computer_focus_window")
            clipped = _intersect((w.left, w.top, w.width, w.height), bounds)
            if clipped is None:
                raise ComputerError(f"the window {window} is not on the screen the agent works on")
            rect, label = clipped, f"window '{w.title[:60]}'"
        if self.typed_secret and self.approve is not None and not self.approve("computer_screenshot", {"site": label, "reason": "a value from the vault was typed on this computer and the picture could show it"}):
            raise ComputerError("no picture was taken: a value from the vault was typed on this computer, the picture could show it, and the person did not allow it. Use computer_ui_tree")
        if self.typed_secret and self.approve is None:
            raise ComputerError("no picture was taken: a value from the vault was typed on this computer and the picture could show it")
        scale = min(1.0, MAX_SIDE / max(rect[2], rect[3]))
        out_w, out_h = max(1, round(rect[2] * scale)), max(1, round(rect[3] * scale))
        data = self.backend.capture(rect[0], rect[1], rect[2], rect[3], out_w, out_h)
        out_w, out_h = png_size(data) or (out_w, out_h)            # the picture says how big it is, whoever made it
        self.view = View(rect[0], rect[1], rect[2], rect[3], out_w, out_h, label)
        self.record("screenshot", {"area": label, "size": f"{rect[2]}x{rect[3]}", "bytes": len(data)})
        return data, f"{label}, {rect[2]}x{rect[3]} pixels shown as {out_w}x{out_h}"

    def window(self, window_id: int) -> Window:
        for w in self.backend.windows():
            if w.id == int(window_id):
                return w
        raise ComputerError(f"there is no window {window_id}: list the windows again with computer_list_windows")

    def list_windows(self) -> List[Window]:
        """The windows on the agent's screen (and the minimized ones), the one in front first."""
        self.checkpoint()
        mons, _ = self.area()
        bounds = [(m.left, m.top, m.width, m.height) for m in mons]
        out = [w for w in self.backend.windows() if w.minimized or any(_intersect((w.left, w.top, w.width, w.height), b) for b in bounds)]
        out.sort(key=lambda w: (not w.foreground, w.minimized))
        return out

    def focus_window(self, window_id: int) -> Window:
        self.checkpoint()
        w = self.window(window_id)
        mons, _ = self.area()
        if not w.minimized and not any(_intersect((w.left, w.top, w.width, w.height), (m.left, m.top, m.width, m.height)) for m in mons):
            raise ComputerError(f"the window {window_id} is on a screen the agent does not work on")
        self.backend.focus(w.id)
        self.elements.clear()
        self.record("focus_window", {"app": w.app, "title": w.title[:80]})
        return w

    def ui_tree(self, window: Optional[int] = None, limit: int = TREE_LIMIT) -> Tuple[Window, List[UiNode]]:
        """The accessibility tree of a window (the one in front by default): numbered elements the model can click by number."""
        self.checkpoint()
        windows = self.backend.windows()
        if window is None:
            front = [w for w in windows if w.foreground]
            if not front:
                raise ComputerError("no window is in front: name one from computer_list_windows")
            target = front[0]
        else:
            target = self.window(window)
        mons, _ = self.area()
        nodes = self.backend.ui_tree(target.id, max(1, min(int(limit), 400)))
        nodes = [n for n in nodes if n.width > 0 and n.height > 0 and any(m.contains(n.left + n.width // 2, n.top + n.height // 2) for m in mons)]
        self.elements = {n.id: n for n in nodes}
        self.record("ui_tree", {"app": target.app, "title": target.title[:80], "elements": len(nodes)})
        return target, nodes

    # -- acting ----------------------------------------------------------------------------------------------------

    def foreground_app(self) -> str:
        try:
            return next((w.app for w in self.backend.windows() if w.foreground), "") or "desktop"
        except ComputerError:
            return "desktop"

    def move(self, x: Optional[int], y: Optional[int], element: Optional[int]) -> str:
        rx, ry, label, _ = self.point(x, y, element)
        self.checkpoint()
        self.backend.move_to(rx, ry)
        return f"Moved the mouse to {label}."

    def click(self, x: Optional[int], y: Optional[int], element: Optional[int], button: str = "left", double: bool = False) -> str:
        if button not in BUTTONS:
            raise ComputerError(f"button is one of {', '.join(BUTTONS)}")
        rx, ry, label, ref = self.point(x, y, element)
        self.checkpoint()
        if self.backend.gestures:                                    # the backend clicks the element itself (no pointer when it can), or the position
            note = self.backend.click(ref if ref and button != "middle" else (rx, ry), button, 2 if double else 1)
            self.elements.clear()
            self.record("click", {"button": button, "double": bool(double), "x": rx, "y": ry, "by": "element" if ref and button != "middle" else "position", "app": self.foreground_app()})
            return f"Clicked {button}{' twice' if double else ''} at {label}." + (" " + note if note else "")
        self.backend.move_to(rx, ry)
        self.sleep(PAUSE)
        for turn in range(2 if double else 1):
            self.backend.button(button, True)
            try:
                self.sleep(PAUSE / 2)
            finally:
                self.backend.button(button, False)
            if turn == 0 and double:
                self.sleep(PAUSE)
        self.elements.clear()
        self.record("click", {"button": button, "double": bool(double), "x": rx, "y": ry, "app": self.foreground_app()})
        return f"Clicked {button}{' twice' if double else ''} at {label}."

    def drag(self, x: Optional[int], y: Optional[int], element: Optional[int], to_x: Optional[int], to_y: Optional[int], to_element: Optional[int]) -> str:
        sx, sy, start, sref = self.point(x, y, element)
        ex, ey, end, eref = self.point(to_x, to_y, to_element)
        self.checkpoint()
        if self.backend.gestures:
            note = self.backend.drag(sref or (sx, sy), eref or (ex, ey))
            self.elements.clear()
            self.record("drag", {"from": [sx, sy], "to": [ex, ey], "app": self.foreground_app()})
            return f"Dragged from {start} to {end}." + (" " + note if note else "")
        steps = max(4, min(40, int(((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5 // 25)))
        self.backend.move_to(sx, sy)
        self.sleep(PAUSE)
        self.backend.button("left", True)
        try:
            for i in range(1, steps + 1):
                if i > 1:
                    self.checkpoint()
                self.backend.move_to(sx + (ex - sx) * i // steps, sy + (ey - sy) * i // steps)
                self.sleep(PAUSE / 2)
        finally:
            self.backend.button("left", False)
        self.elements.clear()
        self.record("drag", {"from": [sx, sy], "to": [ex, ey], "app": self.foreground_app()})
        return f"Dragged from {start} to {end}."

    def scroll(self, direction: str, amount: int, x: Optional[int], y: Optional[int], element: Optional[int]) -> str:
        if direction not in ("up", "down", "left", "right"):
            raise ComputerError("direction is up, down, left or right")
        amount = max(1, min(int(amount), MAX_NOTCHES))
        where = "where the mouse is"
        ref = ""
        if element is not None or x is not None or y is not None:
            rx, ry, where, ref = self.point(x, y, element)
            self.checkpoint()
            if self.backend.gestures:
                note = self.backend.scroll(ref or (rx, ry), direction, amount)
                self.elements.clear()
                self.record("scroll", {"direction": direction, "notches": amount})
                return f"Scrolled {direction} {amount} notch(es) {where}." + (" " + note if note else "")
            self.backend.move_to(rx, ry)
            self.sleep(PAUSE)
        elif self.backend.gestures:
            self.checkpoint()
            note = self.backend.scroll(None, direction, amount)
            self.elements.clear()
            self.record("scroll", {"direction": direction, "notches": amount})
            return f"Scrolled {direction} {amount} notch(es) {where}." + (" " + note if note else "")
        dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[direction]
        for i in range(amount):
            if i % 5 == 0:
                self.checkpoint()
            self.backend.wheel(dx, dy)
            self.sleep(PAUSE / 2)
        self.elements.clear()
        self.record("scroll", {"direction": direction, "notches": amount})
        return f"Scrolled {direction} {amount} notch(es) {where}."

    def type_text(self, text: str, x: Optional[int] = None, y: Optional[int] = None, element: Optional[int] = None, secret: bool = False) -> str:
        """Types text where the keyboard focus is, or after a click on the element or point given. `secret`: it came from the vault (never recorded)."""
        if len(text) > MAX_TEXT:
            raise ComputerError(f"type at most {MAX_TEXT} characters at a time")
        ref = ""
        if self.backend.gestures and element is not None:
            ref = self.point(None, None, element)[3]
        if (element is not None or x is not None or y is not None) and not ref:
            self.click(x, y, element)
            self.sleep(PAUSE)
        self.checkpoint()
        if secret:
            self.typed_secret = True
        if self.backend.gestures:                                    # the backend types the whole text, into the element or where the focus is
            note = self.backend.type_into(ref, text.replace("\r\n", "\n")) if ref else self.backend.type_focus(text.replace("\r\n", "\n"))
            self.elements.clear()
            self.record("type_text", {"characters": len(text), "from_vault": bool(secret), "app": self.foreground_app()})
            return f"Typed {len(text)} character(s)." + (" " + note if note else "")
        count = 0
        for ch in text.replace("\r\n", "\n"):
            if count % 25 == 24:
                self.checkpoint()
            if ch == "\n":
                self._press(["enter"])
            elif ch == "\t":
                self._press(["tab"])
            else:
                self.backend.char(ch)
            count += 1
            self.sleep(0.005)
        self.elements.clear()
        self.record("type_text", {"characters": count, "from_vault": bool(secret), "app": self.foreground_app()})
        return f"Typed {count} character(s)."

    def _press(self, keys: List[str]) -> None:
        down: List[str] = []
        try:
            for k in keys:
                self.backend.key(k, True)
                down.append(k)
            self.sleep(PAUSE / 2)
        finally:
            for k in reversed(down):
                try:
                    self.backend.key(k, False)
                except ComputerError:
                    pass

    def key(self, combo: str, times: int = 1) -> str:
        keys = parse_combo(combo)
        times = max(1, min(int(times), 20))
        notes: List[str] = []
        for i in range(times):
            self.checkpoint()
            if self.backend.gestures:
                notes.append(self.backend.press(keys))
            else:
                self._press(keys)
            self.sleep(PAUSE)
        self.elements.clear()
        self.record("key", {"combo": "+".join(keys), "times": times, "app": self.foreground_app()})
        return f"Pressed {'+'.join(keys)}{f' {times} times' if times > 1 else ''}." + (" " + next((n for n in notes if n), "") if any(notes) else "")

    def clipboard_get(self) -> str:
        self.checkpoint()
        text = self.backend.clipboard_get()
        self.record("clipboard_get", {"characters": len(text)})
        return text

    def clipboard_set(self, text: str) -> str:
        if len(text) > MAX_TEXT:
            raise ComputerError(f"put at most {MAX_TEXT} characters on the clipboard at a time")
        self.checkpoint()
        note = self.backend.clipboard_set(text)
        self.record("clipboard_set", {"characters": len(text)})
        return f"Put {len(text)} character(s) on the clipboard." + (" " + note if note else "")

    def wait(self, seconds: float) -> str:
        self.checkpoint()
        seconds = max(0.0, min(float(seconds), MAX_WAIT))
        slices = int(seconds / 0.25) + (1 if seconds % 0.25 > 1e-9 else 0)
        for i in range(slices):
            if cancelled():
                raise ComputerError(PRESSED_STOP)
            self.sleep(min(0.25, seconds - i * 0.25))
        return f"Waited {seconds:g} second(s)."
