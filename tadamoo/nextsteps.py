"""The card after a task: next steps built from the facts of the work, and the one-time offer of the project's own checks.

Both are made by code, as one `ask_user` card (the card the model uses, with arrow keys) shown to a person who is there, after a final answer:
- Next steps, when the task had some size (`MIN_STEPS` steps, or items left open on the checklist) and the work points somewhere: checks that
  failed, items still open, files changed with no check run. The options come from those facts, the first fitting one is recommended, and
  "Nothing more" and a typed answer are always there. The person's switch is a small file like `tadamoo/persistence.py`.
- The checks a folder seems to have (`tadamoo/project_profile.py`), once per project, as a multiple choice. The server applies the answer.
Nothing here calls a model, and nothing the card offers runs until the person picks it."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo import questions as qs
from tadamoo.persistence import Prefs

FILE = "next-steps.json"
MIN_STEPS = 6                       # steps a task needs before the card of next steps is worth showing, unless items are open
MAX_ITEMS = 3                       # open items named in an option
WRITE_TOOLS = frozenset(("write_file", "edit_file", "write_output_file"))
CHECK_KINDS = ("test", "lint", "typecheck", "build")
RECOMMENDED_KINDS = ("test", "lint", "typecheck")
NEXT_ID, CHECKS_ID = "next", "checks"

_source: Optional[Callable[[], bool]] = None


def configure(source: "Callable[[], bool] | str | Path | None") -> None:
    """Sets where this process learns the person's choice: a function, a file, or None for off."""
    global _source
    _source = source if source is None or callable(source) else Prefs(source).read


def enabled() -> bool:
    return bool(_source()) if _source is not None else False


STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "intro": "The task is done. Here is what could come next.", "next": "What would you like to do next?",
        "fix": "Fix the failing checks", "fix_d": "The project's checks still fail; I fix the cause and run them again.",
        "items": "Finish the open items", "items_d": "Still open on the checklist: {items}.",
        "verify": "Check the work", "verify_d": "I run the project's tests or its build and report what they show.",
        "lesson": " From earlier work: {fix}", "nothing": "Nothing more", "nothing_d": "Close the task here.",
        "checks": "Run these checks before finishing?", "checks_d": "{kind} · from {source}",
        "gate_off": "The check gate is off in the settings: accepted checks run without asking when the agent uses them; switch the gate on to run them before every answer.",
        "closed": "Understood, nothing more for now.", "noted": "Noted.",
    },
    "it": {
        "intro": "Il compito è finito. Ecco cosa potrebbe venire dopo.", "next": "Cosa vuoi fare adesso?",
        "fix": "Correggere i controlli che falliscono", "fix_d": "I controlli del progetto falliscono ancora; correggo la causa e li rilancio.",
        "items": "Finire le voci aperte", "items_d": "Ancora aperte nella lista: {items}.",
        "verify": "Verificare il lavoro", "verify_d": "Lancio i test o la build del progetto e riporto cosa mostrano.",
        "lesson": " Dal lavoro passato: {fix}", "nothing": "Nient'altro", "nothing_d": "Chiudo qui il compito.",
        "checks": "Eseguire questi controlli prima di finire?", "checks_d": "{kind} · da {source}",
        "gate_off": "Il controllo prima della risposta è spento nelle impostazioni: i controlli accettati partono senza chiedere quando l'agente li usa; accendilo per eseguirli prima di ogni risposta.",
        "closed": "Capito, per ora nient'altro.", "noted": "Annotato.",
    },
}


@dataclass
class Facts:
    steps: int = 0
    request: str = ""
    open_items: List[str] = field(default_factory=list)
    check: str = ""                                       # "failed" (the gate gave up), "missing" (files changed and no check is set), or ""
    changed: List[str] = field(default_factory=list)      # files written or edited in this request
    lesson_fix: str = ""
    checks: List[Dict[str, str]] = field(default_factory=list)        # detected checks to offer once: {kind, command, source}
    gate_on: bool = False


def written_paths(turns: Sequence[Any]) -> List[str]:
    """The files the request's calls wrote or edited, in order, without repeats."""
    out: List[str] = []
    for t in turns:
        for c in getattr(t, "tool_calls", ()) or ():
            path = (c.arguments or {}).get("path") if isinstance(c.arguments, dict) else None
            if c.tool_name in WRITE_TOOLS and not c.is_error and isinstance(path, str) and path not in out:
                out.append(path)
    return out


def offerable(checks: Sequence[Any]) -> List[Dict[str, str]]:
    """The detected checks that have a command, the useful kinds first, at most as many as a multiple choice holds."""
    usable = [c for c in checks if getattr(c, "command", "")]
    ordered = sorted(usable, key=lambda c: CHECK_KINDS.index(c.kind) if c.kind in CHECK_KINDS else len(CHECK_KINDS))
    return [{"kind": c.kind, "command": c.command, "source": c.source} for c in ordered[:qs.MAX_OPTIONS]]


