"""The import service: what is imported, and import, re-import and delete for skills, plugins, MCP servers and plans.

Every kind works on COPIES inside the user data folder; the source is read and never written. What is imported is recorded in
`capabilities/imported/manifest.json` (where it came from, when, and a content hash), so a re-import can say what changed and a local change
is never overwritten without saying so."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .common import Lock, Manifest, Problem, Snapshot, Txn
from .mcp import McpKind
from .plans import PlanKind
from .plugins import PluginKind
from .skills import SkillKind

KINDS = ("skill", "plugin", "mcp", "plan")
ROW_FIELDS = ("kind", "name", "version", "description", "source", "imported_at", "updated_at", "hash", "files", "bytes", "parent", "parts", "reused", "dependencies",
              "forms", "runs", "needs_keys", "steps", "last_reimport", "adopted")


@dataclass
class Spaces:
    """Where the copies live: the capabilities folder (imported skills, plugins and servers, and the manifest), the planner's plans and the personal skills."""
    cap_dir: Path
    plans_dir: Path
    personal_dir: Path

    @property
    def root(self) -> Path:
        return self.cap_dir / "imported"

    @classmethod
    def from_data(cls, data: Path) -> "Spaces":
        data = Path(data)
        return cls(data / "capabilities", data / "planner" / "plans", data / "personal-skills")


