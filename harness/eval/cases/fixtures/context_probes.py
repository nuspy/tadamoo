"""E9 fixture: questions put to an agent after a long turn, about what it saw in the part of the conversation that has been folded.

The facts come from the generated repository of E5 (`long_turn_repo`), so the answers are known without reading a transcript: the name of the first
function of a module, the value of its `LIMIT`, how many functions it has, and the two standing rules of the request. The modules asked about are
taken from the first third of the job, which a compaction folds first. Each question says to answer from memory and gives no tool to answer with.
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
    """Ten probes: three function names, three limits, two counts, the closing line of the final answer, the folder that stays as it is."""
    names = [f"mod_{i:03d}" for i in range(1, modules // 3, FIRST_THIRD_STEP)]
    probes: List[Dict[str, Any]] = []
    for n in names[:3]:
        probes.append({"id": f"name-{n}", "kind": "name", "expect": _facts(repo.files[f"pkg/{n}.py"])["first_function"],
                       "question": f"From memory of the work so far, without using any tool: what is the name of the first function in pkg/{n}.py? Answer with the name only."})
    for n in names[3:6]:
        probes.append({"id": f"limit-{n}", "kind": "number", "expect": _facts(repo.files[f"pkg/{n}.py"])["limit"],
                       "question": f"From memory of the work so far, without using any tool: what is the value of LIMIT in pkg/{n}.py? Answer with the number only."})
    for n in names[6:8]:
        probes.append({"id": f"count-{n}", "kind": "number", "expect": str(repo.functions[n]),
                       "question": f"From memory of the work so far, without using any tool: how many top-level functions did you count in pkg/{n}.py? Answer with the number only."})
    probes.append({"id": "rule-final", "kind": "text", "expect": lt.FINAL_LINE,
                   "question": "From memory of the request, without using any tool: what exact line must your final answer end with? Answer with that line only."})
    probes.append({"id": "rule-frozen", "kind": "text", "expect": "frozen",
                   "question": "From memory of the request, without using any tool: which folder must never be modified? Answer with its name only."})
    return [p for p in probes if p["expect"]]


def right(probe: Dict[str, Any], reply: str) -> bool:
    """Whether the reply gives the known answer: a number as a whole number, a name or a line as written, a folder in any case."""
    expect, text = str(probe["expect"]), reply or ""
    if probe["kind"] == "number":
        return re.search(rf"(?<![\w.]){re.escape(expect)}(?!\w|\.\d)", text) is not None
    if probe["kind"] == "text" and probe["id"] == "rule-frozen":
        return expect in text.lower()
    return expect in text