def facts_from(agent: Any, since: int = 0, checks: Sequence[Any] = (), lesson_for: Optional[Callable[[str], str]] = None) -> Facts:
    """What the request left behind, read from the agent loop: `since` is the index of its first turn."""
    turns = list(agent.turns)[since:]
    todo = getattr(agent.tools, "todo", None)
    gate = getattr(agent, "gate", None)
    changed = written_paths(turns)
    check = "failed" if agent.stop_reason == "verify_failed" else "missing" if changed and gate is None else ""
    found = Facts(steps=len(turns), request=" ".join(str(getattr(agent, "last_user_text", "")).split())[:100],
                  open_items=[str(i.get("content") or "")[:60] for i in todo.open_items()] if todo is not None else [],
                  check=check, changed=changed, checks=offerable(checks), gate_on=gate is not None)
    problem = "checks failed" if check == "failed" else (found.open_items[0] if found.open_items else "")
    if lesson_for is not None and problem:
        try:
            block = lesson_for(problem)
        except Exception:
            block = ""
        for line in block.splitlines():
            if line.startswith("- ") and "Fix:" in line:
                found.lesson_fix = line.split("Fix:", 1)[1].strip()[:100]
                break
    return found


def wanted(facts: Facts) -> bool:
    """Whether the card of next steps is worth showing: a task of some size or with open items, and a continuation the facts point to."""
    return (facts.steps >= MIN_STEPS or bool(facts.open_items)) and bool(facts.open_items or facts.check)


def _next_question(facts: Facts, t: Dict[str, str]) -> Optional[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    lesson = t["lesson"].format(fix=facts.lesson_fix) if facts.lesson_fix else ""
    if facts.check == "failed":
        options.append({"label": t["fix"], "description": t["fix_d"] + lesson})
    if facts.open_items:
        shown = "; ".join(facts.open_items[:MAX_ITEMS])
        options.append({"label": t["items"], "description": t["items_d"].format(items=shown) + (lesson if facts.check != "failed" else "")})
    if facts.check == "missing":
        options.append({"label": t["verify"], "description": t["verify_d"]})
    if not options:
        return None
    options[0]["recommended"] = True
    actions = [o["label"] for o in options]
    options.append({"label": t["nothing"], "description": t["nothing_d"]})
    return {"id": NEXT_ID, "text": t["next"], "kind": "single", "options": options, "_actions": actions}


def _checks_question(facts: Facts, t: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if not facts.checks:
        return None
    if len(facts.checks) == 1:                            # a multiple choice needs two options: one command is a yes or a no
        only = facts.checks[0]
        return {"id": CHECKS_ID, "text": f"{t['checks']} {only['command']}", "kind": "confirm", "recommended": True}
    options = [{"label": c["command"], "description": t["checks_d"].format(kind=c["kind"], source=c["source"]), "recommended": c["kind"] in RECOMMENDED_KINDS}
               for c in facts.checks]
    return {"id": CHECKS_ID, "text": t["checks"], "kind": "multi", "options": options, "allow_other": False, "required": False}


def build(facts: Facts, lang: str = "en", show_next: bool = True) -> Optional[Dict[str, Any]]:
    """The card for these facts as `Session._question` takes it, or None when there is nothing to put to the person. `meta` says how to read the
    answer: the labels that mean "go on", the label that means "nothing more", and, for the checks, which command each option stands for."""
    t = STRINGS.get(lang) or STRINGS["en"]
    asked = _next_question(facts, t) if show_next and wanted(facts) else None
    checks = _checks_question(facts, t)
    raw = [q for q in (checks, asked) if q is not None]
    if not raw:
        return None
    actions = asked.pop("_actions") if asked else []
    payload = qs.normalise({"intro": t["intro"], "questions": raw})
    meta: Dict[str, Any] = {"nothing": t["nothing"] if asked else "", "actions": actions, "closed": t["closed"], "noted": t["noted"]}
    if checks:
        wanted_by_label = {qs.clean(c["command"], qs.LABEL_CHARS): c["command"] for c in facts.checks}
        question = next(q for q in payload["questions"] if q["id"] == CHECKS_ID)
        commands = {"yes": facts.checks[0]["command"]} if question["kind"] == "confirm" else {
            o["id"]: wanted_by_label[o["label"]] for o in question["options"] if o["label"] in wanted_by_label}
        meta["offer"] = {"question": CHECKS_ID, "commands": commands}
        if not facts.gate_on:
            payload["notes"] = [*payload["notes"], t["gate_off"]]
    return {"mode": "question", "kind": "questions", "source": "next_steps", **payload, "question": qs.summary(payload), "options": [],
            **({"offer": meta["offer"]} if "offer" in meta else {}), "_meta": meta}


def settle(meta: Dict[str, Any], answer_text: str) -> Optional[str]:
    """What to do with the person's answer to this card: None when the agent should work on (an action was picked or a free answer typed),
    else the closing words, so that no model call is spent on "nothing more"."""
    if not answer_text.startswith(qs.ANSWER_HEADER):
        return None
    if meta.get("actions") and any(label in answer_text for label in meta["actions"]):
        return None
    if meta.get("nothing") and "-> Other:" in answer_text:
        return None
    return meta["closed"] if meta.get("nothing") else meta["noted"]
