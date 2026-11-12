"""One-time move from a folder of skills that the harness read directly (a `skill_dirs` entry, a personal-skills override) to imported copies.

It imports what the folder holds as copies, then takes the folder out of the settings, so that from then on the harness uses the copies. The
folder itself is never changed."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .common import Problem, write_json
from .service import Importer

PERSONAL_OVERRIDE = "personal-skills-dir.json"


def _norm(p: Any) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _try(imp: Importer, report: Dict[str, Any], kind: str, path: Path, dry: bool, **opts: Any) -> None:
    label = f"{kind}:{path.name}"
    try:
        check = imp.verify(kind, path, **opts)
        snap = check["snapshot"]
        if check["already_imported"]:
            have = next((r for r in imp.list(kind) if r["name"] == snap["name"]), {})
            same = have.get("hash") == snap["hash"]
            report["skipped"].append({"item": label, "why": "already imported, same content" if same else "already imported, the source has changed: re-import it to see what"})
            return
        if not check["can_import"]:
            report["conflicts"].append({"item": label, "why": check["blockers"][0]["message"]})
            return
        if dry:
            report["would_import"].append(label)
            return
        out = imp.import_(kind, path, **opts)
        row = out["imported"]
        report["imported"].append({"item": label, **({"parts": row["parts"], "dependencies": row["dependencies"]} if kind == "plugin" else {}),
                                   **({"adopted": row["adopted"]} if row.get("adopted") else {})})
    except Problem as exc:
        report["conflicts"].append({"item": label, "why": str(exc)})


def _same_personal(imp: Importer, name: str, folder: Path) -> bool:
    """Whether the personal skill already imported under this name has the script of this folder."""
    try:
        have = imp.kinds["skill"].store().get(name)
        return have["script"] == (folder / "skill.py").read_text(encoding="utf-8")
    except Exception:
        return False


def migrate(imp: Importer, skills_root: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Imports what `skills_root` holds (its skills, its plugins with their parts and dependencies, its personal skills) and undoes the two
    settings that made the harness read it directly. A plan with an id that the planner already has is taken as the imported copy."""
    root = Path(skills_root)
    if not root.is_dir():
        raise Problem("no_source", f"There is no folder at {root}.")
    report: Dict[str, Any] = {"skills_root": str(root), "dry_run": dry_run, "imported": [], "would_import": [], "skipped": [], "conflicts": [], "settings": []}
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("plugins", "personal") and (p / "SKILL.md").is_file()):
        _try(imp, report, "skill", d, dry_run)
    plugins = root / "plugins"
    for d in sorted(p for p in plugins.iterdir() if p.is_dir()) if plugins.is_dir() else []:
        if (d / "plugin.json").is_file():
            _try(imp, report, "plugin", d, dry_run, adopt=True, with_dependencies=True)
    personal = root / "personal"
    for d in sorted(p for p in personal.iterdir() if p.is_dir()) if personal.is_dir() else []:
        if not ((d / "skill.json").is_file() and (d / "skill.py").is_file()):
            continue
        try:
            name = imp.verify("skill", d)["snapshot"]["name"]
        except Problem:
            name = d.name
        if any(r["name"] == name for r in imp.list("skill")) and _same_personal(imp, name, d):
            report["skipped"].append({"item": f"skill:{d.name}", "why": f"the skill '{name}' is already imported (same script)"})
            continue
        _try(imp, report, "skill", d, dry_run)
    report["settings"] = _undo_settings(imp, root, dry_run)
    report["source_untouched"] = True
    return report


def _undo_settings(imp: Importer, root: Path, dry_run: bool) -> List[Dict[str, Any]]:
    """Takes the folder out of `skill_dirs` and removes the override that pointed the personal skills at it."""
    out: List[Dict[str, Any]] = []
    path = imp.spaces.cap_dir / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and any(_norm(d) == _norm(root) for d in data.get("skill_dirs", []) if isinstance(d, str)):
        out.append({"file": str(path), "did": f"removed {root} from skill_dirs"})
        if not dry_run:
            data["skill_dirs"] = [d for d in data["skill_dirs"] if not (isinstance(d, str) and _norm(d) == _norm(root))]
            write_json(path, data)
    override = imp.spaces.cap_dir.parent / PERSONAL_OVERRIDE
    try:
        chosen = json.loads(override.read_text(encoding="utf-8")).get("dir")
    except (OSError, ValueError, AttributeError):
        chosen = None
    if isinstance(chosen, str) and (_norm(chosen) == _norm(root / "personal") or _norm(chosen).startswith(_norm(root) + os.sep)):
        out.append({"file": str(override), "did": f"set aside (renamed to {override.name}.undone): the personal skills are read from {imp.spaces.personal_dir}"})
        if not dry_run:
            os.replace(override, override.with_name(override.name + ".undone"))
    return out
