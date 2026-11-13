"""The built-in skills: the catalogue in `skills/builtin.json`, and the import of the ones the person chose (what the setup offers).

A choice is a list of names; what each of them requires is added, and each is imported as the importer does it for any skill or plugin (a copy in the user data folder;
what is already imported with the same content is left alone, a changed source is re-imported). Nothing is imported that was not chosen."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .common import Problem
from .migrate import _try
from .service import Importer

CATALOGUE = "builtin.json"


def default_source() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


def load(source: Any = None) -> List[Dict[str, Any]]:
    """The entries of the catalogue, in its order: `{name, kind, path, group, default, requires, title, summary}`."""
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
    """The entries to import for `names`: each with what it requires first, once, in the order of the catalogue. An unknown name is a problem."""
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


def install(imp: Importer, names: Optional[Sequence[str]] = None, source: Any = None, dry_run: bool = False) -> Dict[str, Any]:
    """Imports the chosen built-in skills (`names`; None means the ones the catalogue ticks by default). The report is the one of `migrate`."""
    entries = load(source)
    chosen = choose(entries, defaults(entries) if names is None else names)
    report: Dict[str, Any] = {"source": str(Path(source) if source else default_source()), "dry_run": dry_run, "chosen": [e["name"] for e in chosen], "imported": [], "would_import": [],
                              "updated": [], "would_update": [], "skipped": [], "conflicts": []}
    for e in chosen:
        if not e["present"]:
            report["conflicts"].append({"item": f"{e['kind']}:{e['name']}", "why": f"the folder {e['path']} is missing from the program folder"})
            continue
        opts = {"adopt": True, "with_dependencies": True} if e["kind"] == "plugin" else {}
        _try(imp, report, e["kind"], Path(e["path"]), dry_run, True, **opts)
    return report
