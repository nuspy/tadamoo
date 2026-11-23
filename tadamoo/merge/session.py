"""A proposed update kept on disk under a token, between the moment it is shown and the moment the person applies it.

The caller lays out each thing to merge as a `Unit` (the installed copy as it is now, the new version, the original both started from) in folders under the work folder
this gives it. `propose` merges the units, carries out the person's instructions, asks the model for the description, keeps the proposal and answers what the page shows;
`load` brings it back; `write_finals` writes the merge as the person chose it. Nothing here knows what is being updated or where it is installed."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import assist, tree
from .errors import MergeProblem

SHOW_LIMIT = 40_000
KEEP_SECONDS = 6 * 3600


@dataclass
class Unit:
    """One thing an update merges: the installed copy as it is now, the new version, and the original both started from (None when it is not known)."""
    key: str
    label: str
    ours: Path
    theirs: Path
    base: Optional[Path] = None
    base_from: str = ""                  # `original` (kept when it was installed), `copy` (the installed copy, which was never edited) or empty
    hint: str = ""                       # the name of the source folder: the merged folder is given the same name, because a skill or plugin without a name of its own is called by it


def hash_folder(folder: Path) -> str:
    h = hashlib.sha256()
    for rel, data in sorted(tree.read_tree(folder).items()):
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def _show(data: Optional[bytes]) -> Optional[str]:
    if data is None:
        return None
    text = tree.as_text(data)
    return None if text is None else text[:SHOW_LIMIT]


def public(unit: Unit, files: Dict[str, tree.FileMerge]) -> Dict[str, Any]:
    """One unit as the page shows it: every file that differs with its three versions (the installed one, the new one, the proposed merge)."""
    rows = []
    for p, f in files.items():
        if f.outcome == tree.SAME:
            continue
        rows.append({"path": p, "outcome": f.outcome, "conflicts": f.conflicts, "text": f.is_text and tree.as_text(f.ours) is not None and tree.as_text(f.theirs) is not None,
                     "needs_choice": f.needs_choice, "installed": _show(f.ours), "new": _show(f.theirs), "merged": _show(f.merged),
                     "in_installed": f.ours is not None, "in_new": f.theirs is not None, "in_merge": f.merged is not None})
    return {"key": unit.key, "label": unit.label, "has_base": unit.base is not None, "base_from": unit.base_from, "counts": tree.counts(files), "files": rows}


class Session:
    def __init__(self, staging: Path) -> None:
        self.staging = Path(staging)

    def sweep(self) -> None:
        """Proposals nobody applied are not kept for long."""
        if not self.staging.is_dir():
            return
        for p in self.staging.iterdir():
            try:
                if p.is_dir() and time.time() - p.stat().st_mtime > KEEP_SECONDS:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue

    def discard(self, token: str) -> None:
        if isinstance(token, str) and token.isalnum():
            shutil.rmtree(self.staging / token, ignore_errors=True)

    def new_work(self) -> Tuple[str, Path]:
        self.sweep()
        token = uuid.uuid4().hex[:16]
        return token, self.staging / token

    # -- proposing ---------------------------------------------------------------------------------------------

    def propose(self, token: str, work: Path, meta: Dict[str, Any], units: List[Unit], instructions: List[str], client: Any, language: str = "en") -> Dict[str, Any]:
        """Merges every unit, carries out the instructions of this update, has the update described, keeps the proposal and answers what the page shows.
        A failure leaves nothing on disk."""
        try:
            rows = [{"unit": u, "files": tree.plan_trees(u.base, u.ours, u.theirs)} for u in units]
            notes = [n for n in (" ".join(str(i).split())[:600] for i in instructions) if n]
            applied: Dict[str, Dict[str, str]] = {}
            if notes:
                for r in rows:
                    applied[r["unit"].key] = assist.apply_notes(client, r["files"], notes, language)
            everything = {(f"{r['unit'].key}: {p}" if len(rows) > 1 else p): f for r in rows for p, f in r["files"].items()}
            has_base = all(r["unit"].base is not None for r in rows)
            description = assist.describe(client, meta.get("name", ""), everything, language, has_base)
            self._save(work, meta, rows, notes)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        return {"token": token, "has_base": has_base, "notes": notes, "notes_result": applied, "description": description,
                "units": [public(r["unit"], r["files"]) for r in rows], "needs_choice": sum(1 for r in rows for f in r["files"].values() if f.needs_choice)}

    def _save(self, work: Path, meta: Dict[str, Any], rows: List[Dict[str, Any]], notes: List[str]) -> None:
        units = []
        for i, r in enumerate(rows):
            u: Unit = r["unit"]
            merged_dir = work / "merged" / str(i)
            merged_dir.mkdir(parents=True, exist_ok=True)
            info = {}
            for p, f in r["files"].items():
                if f.merged is not None:
                    target = merged_dir / p
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f.merged)
                info[p] = {"outcome": f.outcome, "conflicts": f.conflicts, "is_text": f.is_text, "unresolved": f.unresolved, "merged_is_none": f.merged is None}
            units.append({"key": u.key, "label": u.label, "hint": u.hint, "ours_hash": hash_folder(u.ours), "base_from": u.base_from, "files": info,
                          "dirs": {"ours": str(u.ours.relative_to(work)), "theirs": str(u.theirs.relative_to(work)), "base": str(u.base.relative_to(work)) if u.base else ""}})
        (work / "plan.json").write_text(json.dumps({**meta, "notes": notes, "units": units}, ensure_ascii=False), encoding="utf-8")

    def load(self, token: str) -> Tuple[Dict[str, Any], Path, List[Dict[str, Any]]]:
        if not isinstance(token, str) or not token.isalnum():
            raise MergeProblem("bad_request", "Send the `token` the update was read with.")
        work = self.staging / token
        try:
            meta = json.loads((work / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise MergeProblem("no_plan", "That update was read a while ago and is not kept any more: read it again.") from None
        rows = []
        for i, u in enumerate(meta["units"]):
            ours, theirs = work / u["dirs"]["ours"], work / u["dirs"]["theirs"]
            base = work / u["dirs"]["base"] if u["dirs"]["base"] else None
            b, o, t = tree.read_tree(base), tree.read_tree(ours), tree.read_tree(theirs)
            merged = tree.read_tree(work / "merged" / str(i))
            files = {p: tree.FileMerge(p, info["outcome"], b.get(p), o.get(p), t.get(p), None if info["merged_is_none"] else merged.get(p, b""), info["conflicts"], info["is_text"],
                                       info["unresolved"]) for p, info in u["files"].items()}
            rows.append({"unit": Unit(u["key"], u["label"], ours, theirs, base, u["base_from"], u.get("hint", "")), "files": files, "ours_hash": u["ours_hash"]})
        return meta, work, rows

    # -- applying ----------------------------------------------------------------------------------------------

    @staticmethod
    def resolutions(rows: List[Dict[str, Any]], given: Any) -> Dict[str, Dict[str, str]]:
        """What the person picked for each file, by unit: `{unit: {path: choice}}`, `{"unit::path": choice}` or, with one unit, `{path: choice}`."""
        out: Dict[str, Dict[str, str]] = {r["unit"].key: {} for r in rows}
        if given in (None, ""):
            return out
        if not isinstance(given, dict):
            raise MergeProblem("bad_request", "`resolutions` is an object of file to \"ours\", \"theirs\" or \"merged\".")
        for key, value in given.items():
            if isinstance(value, dict):
                if key not in out:
                    raise MergeProblem("bad_request", f"'{str(key)[:60]}' is not part of this update.")
                out[key].update({str(p): str(c) for p, c in value.items()})
                continue
            unit, _, path = str(key).partition("::") if "::" in str(key) else ("", "", str(key))
            if unit and unit not in out:
                raise MergeProblem("bad_request", f"'{unit[:60]}' is not part of this update.")
            home = unit or next((r["unit"].key for r in rows if path in r["files"]), "")
            if not home:
                raise MergeProblem("bad_request", f"'{path[:80]}' is not a file of this update.")
            out[home][path] = str(value)
        return out

    @staticmethod
    def open_files(rows: List[Dict[str, Any]], chosen: Dict[str, Dict[str, str]]) -> List[str]:
        """The files that cannot be written yet, as `unit::path` (or `path` with one unit)."""
        return [f"{r['unit'].key}::{p}" if len(rows) > 1 else p for r in rows for p in tree.unresolved(r["files"], chosen[r["unit"].key])]

    @staticmethod
    def write_finals(work: Path, rows: List[Dict[str, Any]], chosen: Dict[str, Dict[str, str]]) -> Dict[str, Path]:
        finals: Dict[str, Path] = {}
        for i, r in enumerate(rows):
            final = work / "final" / str(i) / (r["unit"].hint or "unit")
            final.mkdir(parents=True)
            tree.write_final(r["files"], chosen[r["unit"].key], final)
            finals[r["unit"].key] = final
        return finals

    @staticmethod
    def unchanged_since(rows: List[Dict[str, Any]], units: List[Unit]) -> bool:
        """Whether the installed copies are still as they were when the proposal was made."""
        return [hash_folder(u.ours) for u in units] == [r["ours_hash"] for r in rows]