class Importer:
    def __init__(self, spaces: Spaces, plan_store: Any = None, on_plan_changed: Optional[Callable[[str], None]] = None) -> None:
        """`plan_store` is the PlanStore plans are kept in (or a function giving it); the one the running app uses, so that it sees the change.
        `on_plan_changed(plan_id)` is called after a plan is made or replaced."""
        self.spaces = spaces
        self.root = spaces.root
        self._plan_store = plan_store
        self.on_plan_changed = on_plan_changed
        self.manifest = Manifest(self.root / "manifest.json")
        self.kinds: Dict[str, Any] = {"skill": SkillKind(self), "plugin": PluginKind(self), "mcp": McpKind(self), "plan": PlanKind(self)}

    def plans(self) -> Any:
        store = self._plan_store() if callable(self._plan_store) else self._plan_store
        if store is None:
            from tadamoo.planner import PlanStore
            store = self._plan_store = PlanStore(self.spaces.plans_dir)
        return store

    def plan_changed(self, plan_id: str) -> None:
        if self.on_plan_changed is not None:
            self.on_plan_changed(plan_id)

    def _kind(self, kind: Any) -> Any:
        if kind not in self.kinds:
            raise Problem("bad_kind", f"Import handles {', '.join(KINDS)}; '{str(kind)[:20]}' is none of them.")
        return self.kinds[kind]

    # -- reading ---------------------------------------------------------------------------------------------

    def _row(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        k = self.kinds[entry["kind"]]
        row = {f: entry[f] for f in ROW_FIELDS if f in entry}
        row["source_exists"] = Path(str(entry.get("source", "")).split("#", 1)[0]).exists()
        row["local_edited"] = bool(k.local_changes(entry))
        if entry["kind"] == "mcp":
            row["enabled"] = k.enabled(entry["name"])
        if entry["kind"] == "plan":
            row.update({key: v for key, v in k.state(entry).items() if key in ("ran", "archived", "missing")})
        return row

    def list(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with Lock():
            if kind is not None:
                self._kind(kind)
            items = self.manifest.load()
            return [self._row(e) for k, e in sorted(items.items(), key=lambda kv: (kv[1].get("kind", ""), kv[1].get("name", ""))) if kind in (None, e.get("kind"))]

    def verify(self, kind: str, path: Any, **opts: Any) -> Dict[str, Any]:
        """Reads a source and says what importing it would copy and register. Nothing is written."""
        with Lock():
            k = self._kind(kind)
            items = self.manifest.load()
            snap = k.snapshot(path, items=items, **opts)
            key = f"{kind}:{snap.name}"
            out: Dict[str, Any] = {"snapshot": snap.public(), "already_imported": key in items, "blockers": [], "can_import": key not in items}
            if key in items:
                return out
            out["blockers"] = k.blockers(snap, items, **{o: v for o, v in opts.items() if o == "adopt"})
            out["can_import"] = not out["blockers"]
            if kind == "plugin":
                rows = k.plan_parts(snap, items, adopt=bool(opts.get("adopt")))
                out["parts"] = [{"kind": r["snap"].kind, "name": r["snap"].name, "state": r["state"], "problems": r["problems"]} for r in rows]
            return out

    # -- writing ---------------------------------------------------------------------------------------------

    def _run(self, work: Callable[[Txn, Dict[str, Any]], Any]) -> Any:
        """Runs a change with the manifest loaded and saved around it, and undoes everything if any step fails."""
        shutil.rmtree(self.root / ".work", ignore_errors=True)
        txn = Txn(self.root / ".work")
        items = self.manifest.load()
        try:
            result = work(txn, items)
            txn.write_file(self.manifest.path, {"version": 1, "items": items})
        except BaseException:
            txn.rollback()
            shutil.rmtree(self.root / ".work", ignore_errors=True)
            raise
        txn.commit()
        shutil.rmtree(self.root / ".work", ignore_errors=True)
        return result

    def import_(self, kind: str, path: Any, **opts: Any) -> Dict[str, Any]:
        """Copies a source into the global space. A name that is already imported is refused: re-import it instead."""
        with Lock():
            k = self._kind(kind)
            items = self.manifest.load()
            snap = k.snapshot(path, items=items, **opts)
            key = f"{kind}:{snap.name}"
            if key in items:
                raise Problem("exists", f"'{snap.name}' is already imported. Re-import it to bring in the changes.", name=snap.name)
            blockers = k.blockers(snap, items, adopt=bool(opts.get("adopt")))
            if blockers:
                raise Problem("blocked", blockers[0]["message"], blockers=blockers)

            def work(txn: Txn, current: Dict[str, Any]) -> str:
                k.add(txn, snap, current, adopt=bool(opts.get("adopt")), with_dependencies=bool(opts.get("with_dependencies")))
                return key
            self._run(work)
            return {"imported": self._row(self.manifest.load()[key]), "snapshot": snap.public()}

    def reimport(self, kind: str, name: str, confirm: bool = False, overwrite_local: bool = False, **opts: Any) -> Dict[str, Any]:
        """Reads the recorded source again. Without `confirm` it only says what would change; with it, the copy is replaced. A copy that was
        changed here is replaced only with `overwrite_local`."""
        with Lock():
            k = self._kind(kind)
            items = self.manifest.load()
            key = f"{kind}:{name}"
            entry = items.get(key)
            if entry is None:
                raise Problem("not_imported", f"'{str(name)[:60]}' is not imported.")
            if entry.get("parent"):
                raise Problem("part", f"This {kind} belongs to {entry['parent']}: re-import that.", parent=entry["parent"])
            source = str(entry["source"])
            read = {"server": name} if kind == "mcp" else {}
            if not Path(source.split("#", 1)[0]).exists():
                raise Problem("source_missing", f"The source is not there any more: {source}", source=source)
            snap = k.snapshot(source, items=items, **read)
            if snap.name != name:
                raise Problem("renamed", f"The source is now called '{snap.name}', not '{name}': import it as a new one.")
            preview = k.preview(snap, entry)
            local = k.local_changes(entry)
            out = {"name": name, "kind": kind, "source": source, "local_changes": local, "local_edited": bool(local), "snapshot": snap.public(), **preview}
            if not confirm:
                return {"status": "preview", **out}
            if local and not overwrite_local:
                raise Problem("local_edits", f"The copy of '{name}' was changed here since it was imported. Confirm to replace it with the source.", **{"local_changes": local})
            if not preview["source_changed"] and not local:
                return {"status": "unchanged", **out}

            def work(txn: Txn, current: Dict[str, Any]) -> None:
                k.replace(txn, snap, current, current[key], adopt=bool(opts.get("adopt")), with_dependencies=bool(opts.get("with_dependencies")))
            self._run(work)
            return {"status": "replaced", **out, "imported": self._row(self.manifest.load()[key])}

    def delete(self, kind: str, name: str) -> Dict[str, Any]:
        """Removes the copy and its manifest entry (and, for a plugin, its parts). The source is never touched."""
        with Lock():
            k = self._kind(kind)
            items = self.manifest.load()
            key = f"{kind}:{name}"
            entry = items.get(key)
            if entry is None:
                raise Problem("not_imported", f"'{str(name)[:60]}' is not imported.")
            if entry.get("parent"):
                raise Problem("part", f"This {kind} belongs to {entry['parent']}: delete that, and its parts go with it.", parent=entry["parent"])
            removed = [key, *[p for p in entry.get("parts", []) if p in items]]

            def work(txn: Txn, current: Dict[str, Any]) -> None:
                k.drop(txn, current, current[key])
            self._run(work)
            return {"deleted": key, "removed": removed, "source": entry.get("source", "")}

    def set_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        """Turns an imported MCP server on or off."""
        with Lock():
            if f"mcp:{name}" not in self.manifest.load():
                raise Problem("not_imported", f"'{str(name)[:60]}' is not an imported MCP server.")

            def work(txn: Txn, current: Dict[str, Any]) -> None:
                self.kinds["mcp"].set_enabled(txn, name, bool(enabled))
            self._run(work)
            return {"name": name, "enabled": self.kinds["mcp"].enabled(name)}
