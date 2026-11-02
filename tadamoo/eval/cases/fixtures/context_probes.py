"""E9 fixture: questions put to an agent after a long turn, about what it saw in the part of the conversation that has been folded.

Two groups: what the job needed later (a count it wrote in the ledger, whether a module had the misspelling, the standing rules) and what no later step needed (a
name, a value). A summary is meant to keep the first and may drop the second; the first is what the rule decides on.

The facts come from the generated repository of E5 (`long_turn_repo`), so the answers are known without reading a transcript. The modules asked about are
the first ones of the job, which a compaction folds first. Each question says to answer from memory and gives no tool to answer with.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from . import long_turn_repo as lt

FIRST_THIRD_STEP = 2                  # one module in this many of the first third


def _facts(text: str) -> Dict[str, str]:
    first = re.search(r"^def (\w+)\(", text, re.MULTILINE)
    limit = re.search(r"^LIMIT = (\d+)", text, re.MULTILINE)
    return {"first_function": first.group(1) if first else "", "limit": limit.group(1) if limit else ""}


def build(repo: lt.Repo, modules: int) -> List[Dict[str, Any]]:
    """Fourteen probes. What the job needed later ("task"): how many functions three modules have (the ledger lines), whether three modules had the misspelling
    (the decision to edit), the closing line of the final answer, the folder that stays as it is. What nothing later needed ("incidental"): the name of the first
    function of three modules and the value of `LIMIT` in three others. The modules are the first ones of the job."""
    names = [f"mod_{i:03d}" for i in range(0, min(modules // 3, 15))]
    probes: List[Dict[str, Any]] = []

    def add(group: str, pid: str, kind: str, expect: str, question: str) -> None:
        if expect:
            probes.append({"id": pid, "kind": kind, "group": group, "expect": expect,
                           "question": "From memory of the work so far, without using any tool: " + question})
    for n in names[1:12:4]:
        add("incidental", f"name-{n}", "name", _facts(repo.files[f"pkg/{n}.py"])["first_function"], f"what is the name of the first function in pkg/{n}.py? Answer with the name only.")
    for n in names[2:12:4]:
        add("incidental", f"limit-{n}", "number", _facts(repo.files[f"pkg/{n}.py"])["limit"], f"what is the value of LIMIT in pkg/{n}.py? Answer with the number only.")
    for n in names[3:12:4]:
        add("task", f"count-{n}", "number", str(repo.functions[n]), f"how many top-level functions did you count in pkg/{n}.py? Answer with the number only.")
    early = names[:12]
    for n in [m for m in early if m in repo.typo_modules][:2] + [m for m in early if m not in repo.typo_modules][:1]:
        add("task", f"typo-{n}", "yesno", "yes" if n in repo.typo_modules else "no", f"did pkg/{n}.py contain the misspelling you were asked to correct? Answer yes or no.")
    add("task", "rule-final", "text", lt.FINAL_LINE, "what exact line must your final answer end with? Answer with that line only.")
    add("task", "rule-frozen", "text", "frozen", "which folder must never be modified? Answer with its name only.")
    return probes


def right(probe: Dict[str, Any], reply: str) -> bool:
    """Whether the reply gives the known answer: a number as a whole number, yes or no as the first word, a name or a line as written, a folder in any case."""
    expect, text = str(probe["expect"]), reply or ""
    if probe["kind"] == "number":
        return re.search(rf"(?<![\w.]){re.escape(expect)}(?!\w|\.\d)", text) is not None
    if probe["kind"] == "yesno":
        first = re.match(r"\W*(yes|no)(?!\w)", text.lower())
        return bool(first) and first.group(1) == expect
    if probe["id"] == "rule-frozen":
        return expect in text.lower()
    return expect in text
