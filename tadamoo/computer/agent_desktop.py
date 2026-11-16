"""The agent-desktop backend (https://github.com/vercel-labs/agent-desktop, Apache-2.0): a program that reads the accessibility tree of a window and acts on its elements
by a ref, without moving the mouse. It is run as a subprocess, one command at a time; every command prints one JSON envelope (`ok`, `data` or `error` with `code` and
`disposition`). The tools and the session are the same as with the native backend (docs/computer-control.md): element numbers are mapped to the refs here.

What it does that the native backend cannot: act on an element without the pointer or the focus (click, set a value, scroll), on any window, also one behind another.
What it still does with the real pointer and keyboard (`--headed`): double and right click, typing and dragging on Windows, and anything by position.
Typed text goes to the program on its standard input, never in its arguments (a vault value would show in the list of processes).
Pictures, the list of screens and the pointer position come from this system's native backend when it can give them (a picture of an area, scaled), else from the program.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tadamoo.computer.backend import Backend, ComputerError, Monitor, Target, UiNode, Window
from tadamoo.sandbox import cancelled

PROTOCOL = "2"                       # the major version of the JSON envelope this code reads
CALL_SECONDS = 60.0
SNAPSHOT_MS = 8000                   # the observation budget of a snapshot (a Chromium window can take a while to settle)
TREE_DEPTH = 14
# Commands that need real pointer or keyboard input (--headed) whatever the system; and those that need it on Windows only.
ALWAYS_HEADED = {"mouse-click", "mouse-move", "mouse-wheel", "drag", "double-click", "triple-click", "hover"}
WINDOWS_HEADED = {"type", "right-click", "focus"}
# On other systems these are tried without the pointer first; a refusal for want of physical input (POLICY_DENIED) is tried once with it.
RETRY_HEADED = {"type", "right-click", "focus", "focus-window", "scroll"}
# Real input is never cut short by Stop: a pointer button or a key left pressed would be worse than the second it takes to finish.
UNINTERRUPTIBLE = ALWAYS_HEADED | {"press", "type", "right-click", "click", "set-value", "clear", "select", "toggle", "check", "uncheck", "expand", "collapse", "scroll", "clipboard-set"}
KEY_NAMES = {"enter": "return", "win": "cmd", "escape": "escape", "backspace": "backspace"}
ERROR_ADVICE = {
    "STALE_REF": "the element is gone or has changed: read the tree again with computer_ui_tree",
    "AMBIGUOUS_TARGET": "several identical elements match, so none is chosen: pick another element, or click by x and y of a picture",
    "ELEMENT_NOT_FOUND": "that element is not there any more: read the tree again with computer_ui_tree",
    "SNAPSHOT_NOT_FOUND": "the tree you used has expired: read it again with computer_ui_tree",
    "POLICY_DENIED": "that was refused, either as a dangerous shortcut (Alt+F4, Win+L, Alt+Tab and the like are never forced) or because it needs real mouse and keyboard input",
    "PERM_DENIED": "the system has not given accessibility access to this program",
    "APP_NOT_FOUND": "that program is not running",
    "WINDOW_NOT_FOUND": "that window does not exist (any more): list the windows again",
    "ACTION_NOT_SUPPORTED": "that element does not offer this action: try another element, or click by x and y of a picture",
    "ACTION_FAILED": "the action was rejected or its result did not match what was asked: look at the element again",
    "APP_UNRESPONSIVE": "the program is not answering: wait, then read the tree again",
    "TIMEOUT": "it took too long: wait and read the tree again",
    "INVALID_ARGS": "the request was not valid for agent-desktop",
    "PLATFORM_NOT_SUPPORTED": "this system does not support that",
    "INTERNAL": "agent-desktop failed inside",
}


class AdError(ComputerError):
    """An error envelope of agent-desktop, with its code and whether repeating the command is safe (it sent nothing)."""

    def __init__(self, message: str, code: str = "", retry: str = "unknown") -> None:
        super().__init__(message)
        self.code, self.retry = code, retry


def explain(error: Dict[str, Any]) -> AdError:
    code = str(error.get("code") or "")
    retry = str((error.get("disposition") or {}).get("retry") or "unknown")
    detail = " ".join(str(error.get("message") or "").split())[:200]
    advice = ERROR_ADVICE.get(code, "agent-desktop refused")
    tail = {"safe": " Nothing was sent, so trying again is safe.", "unsafe": " It may already have happened: look at the screen before doing it again."}.get(retry, "")
    return AdError(f"{advice} ({code}: {detail}).{tail}", code, retry)


class AdCli:
    """Runs agent-desktop commands and returns the `data` of the envelope, or raises AdError. `command` is the program (a list: the fake one in the tests is a script)."""

    def __init__(self, command: List[str], home: Optional[Path] = None, timeout: float = CALL_SECONDS, popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.command, self.home, self.timeout, self.popen = list(command), home, timeout, popen

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.home is not None:
            self.home.parent.mkdir(parents=True, exist_ok=True)     # the program makes the folder itself: it refuses one it did not make (its permissions must be private)
            env["AGENT_DESKTOP_HOME"] = str(self.home)             # snapshots can hold private screen text: they stay in TaDaMoo's own folder
        return env

    def run(self, args: List[str], stdin: Optional[str] = None, headed: bool = False, timeout: Optional[float] = None, interruptible: bool = True) -> Dict[str, Any]:
        argv = [*self.command, *(["--headed"] if headed else []), *args]
        flags = 0x08000000 if sys.platform == "win32" else 0                # no console window
        try:
            proc = self.popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env(), creationflags=flags)
        except OSError as e:
            raise ComputerError(f"agent-desktop could not be started: {e}") from None
        payload = stdin.encode("utf-8") if stdin is not None else None
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            try:
                out, err = proc.communicate(input=payload, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if interruptible and cancelled():
                    proc.kill()
                    proc.communicate()
                    raise ComputerError("Stopped: the person pressed Stop. Do nothing more on the computer for this request.") from None
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    raise ComputerError(f"agent-desktop did not answer {args[0]} in {timeout or self.timeout:g} seconds") from None
        text = (out or b"").decode("utf-8-sig", "replace").strip()
        try:
            envelope = json.loads(text)
        except ValueError:
            raise ComputerError(f"agent-desktop did not answer with JSON ({(err or b'').decode('utf-8', 'replace').strip()[:150] or text[:150]!r})") from None
        if not isinstance(envelope, dict) or str(envelope.get("version", "")).split(".")[0] != PROTOCOL:
            raise ComputerError(f"agent-desktop speaks version {envelope.get('version') if isinstance(envelope, dict) else '?'} of its JSON; this code reads {PROTOCOL}.x: update TaDaMoo or install the matching agent-desktop")
        if not envelope.get("ok"):
            raise explain(envelope.get("error") or {})
        return envelope.get("data") or {}


def _window_number(window_id: str) -> Optional[int]:
    m = re.fullmatch(r"w-(\d+)", str(window_id))
    return int(m.group(1)) if m else None


class AgentDesktopBackend(Backend):
    name = "agent-desktop"
    gestures = True
    clipboard = True

    def __init__(self, cli: AdCli, native: Optional[Backend] = None, platform: str = sys.platform) -> None:
        self.cli, self.native, self.platform = cli, native, platform
        self.version = ""
        self._windows: Dict[int, Window] = {}
        self._screens: List[Tuple[Monitor, int]] = []

    # -- what is there -------------------------------------------------------------------------------------------------

    def unavailable(self) -> str:
        if self.version:
            return ""
        try:
            self.version = str(self.cli.run(["version"], interruptible=False).get("version") or "?")
        except ComputerError as e:
            return str(e)
        return ""

    def _native(self) -> Optional[Backend]:
        return self.native if self.native is not None and not self.native.unavailable() else None

    def can_see_cursor(self) -> bool:
        return self._native() is not None

    def cursor(self) -> Tuple[int, int]:
        native = self._native()
        if native is None:
            raise ComputerError("this system does not tell where the mouse is")
        return native.cursor()

    def monitors(self) -> List[Monitor]:
        native = self._native()
        if native is not None:
            return native.monitors()
        found = self.cli.run(["list-displays"])
        rows = found if isinstance(found, list) else found.get("displays") or []
        self._screens = []
        for i, d in enumerate(rows):
            b = d.get("bounds") or {}
            self._screens.append((Monitor(0, int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("width", 0)), int(b.get("height", 0)), bool(d.get("is_primary"))), i))
        self._screens.sort(key=lambda s: (s[0].left, s[0].top))
        self._screens = [(Monitor(n + 1, m.left, m.top, m.width, m.height, m.primary), i) for n, (m, i) in enumerate(self._screens)]
        return [m for m, _ in self._screens]

    def capture(self, left: int, top: int, width: int, height: int, out_width: int, out_height: int) -> bytes:
        native = self._native()
        if native is not None:
            return native.capture(left, top, width, height, out_width, out_height)
        self.monitors()
        for m, index in self._screens:
            if (m.left, m.top, m.width, m.height) == (left, top, width, height):
                with tempfile.TemporaryDirectory() as folder:
                    path = os.path.join(folder, "screen.png")
                    self.cli.run(["screenshot", "--screen", str(index), path])
                    with open(path, "rb") as f:
                        return f.read()
        raise ComputerError("without this system's own backend only a whole screen can be pictured, at its own size")

    def windows(self) -> List[Window]:
        rows = self.cli.run(["list-windows"])
        rows = rows if isinstance(rows, list) else rows.get("windows") or []
        out: List[Window] = []
        self._windows = {}
        for r in rows:
            number = _window_number(r.get("id", ""))
            if number is None:
                continue
            b = r.get("bounds") or {}
            w = Window(number, str(r.get("title") or ""), str(r.get("app_name") or ""), int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("width", 0)), int(b.get("height", 0)),
                       foreground=bool(r.get("is_focused")), minimized=bool(r.get("minimized")))
            self._windows[number] = w
            out.append(w)
        return out

    def _window(self, window_id: int) -> Window:
        if window_id not in self._windows:
            self.windows()
        if window_id not in self._windows:
            raise ComputerError(f"there is no window {window_id}: list the windows again with computer_list_windows")
        return self._windows[window_id]

    def focus(self, window_id: int) -> None:
        self._act("focus-window", ["--window-id", f"w-{window_id}"])

    # -- the tree -----------------------------------------------------------------------------------------------------

    def ui_tree(self, window_id: int, limit: int) -> List[UiNode]:
        w = self._window(window_id)
        if w.minimized:
            raise ComputerError(f"the window {window_id} is minimized: its elements cannot be read; bring it up with computer_focus_window")
        data = self.cli.run(["snapshot", "--app", w.app, "--window-id", f"w-{window_id}", "--compact", "--include-bounds", "--max-depth", str(TREE_DEPTH), "--timeout-ms", str(SNAPSHOT_MS)])
        return self._flatten(data, limit)

    def _flatten(self, data: Dict[str, Any], limit: int) -> List[UiNode]:
        nodes: List[UiNode] = []
        total = [0]

        def walk(n: Dict[str, Any], depth: int, root: bool = False) -> None:
            states = n.get("states") or []
            b = n.get("bounds") or {}
            shows = not root and (n.get("ref_id") or n.get("name") or n.get("value")) and "offscreen" not in states and b.get("width", 0) > 0 and b.get("height", 0) > 0
            if shows:
                total[0] += 1
                if len(nodes) < limit:
                    nodes.append(UiNode(len(nodes) + 1, str(n.get("role") or ""), str(n.get("name") or ""), int(b["x"]), int(b["y"]), int(b["width"]), int(b["height"]), depth,
                                        "disabled" not in states, str(n.get("value") or ""), str(n.get("ref_id") or "")))
            for c in n.get("children") or []:
                walk(c, depth + (1 if shows else 0))

        if data.get("tree"):
            walk(data["tree"], 0, True)                        # the root is the window itself: not an element to act on
        notes = []
        if data.get("complete") is False or data.get("truncated"):
            notes.append("The program was still building its tree, or it is large: the list is partial. Ask again in a moment.")
        if data.get("identity_unproven"):
            notes.append("The window could not be read whole in time, so the elements cannot be acted on by number yet: ask again.")
        if total[0] > len(nodes):
            notes.append(f"{total[0] - len(nodes)} more element(s) were not listed: ask for a larger limit.")
        if nodes and not any(n.ref for n in nodes):
            notes.append("No element offers an action here (a web page or a canvas may expose nothing): use a picture and click by x and y.")
        self.tree_note = " ".join(notes)
        return nodes

    # -- acting -------------------------------------------------------------------------------------------------------

    def _act(self, command: str, args: List[str], stdin: Optional[str] = None, interruptible: Optional[bool] = None) -> str:
        """One command that changes something; returns a few words about its delivery. Real input (`--headed`) only where it is needed."""
        headed = command in ALWAYS_HEADED or (self.platform == "win32" and command in WINDOWS_HEADED)
        flexible = interruptible if interruptible is not None else command not in UNINTERRUPTIBLE
        try:
            data = self.cli.run([command, *args], stdin=stdin, headed=headed, interruptible=flexible)
        except AdError as e:
            if headed or e.code != "POLICY_DENIED" or command not in RETRY_HEADED:
                raise
            data = self.cli.run([command, *args], stdin=stdin, headed=True, interruptible=flexible)
        delivery = str((data.get("disposition") or {}).get("delivery") or "")
        return "" if delivery in ("", "delivered_verified", "not_delivered") else "It was delivered but not confirmed: look at the screen."

    @staticmethod
    def _xy(point: Tuple[int, int]) -> str:
        return f"{point[0]},{point[1]}"

    def move_to(self, x: int, y: int) -> None:
        self._act("mouse-move", [f"--xy={x},{y}"])

    def click(self, target: Target, button: str, count: int) -> str:
        if isinstance(target, str):
            if button == "left" and count == 1:
                return self._act("click", [target])
            if button == "right" and count == 1:
                return self._act("right-click", [target])
            if button == "left" and count == 2:
                return self._act("double-click", [target])
            raise ComputerError("that click needs a position (x and y), not an element")
        return self._act("mouse-click", [f"--xy={self._xy(target)}", "--button", button, "--count", str(count)])

    def drag(self, source: Target, destination: Target) -> str:
        args: List[str] = []
        for flag, end in (("from", source), ("to", destination)):
            args += [f"--{flag}", end] if isinstance(end, str) else [f"--{flag}-xy={self._xy(end)}"]
        return self._act("drag", args)

    def scroll(self, target: Optional[Target], direction: str, amount: int) -> str:
        if isinstance(target, str):
            return self._act("scroll", [target, "--direction", direction, "--amount", str(amount)])
        where = target if target is not None else self.cursor()
        dx, dy = {"up": (0, amount), "down": (0, -amount), "left": (-amount, 0), "right": (amount, 0)}[direction]       # lines: up and right are positive
        return self._act("mouse-wheel", [f"--x={where[0]}", f"--y={where[1]}", f"--dy={dy}", f"--dx={dx}"])

    def press(self, keys: List[str]) -> str:
        return self._act("press", ["+".join(KEY_NAMES.get(k, k) for k in keys)])

    def type_into(self, ref: str, text: str) -> str:
        return self._act("type", [ref, "--stdin"], stdin=text)

    def type_focus(self, text: str) -> str:
        """Types into the element that has the keyboard focus (found in the window in front); the person clicks a field first, or the model does with computer_click."""
        data = self.cli.run(["snapshot", "--surface", "focused", "--include-bounds", "--max-depth", str(TREE_DEPTH), "--timeout-ms", str(SNAPSHOT_MS)])
        found: List[str] = []

        def walk(n: Dict[str, Any]) -> None:
            if n.get("ref_id") and "focused" in (n.get("states") or []):
                found.append(str(n["ref_id"]))
            for c in n.get("children") or []:
                walk(c)

        if data.get("tree"):
            walk(data["tree"])
        if not found:
            raise ComputerError("no element has the keyboard focus: click the field first (computer_click), then type")
        return self.type_into(found[-1], text)

    def clipboard_get(self) -> str:
        data = self.cli.run(["clipboard-get", "--format", "text"])
        if data.get("found") is False:
            return ""
        return str(data.get("text") or "")

    def clipboard_set(self, text: str) -> str:
        return self._act("clipboard-set", ["--stdin"], stdin=text)
