"""An update that keeps what the person changed.

A copy that was imported is often edited here, and its source moves on. Updating by replacing the copy loses those edits; this reads three versions instead:
the installed copy as it is now, the new source, and the original the copy was made from (kept at import, in `imported/.originals`). Where only one side changed a
file that side wins; where both changed a text file in different places the changes are joined line by line; where both changed the same place the person chooses
(tadamoo/merge/). The proposal is kept in a staging folder under a token, shown (the three versions of every file that differs, and a description in plain words
when a model is there), and written only when the person applies it: as the merge, as a plain overwrite, or not at all.

The person can tell the model how to change the merge. Those instructions are carried out on the proposal, and may be kept with the item so that the next update
carries them out again on its own."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from tadamoo.merge import tree
from tadamoo.merge.session import MergeProblem, Session

from .common import Lock, Problem, now_iso

CHOICES = ("merge", "overwrite", "cancel")


def _problem(exc: MergeProblem) -> Problem:
    return Problem(exc.code, str(exc), **exc.data)


class Updater:
    def __init__(self, imp: Any) -> None:
        self.imp = imp
        self.session = Session(imp.root / ".merge")

    def discard(self, token: str) -> None:
        self.session.discard(token)

    def load(self, token: str) -> Any:
        try:
            return self.session.load(token)
        except MergeProblem as exc:
            raise _problem(exc) from None

    # -- reading what an update would do ----------------------------------------------------------------------

    def _read_source(self, kind: str, name: str, source: Optional[str]) -> Any:
        imp = self.imp
        k = imp._kind(kind)
        if not hasattr(k, "merge_units"):
            raise Problem("not_mergeable", f"A {kind} is brought up to date by importing it again: its steps are replaced and what it did is kept, so there is nothing to merge.")
        items = imp.manifest.load()
        entry = items.get(f"{kind}:{name}")
        if entry is None:
            raise Problem("not_imported", f"'{str(name)[:60]}' is not imported.")
        if entry.get("parent"):
            raise Problem("part", f"This {kind} belongs to {entry['parent']}: update that.", parent=entry["parent"])
        source = str(source or entry["source"])
        if not Path(source.split("#", 1)[0]).exists():
            raise Problem("source_missing", f"The source is not there any more: {source}", source=source)
        snap = k.snapshot(source, items=items, **({"server": name} if kind == "mcp" else {}))
        if snap.name != name:
            raise Problem("renamed", f"The source is now called '{snap.name}', not '{name}': import it as a new one.")
        return k, items, entry, snap, source

    def plan(self, kind: str, name: str, source: Optional[str] = None, instructions: Optional[List[str]] = None, language: str = "en", **_: Any) -> Dict[str, Any]:
        """The merged update of `kind:name`, proposed and kept under a token; nothing of the installed copy is changed. `instructions` are what the person told the
        merge to do this time (those kept with the item are carried out too)."""
        imp = self.imp
        with Lock():
            k, items, entry, snap, source = self._read_source(kind, name, source)
            preview = k.preview(snap, entry)
            local = k.local_changes(entry)
            if not preview["source_changed"] and not local:
                return {"status": "unchanged", "name": name, "kind": kind, "source": source, "local_changes": [], "local_edited": False}
            token, work = self.session.new_work()
            try:
                units = k.merge_units(snap, entry, work / "in")
                client = imp.model() if imp.model is not None else None
                meta = {"kind": kind, "name": name, "source": source, "hash": snap.hash, "entry_hash": entry.get("hash")}
                out = self.session.propose(token, work, meta, units, imp.merge_notes(kind, name), instructions or [], client, language)
            except MergeProblem as exc:
                shutil.rmtree(work, ignore_errors=True)
                raise _problem(exc) from None
            except BaseException:
                shutil.rmtree(work, ignore_errors=True)
                raise
            return {"status": "proposed", "kind": kind, "name": name, "source": source, "local_changes": local, "local_edited": bool(local), "source_changed": preview["source_changed"],
                    "parts": preview.get("parts", []), "parts_removed": preview.get("parts_removed", []), "choices": list(CHOICES), **out}

    # -- doing what the person chose ----------------------------------------------------------------------------

    def apply(self, token: str, choice: str = "merge", resolutions: Any = None, remember: bool = False, **_: Any) -> Dict[str, Any]:
        """`merge`: writes the proposal, with the person's choice for every file that was a conflict. `overwrite`: replaces the copy with the source, the person's changes
        gone. `cancel`: leaves everything as it is. The proposal is dropped in each case."""
        imp = self.imp
        if choice not in CHOICES:
            raise Problem("bad_request", f"`choice` is one of {', '.join(CHOICES)}.")
        with Lock():
            meta, work, rows = self.load(token)
            kind, name = meta["kind"], meta["name"]
            if choice == "cancel":
                self.discard(token)
                return {"status": "cancelled", "kind": kind, "name": name}
            if choice == "overwrite":
                out = imp.reimport(kind, name, confirm=True, overwrite_local=True, source=meta["source"])
                self.discard(token)
                return out
            k, items, entry, snap, source = self._read_source(kind, name, meta["source"])
            if snap.hash != meta["hash"] or entry.get("hash") != meta["entry_hash"]:
                raise Problem("stale", "The source or the installed copy changed after this update was read: read it again.")
            again = work / "again"
            try:
                if not self.session.unchanged_since(rows, k.merge_units(snap, entry, again)):
                    raise Problem("stale", "The installed copy was changed after this update was read: read it again.")
            finally:
                shutil.rmtree(again, ignore_errors=True)
            try:
                chosen = self.session.resolutions(rows, resolutions)
            except MergeProblem as exc:
                raise _problem(exc) from None
            open_ = self.session.open_files(rows, chosen)
            if open_:
                raise Problem("conflicts", "Some files need a choice (the installed one, the new one, or the merge once it holds no conflict): " + ", ".join(open_[:5]), files=open_)
            merged = k.merged_snapshot(snap, self.session.write_finals(work, rows, chosen))
            key = f"{kind}:{name}"
            notes = meta["notes"]

            def write(txn: Any, current: Dict[str, Any]) -> None:
                k.replace(txn, merged, current, current[key])
                if remember:
                    if notes:
                        current[key]["merge_notes"] = [{"text": t, "at": now_iso()} for t in notes]
                    else:
                        current[key].pop("merge_notes", None)
            imp._run(write)
            self.discard(token)
            return {"status": "merged", "kind": kind, "name": name, "counts": {r["unit"].key: tree.counts(r["files"]) for r in rows}, "kept_notes": notes if remember else [],
                    "imported": imp._row(imp.manifest.load()[key])}
