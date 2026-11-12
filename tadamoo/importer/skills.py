"""Skills: a folder with a SKILL.md (copied to the imported skills, where the capability index reads it) and/or a skill.json + skill.py (saved
among the person's personal skills). A folder with both is both, under one name."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from tadamoo.personal_skills.store import MAX_FILE as PSKILL_FILE, PersonalSkills, SkillError, digest, slugify, spec_without
from tadamoo.skills import parse_frontmatter

from .common import NAME, Problem, Snapshot, Tree, check_name, copy_tree, diff_trees, line_delta, now_iso, scan_tree

KIND = "skill"


def _read(path: Path, what: str) -> str:
    try:
        if path.stat().st_size > PSKILL_FILE * 4:
            raise Problem("too_big", f"{what} is too large to be a skill file.")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise Problem("unreadable", f"{what} cannot be read as text.") from None


class SkillKind:
    kind = KIND

    def __init__(self, imp: Any) -> None:
        self.imp = imp

    def store(self) -> PersonalSkills:
        return PersonalSkills(self.imp.spaces.personal_dir)

    def folder(self, name: str) -> Path:
        return self.imp.root / "skills" / check_name(name)

    # -- reading a source ----------------------------------------------------------------------------------

    def snapshot(self, path: Any, **_: Any) -> Snapshot:
        p = Path(str(path or "").strip().strip('"'))
        if p.is_file() and p.name in ("SKILL.md", "skill.json", "skill.py"):
            p = p.parent
        if not p.is_dir():
            raise Problem("no_source", f"There is no skill folder at {p}.")
        md = (p / "SKILL.md").is_file()
        py = (p / "skill.json").is_file() and (p / "skill.py").is_file()
        if not md and not py:
            raise Problem("not_a_skill", f"{p.name} has no SKILL.md and no skill.json with skill.py: it is not a skill.")
        tree = scan_tree(p)
        if not md:                                                  # a personal skill alone is those two files; the rest of the folder is not kept
            tree.files = {k: v for k, v in tree.files.items() if k in ("skill.json", "skill.py")}
            tree.sizes = {k: v for k, v in tree.sizes.items() if k in tree.files}
        meta: Dict[str, str] = {}
        spec: Dict[str, Any] = {}
        script = ""
        warnings: List[str] = []
        if md:
            meta, _body = parse_frontmatter(_read(p / "SKILL.md", "SKILL.md"))
        if py:
            try:
                spec = json.loads(_read(p / "skill.json", "skill.json"))
            except ValueError:
                raise Problem("bad_skill", "skill.json is not valid JSON.") from None
            if not isinstance(spec, dict):
                raise Problem("bad_skill", "skill.json is not a skill definition.")
            script = _read(p / "skill.py", "skill.py")
        if md:
            raw = (meta.get("name") or "").strip()
            name = raw if NAME.match(raw) else p.name
        else:
            name = slugify(str(spec.get("name") or p.name))
        check_name(name, "skill name")
        slug, clean = "", {}
        if py:
            slug = slugify(name if md else str(spec.get("name") or name))
            try:
                clean = PersonalSkills.validate(spec, script)
            except SkillError as exc:
                raise Problem("bad_skill", f"The personal skill is refused: {exc}") from None
        if md and not (meta.get("description") or "").strip():
            warnings.append("SKILL.md has no description: the agent finds a skill by it.")
        description = (meta.get("description") or str(spec.get("description") or "")).strip()
        return Snapshot(KIND, name, str(p), tree.hash, version=str(meta.get("version") or ""), description=" ".join(description.split()), tree=tree,
                        payload={"md": md, "py": py, "slug": slug, "spec": spec, "script": script, "clean": clean if py else {}},
                        warnings=warnings, extra={"forms": [f for f, on in (("SKILL.md", md), ("personal", py)) if on]})

    # -- what is in the way of a new import ----------------------------------------------------------------

    def blockers(self, snap: Snapshot, items: Dict[str, Any], **_: Any) -> List[Dict[str, Any]]:
        out = []
        slug = snap.payload["slug"]
        if slug and (self.imp.spaces.personal_dir / slug).exists():
            owner = next((k for k, v in items.items() if v.get("personal", {}).get("slug") == slug), "")
            if not owner:
                out.append({"code": "personal_exists", "message": f"A personal skill named '{slug}' is already there and was not imported. Delete or rename it first."})
        if snap.payload["md"] and self.folder(snap.name).exists() and f"skill:{snap.name}" not in items:
            out.append({"code": "folder_exists", "message": f"The imported skills folder already holds '{snap.name}' and the manifest does not know it."})
        return out

    # -- writing -------------------------------------------------------------------------------------------

    def _put_copy(self, txn: Any, snap: Snapshot, entry: Dict[str, Any]) -> None:
        if snap.payload["md"]:
            staged = txn.temp("skill")
            copy_tree(snap.tree, staged)
            txn.on_undo(lambda: shutil.rmtree(staged, ignore_errors=True))
            txn.install_dir(staged, self.folder(snap.name))
            entry["copy_hash"] = snap.tree.hash
        else:
            entry.pop("copy_hash", None)
        if snap.payload["py"]:
            slug = snap.payload["slug"]
            txn.backup_dir(self.imp.spaces.personal_dir / slug)
            try:
                saved = self.store().save(snap.payload["spec"], snap.payload["script"], slug=slug)
            except SkillError as exc:
                raise Problem("bad_skill", f"The personal skill is refused: {exc}") from None
            entry["personal"] = {"slug": saved["slug"], "copy_hash": digest(spec_without(saved), saved["script"])}
        else:
            entry.pop("personal", None)

    def add(self, txn: Any, snap: Snapshot, items: Dict[str, Any], parent: str = "", **_: Any) -> Dict[str, Any]:
        entry = {"kind": KIND, "name": snap.name, "version": snap.version, "description": snap.description, "source": snap.source, "hash": snap.hash,
                 "imported_at": now_iso(), "updated_at": now_iso(), "files": len(snap.tree.files), "bytes": snap.tree.total, "parent": parent,
                 "forms": snap.extra["forms"]}
        self._put_copy(txn, snap, entry)
        items[f"skill:{snap.name}"] = entry
        return entry

    def replace(self, txn: Any, snap: Snapshot, items: Dict[str, Any], entry: Dict[str, Any], **_: Any) -> None:
        entry.update(version=snap.version, description=snap.description, source=snap.source, hash=snap.hash, updated_at=now_iso(),
                     files=len(snap.tree.files), bytes=snap.tree.total, forms=snap.extra["forms"])
        old_slug = (entry.get("personal") or {}).get("slug")
        self._put_copy(txn, snap, entry)
        if old_slug and old_slug != (entry.get("personal") or {}).get("slug"):
            txn.remove_dir(self.imp.spaces.personal_dir / old_slug)
        if not snap.payload["md"]:
            txn.remove_dir(self.folder(snap.name))

    def drop(self, txn: Any, items: Dict[str, Any], entry: Dict[str, Any]) -> None:
        txn.remove_dir(self.folder(entry["name"]))
        slug = (entry.get("personal") or {}).get("slug")
        if slug:
            txn.remove_dir(self.imp.spaces.personal_dir / slug)
        items.pop(f"skill:{entry['name']}", None)

    # -- what the copy is now ------------------------------------------------------------------------------

    def local_changes(self, entry: Dict[str, Any]) -> List[str]:
        """What was changed in the copy since it was imported: the files of the folder, and the personal skill as it is saved now."""
        out = []
        if entry.get("copy_hash"):
            folder = self.folder(entry["name"])
            try:
                if scan_tree(folder).hash != entry["copy_hash"]:
                    out.append("files")
            except Problem:
                out.append("missing")
        personal = entry.get("personal")
        if personal:
            try:
                have = self.store().get(personal["slug"])
                if digest(spec_without(have), have["script"]) != personal["copy_hash"]:
                    out.append("personal")
            except SkillError:
                out.append("missing")
        return out

    def preview(self, snap: Snapshot, entry: Dict[str, Any]) -> Dict[str, Any]:
        """What re-importing would change, from the copy as it is now to the source as it is now."""
        folder = self.folder(entry["name"])
        if entry.get("copy_hash"):
            old = scan_tree(folder) if folder.is_dir() else Tree(folder)
            d = diff_trees(old, snap.tree if snap.payload["md"] else Tree(snap.tree.root))
        else:
            d = {"added": [], "removed": [], "changed": [], "counts": {"added": 0, "removed": 0, "changed": 0}}
            personal = entry.get("personal")
            if personal and snap.payload["py"]:
                try:
                    have = self.store().get(personal["slug"])
                except SkillError:
                    have = None
                rows = []
                if have is None or have["script"] != snap.payload["script"]:
                    rows.append({"file": "skill.py", **(line_delta(have["script"], snap.payload["script"]) if have else {})})
                if have is None or any(have.get(k) != v for k, v in snap.payload["clean"].items()):
                    rows.append({"file": "skill.json"})
                d.update(changed=rows, counts={"added": 0, "removed": 0, "changed": len(rows)})
        return {"source_changed": snap.hash != entry["hash"], "diff": d}
