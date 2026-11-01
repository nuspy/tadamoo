"""Watching the course of a request, to act before it fails or is dropped.

Deterministic signals read from what the loop already reports: the same tool failing the same way, the project's checks failing again, the loop guards
striking, a checklist that does not move while the budget is going, a context about to be folded. A signal first becomes a note to the agent with the
facts and the lessons that match, so it can change course by itself. If the signal comes back after the note, it becomes a card of options for the
person (the same card as `ask_user`), built here from templates. Nothing here calls a model."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

COOLDOWN_STEPS = 2                  # steps between two actions of the watcher
REPEAT_ERRORS = 2                   # the same failure this many times: a note; this many again after it: a card
GATE_RETRIES = 2
FOLD_NEAR = 0.9                     # the share of the masking threshold at which the agent is told a fold is near
HALF, THREE_QUARTERS = 0.5, 0.75    # the share of the steps used with the checklist not moving
_DIGITS = re.compile(r"\d+")
_FAILED = re.compile(r"^(?:error|tool execution error|permission denied|unknown tool|loop detected)", re.IGNORECASE)


class Action:
    """What the watcher wants done: a note for the agent, or a card for the person."""

    def __init__(self, kind: str, stage: str, note: str = "", problem: str = "", fix: str = "") -> None:
        self.kind, self.stage, self.note, self.problem, self.fix = kind, stage, note, problem, fix

    def card(self) -> Dict[str, Any]:
        """The questions of the card, as `ask_user` takes them: what happened, and three ways to go on, the first recommended."""
        first = {"label": "Try the lesson" if self.fix else "Try another way", "recommended": True,
                 "description": (self.fix[:160] if self.fix else "I look for a different route to the goal.")}
        return {"intro": f"I keep running into the same problem: {self.problem[:200]}. How do you want to go on?",
                "questions": [{"text": "How should I go on?", "kind": "single", "options": [
                    first,
                    {"label": "Do what works, report the rest", "description": "I finish the part that can be done and say exactly what is left."},
                    {"label": "Stop and report", "description": "I stop here with a summary of what was done and what blocks."}]}]}


class Watcher:
    def __init__(self, lessons_for: Optional[Callable[[str], str]] = None) -> None:
        self.lessons_for = lessons_for
        self.restart()

    def restart(self) -> None:
        """A request of the person starts the watch again."""
        self.errors: Counter = Counter()
        self.gate_retries = 0
        self.noted: Dict[str, int] = {}                 # signal -> its count when the agent was last told
        self.carded: set = set()
        self.fold_told = False
        self.last_step = -10

    def observe(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "tool_end" and not event.get("gate") and event.get("status") == "failed":
            out = _DIGITS.sub("#", str(event.get("output") or "").strip().splitlines()[0][:80]) if str(event.get("output") or "").strip() else ""
            if out or event.get("decision") in ("executed", "denied", "invalid"):
                self.errors[(str(event.get("name")), out)] += 1
        elif kind == "gate" and event.get("decision") == "retry":
            self.gate_retries += 1

    def _stage(self, key: str, count: int, note_at: int, card_at: int) -> str:
        """"note" the first time a signal reaches `note_at`, "card" once it has grown by `card_at` after that, else ""."""
        if count < note_at:
            return ""
        if key not in self.noted:
            self.noted[key] = count
            return "note"
        if count >= self.noted[key] + card_at and key not in self.carded:
            self.carded.add(key)
            return "card"
        return ""

    def check(self, agent: Any, step: int) -> Optional[Action]:
        """Called before each model call of a request: the one thing worth doing now, or None."""
        if step - self.last_step < COOLDOWN_STEPS:
            return None
        found = self._look(agent)
        if found is not None:
            self.last_step = step
        return found

    def _look(self, agent: Any) -> Optional[Action]:
        worst = max(self.errors.items(), key=lambda kv: kv[1], default=None)
        if worst is not None:
            (tool, what), count = worst
            stage = self._stage(f"error:{tool}:{what}", count, REPEAT_ERRORS, REPEAT_ERRORS)
            if stage:
                problem = f"{tool} failed {count} times the same way ({what or 'no message'})"
                return self._action("repeat_error", stage, problem, f"{tool} {what}")
        stage = self._stage("gate", self.gate_retries, GATE_RETRIES, 1)
        if stage:
            return self._action("gate", stage, f"the project's checks failed {self.gate_retries} times", "checks failed")
        stage = self._stage("loop", getattr(agent, "_loop_strikes", 0), 1, 1)
        if stage:
            return self._action("loop", stage, "I am repeating the same steps without getting anywhere", "repeated calls loop")
        todo = getattr(getattr(agent, "tools", None), "todo", None)
        limit = getattr(agent, "max_turns", 0) or 0
        if todo is not None and limit > 0 and todo.open_items():
            used = len(agent.turns) / limit
            done = len(todo.items) - len(todo.open_items()) if hasattr(todo, "items") else 0
            if done == 0:
                stage = self._stage("stalled", int(used * 100), int(HALF * 100), int((THREE_QUARTERS - HALF) * 100))
                if stage:
                    return self._action("stalled", stage, f"{int(used * 100)}% of my steps are used and no item of my checklist is done", "no progress checklist")
        context = getattr(agent, "context", None)
        if context is not None and not self.fold_told:
            used = context.estimate_tokens(agent.messages) / max(1, context.budget_tokens)
            if used >= context.mask_at * FOLD_NEAR:
                self.fold_told = True
                return Action("fold", "note", "[harness] The context is nearly full: older steps will soon be folded into a record. Put what must survive (decisions, "
                                              "file names, what is done and what is left) in your plan or notes now, and keep working. The originals stay on disk.")
        return None

    def _action(self, kind: str, stage: str, problem: str, query: str) -> Action:
        lesson = ""
        if self.lessons_for is not None:
            try:
                lesson = self.lessons_for(query)
            except Exception:
                lesson = ""
        fix = ""
        for line in lesson.splitlines():
            if line.startswith("- ") and "fix" in line.lower():
                fix = line.split("fix", 1)[1].lstrip(": ").strip()[:200]
                break
        text = (f"[harness] Course check: {problem}. Change what you are doing: a different tool, other arguments, a smaller step, or ask the person if the "
                f"decision is theirs." + (f"\n{lesson}" if lesson else ""))
        return Action(kind, stage, text, problem, fix)
