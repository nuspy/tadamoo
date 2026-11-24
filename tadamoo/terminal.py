"""The person's terminal, as the command line of API-CLI describes it with a message: the shell, its version, the system and the working folder.

Nothing of it is held until a message brings it. From then on the conversation carries `terminal_run`, which writes one command line on the person's own
prompt: the person reads it, changes it if they like and presses Enter. What the command prints comes back to the agent only if the person writes again
and the terminal's text since the last message is sent with it.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, Optional

TOOL = "terminal_run"
FIELDS = {"shell": 24, "version": 24, "os": 40, "cwd": 500, "venv": 200}
COMMAND_CHARS = 2000


def _text(value: Any, limit: int) -> str:
    """One short line of plain text: no control characters."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if ord(c) >= 32 else " " for c in value).split())[:limit]


def clean(raw: Any) -> Optional[Dict[str, str]]:
    """The facts as they are kept; None when the shell or the folder is missing. Unknown keys are dropped."""
    if not isinstance(raw, dict):
        return None
    held = {key: _text(raw.get(key), limit) for key, limit in FIELDS.items()}
    held = {key: value for key, value in held.items() if value}
    return held if held.get("shell") and held.get("cwd") else None


class Terminal:
    """The last description of the person's terminal this conversation was given, and the way to write a command on its prompt."""

    def __init__(self) -> None:
        self.held: Optional[Dict[str, str]] = None
        self._told: Optional[Dict[str, str]] = None
        self.on_command: Optional[Callable[[Dict[str, str]], None]] = None      # who carries the command to the terminal (the conversation's process: an event)

    def set(self, raw: Any) -> bool:
        held = clean(raw)
        if held is None:
            return False
        self.held = held
        return True

    def note(self) -> Optional[str]:
        """What the agent is told about the terminal, once and again only when it changed (the folder after a `cd`, a virtual environment switched on)."""
        held = self.held
        if held is None or held == self._told:
            return None
        self._told = held
        shell = " ".join(x for x in (held["shell"], held.get("version")) if x) + (f" on {held['os']}" if held.get("os") else "")
        return (f"[terminal] The person is typing in {shell}; its working folder is {held['cwd']}"
                + (f" (virtual environment: {held['venv']})" if held.get("venv") else "") + ". Your own tools work from the conversation's folder. To change "
                "something in their own shell (the folder, an environment variable, a virtual environment, an ssh connection) write ONE command line in that shell's "
                f"syntax with {TOOL}: it appears on their prompt and waits for their Enter.")

    def run(self, command: Any, why: Any = "") -> str:
        line = command.strip() if isinstance(command, str) else ""
        if not line:
            return "Nothing was written: the command is empty."
        if "\n" in line or "\r" in line:
            return "Nothing was written: it must be ONE line; join the steps with ';' (or '&&' where the shell has it)."
        if len(line) > COMMAND_CHARS:
            return f"Nothing was written: the line is longer than {COMMAND_CHARS} characters."
        if self.held is None or self.on_command is None:
            return "Nothing was written: no terminal is attached to this conversation."
        self.on_command({"id": uuid.uuid4().hex[:8], "command": line, "why": _text(why, 300)})
        return ("Written on the person's prompt, waiting for their Enter (they may change it or not press it). What it prints is not seen by you now: it comes "
                "back with their next message that continues this conversation.")


def register_terminal_tool(registry: Any, terminal: Terminal) -> None:
    """Offers `terminal_run`; the conversation calls this once its first description of a terminal arrives, so one without a terminal carries no such tool."""
    from tadamoo.tools.registry import EffectClass

    if registry.get(TOOL) is not None:
        return
    registry.register(
        name=TOOL,
        description=("Writes one command line on the person's own terminal prompt. Use it only for what has to happen in THEIR shell: changing its folder, setting an "
                     "environment variable, activating a virtual environment, opening an ssh connection, or running something they want to see or type themselves. "
                     "The line is not run: the person reads it and presses Enter (or changes it). Everything else you do with your own tools. One line, in the "
                     "syntax of the shell named in the [terminal] note."),
        parameters={"type": "object", "properties": {"command": {"type": "string", "description": "the command line, one line"},
                                                     "why": {"type": "string", "description": "a few words for the person on what it does"}},
                    "required": ["command"]},
        effect=EffectClass.PURE, handler=lambda command, why="": terminal.run(command, why))
