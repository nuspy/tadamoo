"""The tool with which the agent writes a personal skill of its own: a Python script that repeats work it has done.

The model is asked to put everything that can be code in code and to call `ai()` only where the language itself must be understood; the
tool reports every problem of the script at once and how many places are left to the model, so a second try is informed."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from .store import PersonalSkills, SkillError

GUIDE = ("A skill is a Python script: from tadamoo_skill import tool, ai, ask, say; def main(params): ... . Do everything that can be done by code with "
         "code and with tool(...) calls (read files, run programs, filter, count, format). Use ai(...) only where the language itself has to be understood "
         "(summarising, judging, writing); each ai() call costs model time on every run.")


def ai_calls(script: str) -> int:
    return len(re.findall(r"(?<![\w.])ai\(", script))


def register_authoring_tool(registry: Any, store: PersonalSkills) -> None:
    from tadamoo.tools.registry import EffectClass

    def create_skill(name: str, description: str, script: str, params: List[Dict[str, Any]] | None = None) -> Any:
        spec = {"name": name, "description": description, "params": params or [], "source": {"agent": True}}
        try:
            saved = store.save(spec, script)
        except SkillError as exc:
            from . import check as script_check
            problems = script_check.check(script)
            return {"saved": False, "problems": [f"line {p['line']}: {p['message']}" for p in problems] or [str(exc)], "guide": GUIDE}
        n = ai_calls(script)
        return {"saved": True, "skill": saved["slug"], "ai_calls": n,
                **({"note": f"{n} place(s) still call the model; make them code where you can"} if n else {})}

    registry.register(
        name="create_skill",
        description="Saves a personal skill: a Python script that repeats work you did, to be run again with other values. " + GUIDE,
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "description": "Up to 60 characters"},
            "description": {"type": "string", "description": "What it does, one sentence"},
            "script": {"type": "string", "description": "The Python source, with def main(params)"},
            "params": {"type": "array", "description": "Values that change from run to run: {name, label, type, default}", "items": {"type": "object"}}},
            "required": ["name", "description", "script"]},
        effect=EffectClass.REVERSIBLE, handler=create_skill)
