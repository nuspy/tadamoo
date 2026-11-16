"""The `computer_*` tools: the agent looks at the screen and works the mouse and keyboard of this PC, where there is no API or command to do the job.

Permissions and the record are the ones the browser tools use (docs/computer-control.md): the effect classes below put each tool at its place in the
permission levels, `group:computer` and `computer_click`-style allow rules work as for any tool, a tool that reads the screen marks what it returns as
untrusted (a window can show anything), the Rule of Two looks at the calls that act (`outward`), a picture taken after a value from the vault was
typed asks the person first (at the Auto level it is taken), and what the tools do is recorded in the audit ledger under the actor "computer" (never
the text typed, only its length).
"""
from __future__ import annotations

import fnmatch
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.computer.backend import Backend, ComputerError, UiNode, Window, pick_backend
from tadamoo.computer.session import BUTTONS, MAX_NOTCHES, MAX_TEXT, MAX_WAIT, TREE_LIMIT, WINDOWS_SHOWN, Computer

HONEST = ("Slower and less sure than a script or an API: it works through what is on the screen, so use it only where there is no better way. "
          "The screen can change between a picture and the next action (a window opens, a page loads); if what you see is not what you expected, or the interface is "
          "unclear, say so instead of guessing, and look again before acting.")


def register_computer_tools(registry: Any, secrets: Any = None, backend: Optional[Backend] = None, monitor: Callable[[], int] = lambda: 0,
                            confirm: Optional[Callable[[str, Dict[str, Any]], bool]] = None, audit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                            screenshots: Optional[Callable[[bytes, str], Dict[str, Any]]] = None, auto: Optional[Callable[[], bool]] = None,
                            rules: Optional[Callable[[], List[str]]] = None, computer: Optional[Computer] = None, sleep: Callable[[float], None] = time.sleep,
                            backend_choice: str = "auto", tools_dir: Optional[Path] = None) -> Computer:
    """Adds the computer tools. `monitor` says which screen the agent works on (0: all), read at each call so a change applies at once. With `screenshots`
    (it keeps a picture as a file of the conversation and returns its card) the model can look at the screen."""
    from tadamoo.tools.registry import EffectClass

    mask = secrets.mask_secrets if secrets is not None else (lambda t: t)

    def approve(tool: str, details: Dict[str, Any]) -> Optional[bool]:
        """Whether the person allows what the tool would otherwise refuse: at the Auto level always; otherwise by an allow rule, or by the answer to the question."""
        try:
            if auto is not None and auto():
                return True
            site = str(details.get("site") or "")
            patterns = [str(p).lower() for p in (rules() if rules is not None else [])]
            if any(fnmatch.fnmatchcase(t.lower(), p) for t in [tool] + ([f"{tool}:{site}"] if site else []) for p in patterns):
                return True
            return bool(confirm(tool, details)) if confirm is not None else False
        except Exception:
            return False

    c = computer or Computer(backend or pick_backend(backend_choice, tools_dir), monitor, audit, approve, sleep)

    def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
        def run(**kw: Any) -> Any:
            try:
                return fn(**kw)
            except ComputerError as e:
                return {"error": str(e)}
            except Exception as e:                      # the system's own failure: the model hears it, the person finds it in the record
                c.record("error", {"error": f"{type(e).__name__}: {e}"[:300]})
                return {"error": f"the computer could not do that: {type(e).__name__}: {e}"[:300]}
        return run

    def take(window: Optional[int] = None, monitor_n: Optional[int] = None) -> str:
        data, what = c.screenshot(window, monitor_n)
        card = screenshots(data, f"screen_{time.strftime('%H%M%S')}.png")
        where = f" Path: {card['path']}." if card.get("path") else ""
        return (f"Picture of {what} kept as [file:{card['id']}:{card['name']}] ({len(data) // 1000} kB).{where} Look at it with analyze_image (path output:{card['id']}); "
                f"show it in your answer with ![caption](tadamoo-file:{card['id']}). x and y of computer_click, computer_move, computer_drag and computer_scroll are pixels of this picture.")

    def with_shot(fn: Callable[..., Any]) -> Callable[..., Any]:
        """The action, and when `screenshot` is true the picture of the screen it led to."""
        def run(screenshot: bool = False, **kw: Any) -> Any:
            out = fn(**kw)
            if screenshot and isinstance(out, str):
                sleep(0.4)
                try:
                    return out + "\n" + take()
                except ComputerError as e:
                    return out + f"\nNo picture: {e}"
            return out
        return run

    def shot_prop(props: Dict[str, Any]) -> Dict[str, Any]:
        if screenshots is None:
            return props
        return {**props, "screenshot": {"type": "boolean", "description": "also keep a picture of the screen after this (a file of the conversation)"}}

    at = {"x": {"type": "integer", "description": "pixels from the left of the last picture"}, "y": {"type": "integer", "description": "pixels from the top of the last picture"},
          "element": {"type": "integer", "description": "instead of x and y: the number of an element from the last computer_ui_tree"}}

    def reg(name: str, description: str, props: Dict[str, Any], effect: Any, handler: Callable[..., Any], required: Optional[List[str]] = None, outward: bool = False) -> None:
        schema: Dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        registry.register(name, description, schema, effect, guard(handler), untrusted=True, outward=outward)

    if screenshots is not None:
        reg("computer_screenshot", "Takes a picture of the screen the person gave you (or of one window) and keeps it as a file of the conversation; returns its path and how to look at it. "
            "Do this first and after each step that changes the screen: positions you give are pixels of the last picture. " + HONEST,
            {"window": {"type": "integer", "description": "the number of a window from computer_list_windows (default: the whole screen)"},
             "monitor": {"type": "integer", "description": "which screen, only when you were given all of them (default: all)"}}, EffectClass.REVERSIBLE,
            lambda window=None, monitor=None: take(window, monitor))

    def windows_text(found: List[Window]) -> str:
        if not found:
            return "No window is open on this screen."
        lines = [f"[{w.id}] {mask(w.title)[:80]!r} - {w.app} - {w.width}x{w.height} at {w.left},{w.top}{' - in front' if w.foreground else ''}{' - minimized' if w.minimized else ''}"
                 for w in found[:WINDOWS_SHOWN]]
        return "\n".join(lines) + (f"\n... and {len(found) - WINDOWS_SHOWN} more." if len(found) > WINDOWS_SHOWN else "")

    reg("computer_list_windows", "Lists the windows on the screen: a number, the title, the program, the size and place; the one in front is marked.", {}, EffectClass.PURE,
        lambda: windows_text(c.list_windows()))
    reg("computer_focus_window", "Brings a window to the front (and restores it if it is minimized).", {"window": {"type": "integer", "description": "the number from computer_list_windows"}},
        EffectClass.IRREVERSIBLE, lambda window: f"Window {window} ({c.focus_window(window).title[:60]!r}) is in front.", ["window"])

    def tree_text(target: Window, nodes: List[UiNode]) -> str:
        if not nodes:
            return f"{target.title[:60]!r} gave no elements on this screen: look at a picture instead."
        lines = []
        for n in nodes:
            extra = ("" if n.enabled else " [disabled]") + (f" value={mask(n.value)[:60]!r}" if n.value else "")
            lines.append(f"{'  ' * min(n.depth, 8)}[{n.id}] {n.role} {mask(n.name)[:80]!r} ({n.width}x{n.height} at {n.left},{n.top}){extra}")
        note = f"\nNote: {c.backend.tree_note}" if c.backend.tree_note else ""
        return (f"Elements of {target.title[:60]!r} ({target.app}); click one by its number with computer_click(element=N). The numbers end when something is clicked, typed or scrolled.\n"
                + "\n".join(lines) + note)

    reg("computer_ui_tree", "Reads the accessibility tree of a window (the one in front by default): numbered elements with a role, a name and a rectangle. It is how a model that "
        "cannot see pictures finds buttons and fields, and it is often more exact than a picture. Not every program offers one (games, some canvases): then use a picture. " + HONEST,
        {"window": {"type": "integer", "description": "the number from computer_list_windows (default: the window in front)"},
         "limit": {"type": "integer", "description": f"how many elements at most (default {TREE_LIMIT}, up to 400)"}}, EffectClass.PURE,
        lambda window=None, limit=TREE_LIMIT: tree_text(*c.ui_tree(window, limit)))
    reg("computer_move", "Moves the mouse pointer to a place.", shot_prop(at), EffectClass.IRREVERSIBLE, with_shot(lambda x=None, y=None, element=None: c.move(x, y, element)))
    reg("computer_click", "Clicks at a place: x and y of the last picture, or an element number from computer_ui_tree. Left, right or middle button; double for a double click. " + HONEST,
        shot_prop({**at, "button": {"type": "string", "enum": list(BUTTONS), "description": "default left"}, "double": {"type": "boolean", "description": "a double click"}}),
        EffectClass.IRREVERSIBLE, with_shot(lambda x=None, y=None, element=None, button="left", double=False: c.click(x, y, element, button, bool(double))), outward=True)
    reg("computer_drag", "Presses the left button at one place, moves to another and lets go: moving a window, a slider, a selection, a file.",
        shot_prop({"x": at["x"], "y": at["y"], "element": at["element"], "to_x": {"type": "integer"}, "to_y": {"type": "integer"},
                   "to_element": {"type": "integer", "description": "instead of to_x and to_y: an element number"}}), EffectClass.IRREVERSIBLE,
        with_shot(lambda x=None, y=None, element=None, to_x=None, to_y=None, to_element=None: c.drag(x, y, element, to_x, to_y, to_element)), outward=True)
    reg("computer_scroll", "Turns the mouse wheel, where the mouse is or at the place given.",
        shot_prop({**at, "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                   "amount": {"type": "integer", "description": f"notches of the wheel (default 3, at most {MAX_NOTCHES})"}}), EffectClass.IRREVERSIBLE,
        with_shot(lambda direction, amount=3, x=None, y=None, element=None: c.scroll(direction, amount, x, y, element)), ["direction"])

    def type_text(text: str, x: Optional[int] = None, y: Optional[int] = None, element: Optional[int] = None) -> Any:
        from_vault = secrets is not None and secrets.has_references(text)
        if from_vault:
            try:
                text = secrets.resolve_tokens(text, destinations=[c.foreground_app()])       # a value limited to named sites is not typed into a program
            except Exception as e:
                return {"error": str(e)}
        out = c.type_text(text, x, y, element, secret=from_vault)
        return out

    reg("computer_type_text", f"Types text where the keyboard focus is (or after a click on the place or element given), up to {MAX_TEXT} characters. Write {{{{vault:ENTRY.FIELD}}}} (or "
        "{{secret:NAME}}) for a value from the person's vault: it is typed without you seeing it, but only an entry that names no site can be typed into a program, and after "
        "that a picture of the screen asks the person first. Use computer_key for Enter, Tab and shortcuts. " + HONEST,
        shot_prop({"text": {"type": "string"}, **at}), EffectClass.IRREVERSIBLE, with_shot(type_text), ["text"], outward=True)
    reg("computer_key", "Presses a key or a combination: Enter, Tab, Escape, Ctrl+C, Alt+Tab, Ctrl+Shift+S, Win+D, F5... Modifiers (ctrl, shift, alt, win), then one key.",
        shot_prop({"combo": {"type": "string", "description": "for example Ctrl+S"}, "times": {"type": "integer", "description": "repeat (default 1, at most 20)"}}),
        EffectClass.IRREVERSIBLE, with_shot(lambda combo, times=1: c.key(combo, times)), ["combo"], outward=True)
    if c.backend.clipboard:
        reg("computer_clipboard_get", "Reads the text on the clipboard (what the person last copied: it can be private).", {}, EffectClass.PURE, lambda: mask(c.clipboard_get())[:4000] or "The clipboard has no text.")

        def clipboard_set(text: str) -> Any:
            if secrets is not None and secrets.has_references(text):
                return {"error": "a value from the vault is not put on the clipboard: it would stay there for any program to read. Type it with computer_type_text instead"}
            return c.clipboard_set(text)

        reg("computer_clipboard_set", "Puts text on the clipboard, replacing what is there (then paste it with computer_key Ctrl+V). Never for a value from the vault.",
            {"text": {"type": "string"}}, EffectClass.IRREVERSIBLE, clipboard_set, ["text"], outward=True)
    reg("computer_wait", f"Waits (up to {MAX_WAIT:g} seconds) for something on the screen to load or open, then look again.",
        {"seconds": {"type": "number", "description": "how long"}}, EffectClass.PURE, lambda seconds: c.wait(seconds), ["seconds"])
    return c


def tool_names() -> List[str]:
    return ["computer_screenshot", "computer_list_windows", "computer_focus_window", "computer_ui_tree", "computer_move", "computer_click", "computer_drag", "computer_scroll",
            "computer_type_text", "computer_key", "computer_wait", "computer_clipboard_get", "computer_clipboard_set"]
