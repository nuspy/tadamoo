"""What the person chose about how a turn is checked before it ends: the project's own check commands and when they run (the verify gate),
whether a budget that runs out warns first and allows a last report, and the proposal about model errors that waiting cannot cure. Kept in
one small file; the conversations are started with the matching options (tadamoo/stack.py)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from tadamoo.gate import COMMAND_CHARS, DEFAULT_ROUNDS, MAX_COMMANDS, MAX_ROUNDS, MODES, clean_commands


CHAT_NOTES = ("review", "completion_check", "course", "todo_open")       # the notes of the harness the chat can show; each is off until the person turns it on


class GateError(ValueError):
    """What is wrong with a value, in words for the person; `field` names it."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class GatePrefs:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _stored(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def read(self) -> Dict[str, Any]:
        data = self._stored()
        rounds = data.get("verify_rounds")
        return {"verify_commands": clean_commands(data.get("verify_commands")),
                "verify_gate": data.get("verify_gate") if data.get("verify_gate") in MODES else "always",
                "verify_rounds": rounds if isinstance(rounds, int) and not isinstance(rounds, bool) and 1 <= rounds <= MAX_ROUNDS else DEFAULT_ROUNDS,
                "budget_grace": data.get("budget_grace") is not False,
                "error_classifier": data.get("error_classifier") is not False,
                "chat_notes": {k: (data.get("chat_notes") or {}).get(k) is True if isinstance(data.get("chat_notes"), dict) else False for k in CHAT_NOTES}}

    def write(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Saves the values given and keeps the others; a wrong value is refused whole, with the reason."""
        now = self.read()
        if "verify_commands" in changes:
            raw = changes["verify_commands"]
            lines = raw.splitlines() if isinstance(raw, str) else raw
            if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
                raise GateError("Give the commands as text, one to a line.", "verify_commands")
            wanted = [x.strip() for x in lines if x.strip()]
            if any(len(x) > COMMAND_CHARS for x in wanted):
                raise GateError(f"A command is at most {COMMAND_CHARS} characters.", "verify_commands")
            if len(set(wanted)) > MAX_COMMANDS:
                raise GateError(f"At most {MAX_COMMANDS} commands.", "verify_commands")
            now["verify_commands"] = clean_commands(wanted)
        if "verify_gate" in changes:
            if changes["verify_gate"] not in MODES:
                raise GateError("Choose off, unattended or always.", "verify_gate")
            now["verify_gate"] = changes["verify_gate"]
        if "verify_rounds" in changes:
            value = changes["verify_rounds"]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ROUNDS:
                raise GateError(f"This is a whole number from 1 to {MAX_ROUNDS}.", "verify_rounds")
            now["verify_rounds"] = value
        for key in ("budget_grace", "error_classifier"):
            if key in changes:
                if not isinstance(changes[key], bool):
                    raise GateError("This is on or off.", key)
                now[key] = changes[key]
        if "chat_notes" in changes:
            asked = changes["chat_notes"]
            if not isinstance(asked, dict) or any(k not in CHAT_NOTES or not isinstance(v, bool) for k, v in asked.items()):
                raise GateError("Choose on or off for each note.", "chat_notes")
            now["chat_notes"] = {**now["chat_notes"], **asked}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(now, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        return self.read()

    def view(self) -> Dict[str, Any]:
        """The values and the facts the page shows beside them."""
        return {**self.read(), "modes": list(MODES), "limits": {"verify_rounds": [1, MAX_ROUNDS], "commands": MAX_COMMANDS, "command_chars": COMMAND_CHARS},
                "chat_note_kinds": list(CHAT_NOTES),
                "defaults": {"verify_gate": "always", "verify_rounds": DEFAULT_ROUNDS, "budget_grace": True, "error_classifier": True, "chat_notes": {k: False for k in CHAT_NOTES}}}

    def flags(self) -> List[str]:
        """The options a conversation's process is started with."""
        v = self.read()
        out = ["--verify-gate", v["verify_gate"], "--verify-rounds", str(v["verify_rounds"])]
        for command in v["verify_commands"]:
            out += ["--verify-command", command]
        if not v["budget_grace"]:
            out.append("--no-budget-grace")
        if not v["error_classifier"]:
            out.append("--no-error-classifier")
        return out
