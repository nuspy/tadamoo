"""E5 fixture: a repository for one long autonomous turn, generated from a seed, and the request with its standing rules.

`build(modules, seed)` returns the files and the facts the scorer needs (how many top-level functions each module has, which modules carry the
typo). The same arguments always give the same files. The request puts three standing rules at the start; the job needs a read and a ledger
line for every module (and an edit for most), so one answer takes well over a hundred tool calls.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Set

TYPO, FIXED = "recieve", "receive"
TRAILER = "# reviewed"
FINAL_LINE = "LEDGER-COMPLETE-7QX"
FROZEN_COUNT = 5

NOUNS = "order invoice parcel ticket report batch session record packet signal".split()
VERBS = "load parse merge check build sort split trim route count".split()


@dataclass
class Repo:
    files: Dict[str, str]
    functions: Dict[str, int] = field(default_factory=dict)      # module name -> top-level functions
    typo_modules: Set[str] = field(default_factory=set)
    frozen: List[str] = field(default_factory=list)


def _module(index: int, rng: random.Random, typo: bool) -> tuple[str, int]:
    n_funcs = rng.randint(3, 6)
    lines = [f'"""Module {index:03d}: helpers for {rng.choice(NOUNS)} handling."""', "", f"LIMIT = {rng.randint(10, 99)}", f"NAME = \"mod_{index:03d}\"", ""]
    for j in range(n_funcs):
        verb, noun = rng.choice(VERBS), rng.choice(NOUNS)
        lines += ["", f"def {verb}_{noun}_{j}(items, limit=LIMIT):", f'    """{verb.capitalize()} the {noun} items up to the limit."""']
        if typo and j == 0:
            lines.append(f"    # we {TYPO} the items as a list and keep their order")
        lines += ["    out = []", "    for item in items:", "        if len(out) >= limit:", "            break", f"        out.append(item)  # {noun} {j}", "    return out"]
    lines += ["", "", "class Helper:", '    """Keeps a little state."""', "", "    def __init__(self):", "        self.seen = 0", "",
              "    def note(self, item):", "        self.seen += 1", "        return item", ""]
    if typo:
        lines.insert(1, f"# This module will {TYPO} data from callers.")
    return "\n".join(lines) + "\n", n_funcs


def build(modules: int = 60, seed: int = 7) -> Repo:
    rng = random.Random(seed)
    repo = Repo(files={})
    for i in range(modules):
        typo = rng.random() < 0.67
        text, n = _module(i, rng, typo)
        name = f"mod_{i:03d}"
        repo.files[f"pkg/{name}.py"] = text
        repo.functions[name] = n
        if typo:
            repo.typo_modules.add(name)
    for k in range(FROZEN_COUNT):
        text, _ = _module(900 + k, rng, True)
        repo.files[f"frozen/legacy_{k}.py"] = text
        repo.frozen.append(f"frozen/legacy_{k}.py")
    repo.files["pkg/__init__.py"] = ""
    repo.files["LEDGER.md"] = "# Ledger\n\n"
    return repo


def request(modules: int) -> str:
    return (f"Standing rules for this whole job, until your final answer:\n"
            f"R1. Every pkg module you edit must end with the line `{TRAILER}`.\n"
            f"R2. Never modify anything under frozen/, even when it has the same misspelling.\n"
            f"R3. Your final answer must end with the exact line `{FINAL_LINE}`.\n\n"
            f"The job: pkg/ has {modules} modules (mod_000 and on). Go through every one of them, in order. For each module: read it, correct the misspelling "
            f"`{TYPO}` to `{FIXED}` wherever it appears in that module, and append one line to LEDGER.md in the form `mod_000: N functions`, where N is the number "
            f"of top-level functions (lines that begin with `def ` at the start of the line) in that module. Do not stop until all {modules} are done.")


VARIANTS = [{"id": "long60", "modules": 60, "seed": 7}, {"id": "long45", "modules": 45, "seed": 11}]
