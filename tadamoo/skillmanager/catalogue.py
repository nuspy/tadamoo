"""The skills that ship with the program: the catalogue in `skills/builtin.json`. It is data only; what is installed is the person's choice (state.py).

An entry: `name` (the folder under skills/, a plugin's under skills/plugins/), `kind`, `group`, `default` (whether the setup starts with it ticked), `requires`
(built-in skills it cannot work without, installed with it), `load` (`hot` or `cold`: how the tools of the skill are listed in a conversation's prompt
until the person chooses, see tadamoo/cold_tools.py), `cold_tools` (the tools that are listed with one line when the skill is `cold`; none when it has no tools of its own), `download_gb` (about what its models weigh on a PC that has none of them; absent when not known),
a `title` and a `summary` in English and Italian."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tadamoo.importer.common import Problem

CATALOGUE = "builtin.json"
EXTRA_ENV = "TADAMOO_EXTRA_SKILLS"      # a catalogue-shaped JSON file of entries added on top of the real one, for tests only (a skill with no models to install)


def default_source() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


def load(source: Any = None) -> List[Dict[str, Any]]:
    """The entries of the catalogue, in its order, each with its folder (`path`) and whether that folder is there (`present`); plus, when
    TADAMOO_EXTRA_SKILLS names a catalogue-shaped file, its own entries (an explicit `path`, or the extra file's own folder)."""
    root = Path(source) if source else default_source()
    try:
        data = json.loads((root / CATALOGUE).read_text(encoding="utf-8"))
        entries = data["skills"]
    except (OSError, ValueError, KeyError):
        raise Problem("no_catalogue", f"There is no catalogue of built-in skills at {root / CATALOGUE}.") from None
    extra_file = os.environ.get(EXTRA_ENV)
    if extra_file:
        try:
            extra = Path(extra_file)
            edata = json.loads(extra.read_text(encoding="utf-8"))
            entries = entries + [{**e, "path": e.get("path") or str(extra.parent / e["name"])} for e in edata.get("skills", [])]
        except (OSError, ValueError, KeyError):
            pass                        # a broken override is never a reason to refuse the real catalogue
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
