"""The settings that can change under the model's feet, told to it when they change: a short `[state]` note, never in the prompt prefix.

The fixed block in the prompt (awareness.py) cannot carry what changes during a conversation (the thinking effort, the permission level, plan
mode, the window). `read` collects those values from the objects that hold them; `Teller` remembers what the model was last told and gives the
note with only the fields that differ. After the older conversation is folded into a record the note may be gone, so everything is told again.
Nothing here calls a model."""
from __future__ import annotations

from typing import Any, Dict, Optional

FIELDS = ("window", "effort", "level", "plan_mode", "unattended", "steps", "orchestration")
PROMPT_TOLD = ("window",)         # the fixed block already states these when the session is built


def _word(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def read(agent: Any, *, window: int, unattended: bool) -> Dict[str, Any]:
    """The values now, as plain words, from the objects that hold them."""
    perms = getattr(agent, "permissions", None)
    steps = getattr(agent, "max_turns", 0) or 0
    tools = getattr(agent, "tools", None)
    return {
        "window": int(window),
        "effort": (getattr(agent, "sampling", {}) or {}).get("reasoning_effort") or "default",
        "level": getattr(getattr(perms, "level", None), "value", "") or "unknown",
        "plan_mode": bool(getattr(perms, "plan_mode", False)),
        "unattended": bool(unattended),
        "steps": steps if steps > 0 else "unlimited",
        "orchestration": bool(tools is not None and tools.get("session_send") is not None),
    }


class Teller:
    """What the model was last told; `note(state, folds)` is the text to put before the next model call, or "" when nothing changed."""

    def __init__(self, told: Optional[Dict[str, Any]] = None) -> None:
        self.shown: Dict[str, Any] = dict(told or {})
        self.folds = 0

    def note(self, state: Dict[str, Any], folds: int = 0) -> str:
        if folds != self.folds:                 # the record that replaced the old conversation may have swallowed the last note
            self.folds, self.shown = folds, {}
        changed = [f"{key}={_word(state[key])}" for key in FIELDS if key in state and self.shown.get(key) != state[key]]
        if not changed:
            return ""
        self.shown.update({key: state[key] for key in FIELDS if key in state})
        return "[state] " + "; ".join(changed)
