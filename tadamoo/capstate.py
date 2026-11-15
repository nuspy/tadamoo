"""The settings that can change under the model's feet, told to it when they change: a short `[state]` note, never in the prompt prefix.

The fixed block in the prompt (awareness.py) cannot carry what changes during a conversation (the thinking effort, the permission level, plan
mode, the window). `read` collects those values from the objects that hold them; `Teller` remembers what the model was last told and gives the
note with only the fields that differ. After the older conversation is folded into a record the note may be gone, so everything is told again.
Nothing here calls a model."""
from __future__ import annotations

from typing import Any, Dict, Optional

from tadamoo import netaddr

FIELDS = ("window", "effort", "level", "plan_mode", "unattended", "steps", "max_tokens", "orchestration", "long_answer", "compaction", "reader", "address", "sampling", "knowledge")
PROMPT_TOLD = ("window",)         # the fixed block already states these when the session is built


def _word(value: Any, key: str = "") -> str:
    if key == "address":
        return netaddr.note_words(str(value))
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def long_answer(message: Any) -> str:
    """Whether the person's latest message carries the Long answer control line (serve/longform.py `parse_hint`), in words."""
    if not isinstance(message, str) or not message:
        return "off"
    from serve import longform
    hint = longform.parse_hint(message)
    if hint is None:
        return "off"
    words = hint["fields"].get("words", "")
    return f"on ({hint['mode']}{', about ' + words + ' words' if hint['mode'] == 'write' and words.isdigit() else ''}): write each text over about 1500 words with write_long"


def read(agent: Any, *, window: int, unattended: bool) -> Dict[str, Any]:
    """The values now, as plain words, from the objects that hold them."""
    perms = getattr(agent, "permissions", None)
    steps = getattr(agent, "max_turns", 0) or 0
    tools = getattr(agent, "tools", None)
    chosen = {k: v for k, v in (getattr(agent, "sampling", {}) or {}).items() if k in ("temperature", "top_p", "top_k", "min_p") and v is not None}
    sampling = ", ".join(f"{k} {v}" for k, v in chosen.items()) or "the model file's own"
    context = getattr(agent, "context", None)
    comp = getattr(context, "compaction", None)
    mask = getattr(context, "mask_at", None)
    if mask is None:
        folding = "unknown"
    elif comp is not None and getattr(comp, "enabled", False):
        folding = f"on (old results masked from {int(mask * 100)}% of the context, older steps folded from {int(max(comp.threshold, mask) * 100)}%)"
    else:
        folding = f"off (old results masked from {int(mask * 100)}% of the context, older steps never folded)"
    index = getattr(agent, "knowledge_index", None)
    state = {
        "window": int(window),
        "effort": (getattr(agent, "sampling", {}) or {}).get("reasoning_effort") or "default",
        "level": getattr(getattr(perms, "level", None), "value", "") or "unknown",
        "plan_mode": bool(getattr(perms, "plan_mode", False)),
        "unattended": bool(unattended),
        "steps": steps if steps > 0 else "unlimited",
        "max_tokens": getattr(agent, "max_tokens", None) or "the server's own",
        "orchestration": bool(tools is not None and tools.get("session_spawn") is not None),
        "long_answer": long_answer(getattr(agent, "incoming", "")),
        "compaction": folding,
        "reader": getattr(agent, "reader", "") or "this PC",
        "sampling": sampling,
    }
    if hasattr(agent, "reader_address"):                        # set once a message told where the person is: "" at this machine, else the address they reach this server at
        state["address"] = agent.reader_address
    if index is not None:                                       # the knowledge archives this conversation can use now, named (tadamoo/rag/tools.py)
        try:
            state["knowledge"] = index()
        except Exception as e:                                  # an archive that cannot be reached must not stop the conversation
            state["knowledge"] = f"not available now ({type(e).__name__})"
    return state


class Teller:
    """What the model was last told; `note(state, folds)` is the text to put before the next model call, or "" when nothing changed."""

    def __init__(self, told: Optional[Dict[str, Any]] = None) -> None:
        self.shown: Dict[str, Any] = dict(told or {})
        self.folds = 0

    def note(self, state: Dict[str, Any], folds: int = 0) -> str:
        if folds != self.folds:                 # the record that replaced the old conversation may have swallowed the last note
            self.folds, self.shown = folds, {}
        changed = [f"{key}={_word(state[key], key)}" for key in FIELDS if key in state and self.shown.get(key) != state[key]
                   and not (key == "address" and not state[key] and key not in self.shown)]        # a person at this machine is the usual case: nothing to say until they are elsewhere
        if not changed:
            return ""
        self.shown.update({key: state[key] for key in FIELDS if key in state})
        return "[state] " + "; ".join(changed)
