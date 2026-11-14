"""The skills that ship with the program: the catalogue in `skills/builtin.json`. It is data only; what is installed is the person's choice (state.py).

An entry: `name` (the folder under skills/, a plugin's under skills/plugins/), `kind`, `group`, `default` (whether the setup starts with it ticked), `requires`
(built-in skills it cannot work without, installed with it), `download_gb` (about what its models weigh on a PC that has none of them; absent when not known),
a `title` and a `summary` in English and Italian."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tadamoo.importer.common import Problem

CATALOGUE = "builtin.json"


def default_source() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


def load(source: Any = None) -> List[Dict[str, Any]]:
    """The entries of the catalogue, in its order, each with its folder (`path`) and whether that folder is there (`present`)."""
    root = Path(source) if source else default_source()
    try:
        data = json.loads((root / CATALOGUE).read_text(encoding="utf-8"))
        entries = data["skills"]
    except (OSError, ValueError, KeyError):
        raise Problem("no_catalogue", f"There is no catalogue of built-in skills at {root / CATALOGUE}.") from None
    out = []
    for e in entries:
        folder = root / (e.get("path") or e["name"])
        out.append({**e, "path": str(folder), "requires": list(e.get("requires") or []), "present": folder.is_dir()})
    return out


def choose(entries: List[Dict[str, Any]], names: Sequence[str]) -> List[Dict[str, Any]]:
    """The entries for `names`: each with what it requires, once, in the order of the catalogue. An unknown name is a problem."""
    by_name = {e["name"]: e for e in entries}
    wanted: set = set()

    def want(name: str) -> None:
        if name not in by_name:
            raise Problem("unknown_skill", f"'{name[:40]}' is not a built-in skill: " + ", ".join(by_name))
        if name not in wanted:
            wanted.add(name)
            for need in by_name[name]["requires"]:
                want(need)

    for n in names:
        want(n)
    return [e for e in entries if e["name"] in wanted]


def defaults(entries: List[Dict[str, Any]]) -> List[str]:
    return [e["name"] for e in entries if e.get("default")]


def find(entries: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    return next((e for e in entries if e["name"] == name), None)
