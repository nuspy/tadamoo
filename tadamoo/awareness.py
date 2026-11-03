"""What the agent knows about itself: a fixed block in the prompt, and a tool that reads the live numbers.

The block is built by code once per session from the configuration and does not change within it, so the prompt prefix stays the same. The tool
`self_status` answers with the live state of the conversation: how full the context is, what is left of the budgets, what the guards have seen.
Nothing here calls a model."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from tadamoo import persistence

# optional features and the plain reason each is missing, in the order they are listed to the model
FEATURES = (
    ("browser", "no browser tools (the person has not enabled them)"),
    ("vault", "no vault: it cannot sign in or fill in personal data"),
    ("phone", "no phone calls"),
    ("memory", "no project notes"),
    ("personal_memory", "no memories of the person"),
    ("past", "cannot search earlier conversations"),
    ("skills", "cannot write personal skills"),
    ("lsp", "no language-server tools"),
    ("mcp", "no tool servers connected"),
)


def self_model(*, model: str, window: int, mask_at: float, compaction_at: float, compaction: bool, max_turns: int, max_tokens: Optional[int],
               max_seconds: Optional[float], absent: List[str], lessons: str) -> str:
    """The fixed block "what you are": the limits and the habits of the harness around the model, in plain sentences."""
    steps = f"{max_turns} steps per answer" if max_turns and max_turns > 0 else "no limit on the steps of an answer"
    budgets = [steps]
    if max_tokens:
        budgets.append(f"{max_tokens} tokens in all")
    if max_seconds:
        budgets.append(f"{int(max_seconds)} seconds")
    lines = [
        "Written by the harness around you; the numbers are fixed for this session, `self_status` gives the live ones:",
        f"- You are {model or 'the loaded model'}, run by TaDaMoo on this PC.",
        f"- You have about {window} tokens of room for the conversation. Old tool results are replaced by a marker that names the file where the full text is saved (from "
        f"{int(mask_at * 100)}% of the window), and " + (f"the older conversation is folded into a record from {int(compaction_at * 100)}%; the originals stay on disk and can be read again."
                                                         if compaction else "the conversation is not folded.")
        + " Put the decisions and facts that must survive into your notes or the plan before that happens.",
        f"- Limits: {', '.join(budgets)}. When one runs out you are warned, then given one last report.",
        "- Your tools are the ones in the tool definitions." + (f" Not available here: {'; '.join(absent)}." if absent else ""),
        "- A task is yours until it is done: check your result against what was asked before you say it is finished, and say plainly what is not done.",
    ]
    if lessons:
        lines.append(f"- Lessons from earlier work are kept in {lessons}; relevant ones are shown to you at the start of a task as advice, not as orders.")
    return "\n".join(lines)


def absent_features(present: Dict[str, bool]) -> List[str]:
    return [why for key, why in FEATURES if not present.get(key)]


def status_of(agent: Any, window: int, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The live state of a conversation, read from the objects that already count it."""
    context = getattr(agent, "context", None)
    used = context.estimate_tokens(agent.messages) if context is not None else 0
    compaction = getattr(context, "compaction", None)
    comp = compaction.status() if compaction is not None else {}
    perms = getattr(agent, "permissions", None)
    gate = getattr(agent, "gate", None)
    out: Dict[str, Any] = {
        "context": {"tokens": used, "window": window, "share": round(used / window, 3) if window else 0.0,
                    "masking_from": getattr(context, "mask_at", None), "folds_from": comp.get("threshold"), "folds_so_far": comp.get("count", 0)},
        "steps": {"this_answer": len(agent.turns), "max": agent.max_turns if agent.max_turns and agent.max_turns > 0 else None},
        "tokens_used": agent.total_tokens, "tokens_max": agent.max_total_tokens, "seconds_max": agent.max_seconds,
        "loop_strikes": getattr(agent, "_loop_strikes", 0), "gate_rounds": getattr(gate, "rounds", None) if gate is not None else None,
        "refused_calls": getattr(perms, "denied", 0) if perms is not None else 0,
        "instructions_waiting": list(getattr(perms, "instructions", [])) if perms is not None else [],
        "mission": dict(getattr(agent, "mission", {}) or {}), "work_until_done": persistence.enabled(),
        "role": getattr(getattr(agent, "role", None), "id", "general"), "level": getattr(getattr(perms, "level", None), "value", ""),
    }
    out.update(extra or {})
    return out


def register_self_status(registry: Any, probe: Callable[[], Dict[str, Any]]) -> None:
    from tadamoo.tools.registry import EffectClass
    registry.register(
        name="self_status",
        description=("Reads the live state of this conversation: how full your context is and when it will be folded, steps, tokens and time used and left, what the loop "
                     "guards have seen, the mission and the lessons in force. Use it before a long step, when unsure how much room is left, or to check where you are."),
        parameters={"type": "object", "properties": {}}, effect=EffectClass.PURE, handler=lambda: probe())
