"""Plan mode's way out: the model presents its plan, the person approves it, and the run goes on in the mode they chose.

In plan mode nothing is changed or run. `present_plan` is the one tool that can end it, and only when the person says yes:
the question is put the way the permission questions are (the conversation's confirmation, the terminal, the editor), and a
run with nobody to ask stays a plan.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from harness.tools.registry import EffectClass

PLAN_TOOL = "present_plan"
MAX_PLAN_CHARS = 20_000
MODES_AFTER_PLAN = ("default", "auto")


class PlanGate:
    """How a plan is put to the person; whoever owns the conversation sets `ask`. With none, no plan can be approved.

    `ask(plan, modes, outside)` returns the mode the person chose to go on in (one of `modes`, the first being their usual
    choice), or "" when they did not approve. `outside` says the agent read content from outside before writing the plan."""
    ask: Optional[Callable[[str, List[str], bool], str]] = None


def register_plan_tool(registry: Any, permissions: Any, gate: PlanGate, switch: Callable[[str], None], after: str = "default",
                       audit: Any = None) -> None:
    """Adds `present_plan`. `switch(mode)` changes the run's permission mode (and takes the tool away again)."""
    first = after if after in MODES_AFTER_PLAN else "default"
    modes = [first] + [m for m in MODES_AFTER_PLAN if m != first]

    def present_plan(plan: str) -> str:
        if not permissions.plan_mode:
            return "Plan mode is not on: there is nothing to approve. Go on with the work."
        text = plan.strip()
        if not text:
            return "The plan is empty. Write what you will do, step by step, and present it again."
        if gate.ask is None:
            return "Nobody can approve a plan in this run, so it stays a plan: nothing was changed or run. Say what you would do and stop."
        choice = gate.ask(text[:MAX_PLAN_CHARS], modes, bool(permissions.untrusted_input))
        if choice not in modes:
            if audit is not None:
                audit.record("agent", "plan_rejected", {"chars": len(text)})
            return "The person did not approve this plan. Plan mode stays on: ask what they want changed, revise the plan and present it again."
        switch(choice)
        if audit is not None:
            audit.record("agent", "plan_approved", {"chars": len(text), "mode": choice})
        return f"The person approved the plan and the mode is now '{choice}'. Carry out the plan."

    registry.register(
        PLAN_TOOL,
        "Plan mode is on: nothing can be changed or run until the person approves a plan. Present your plan here (what you will do, "
        "step by step, which files and commands) and wait for the answer; if they approve, you can carry it out at once.",
        {"type": "object", "properties": {"plan": {"type": "string", "description": "The plan, in plain text or markdown"}}, "required": ["plan"]},
        EffectClass.PURE, present_plan)
