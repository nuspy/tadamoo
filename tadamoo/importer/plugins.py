"""Plugins: a folder with a plugin.json. The folder is copied, and its parts are imported as copies in their own places (its skills as imported
skills, its plans as imported plans, its MCP servers as imported servers, turned off), all registered as parts of the plugin: deleting the
plugin deletes them. The skills it depends on are reported, and imported too when the person says so; they stay when the plugin goes."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from .common import Problem, Snapshot, Tree, Unit, check_name, copy_tree, diff_trees, now_iso, safe_join, scan_tree

KIND = "plugin"
MANIFESTS = ("plugin.json", ".claude-plugin/plugin.json")


def _paths(value: Any, key: str = "path") -> List[str]:
    """Relative paths a plugin.json lists, each a string or an object holding it under `key`."""
    out = []
    for v in value if isinstance(value, list) else []:
        r = v.get(key) or v.get("file") if isinstance(v, dict) else v
        if isinstance(r, str) and r.strip():
            out.append(r.strip())
    return out


class PluginKind:
    kind = KIND

    def __init__(self, imp: Any) -> None:
        self.imp = imp

    def folder(self, name: str) -> Path:
        return self.imp.root / "plugins" / check_name(name)

    # -- reading a source ----------------------------------------------------------------------------------

    def snapshot(self, path: Any, items: Dict[str, Any] | None = None, **_: Any) -> Snapshot:
        p = Path(str(path or "").strip().strip('"'))
        if p.is_file() and p.name == "plugin.json":
            p = p.parent.parent if p.parent.name == ".claude-plugin" else p.parent
        if not p.is_dir():
            raise Problem("no_source", f"There is no plugin folder at {p}.")
        manifest = next((p / m for m in MANIFESTS if (p / m).is_file()), None)
        if manifest is None:
            raise Problem("not_a_plugin", f"{p.name} has no plugin.json: it is not a plugin.")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError):
            raise Problem("bad_plugin", "plugin.json is not valid JSON.") from None
        if not isinstance(data, dict):
            raise Problem("bad_plugin", "plugin.json is not an object.")
        name = check_name(str(data.get("name") or p.name), "plugin name")
        tree = scan_tree(p)
        parts: List[Snapshot] = []

        def part(make: Any, what: str) -> None:
            try:
                snap = make()
            except Problem as exc:
                raise Problem(exc.code, f"{what}: {exc}", **exc.data) from None
            if all((q.kind, q.name) != (snap.kind, snap.name) for q in parts):
                parts.append(snap)

        def listed(key: str, folder: str, pattern: str) -> List[str]:
            rels = _paths(data.get(key))
            if key not in data and (p / folder).is_dir():
                rels = [x.relative_to(p).as_posix() for x in sorted((p / folder).glob(pattern)) if (x.is_dir() if folder == "skills" else x.is_file())]
            return rels

        for rel in listed("skills", "skills", "*"):
            part(lambda rel=rel: self.imp.kinds["skill"].snapshot(safe_join(p, rel)), f"skill {rel}")
        for rel in listed("plans", "plans", "*.plan.json"):
            part(lambda rel=rel: self.imp.kinds["plan"].snapshot(safe_join(p, rel)), f"plan {rel}")
        servers = data.get("mcp_servers")
        for i, spec in enumerate(servers if isinstance(servers, list) else []):
            if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                part(lambda spec=spec: self.imp.kinds["mcp"].snapshot_from({"mcpServers": {spec["name"]: {k: v for k, v in spec.items() if k != "name"}}},
                                                                           f"{manifest}#{spec['name']}", spec["name"]), f"MCP server {spec.get('name')}")
            elif isinstance(spec, str):
                f = safe_join(p, spec)
                part(lambda f=f: self.imp.kinds["mcp"].snapshot(f), f"MCP config {spec}")
        if (p / ".mcp.json").is_file():
            from .mcp import servers_in
            try:
                names = sorted(servers_in(json.loads((p / ".mcp.json").read_text(encoding="utf-8-sig"))))
            except (OSError, ValueError):
                names = []
            for n in names:
                part(lambda n=n: self.imp.kinds["mcp"].snapshot(p / ".mcp.json", server=n), f".mcp.json server {n}")
        deps = self.dependencies(p, data, items if items is not None else self.imp.manifest.load())
        warnings = [f"The plugin lists {d['name']} as a dependency, but its folder is not there ({d['source']})." for d in deps if not d["exists"] and not d["imported"]]
        return Snapshot(KIND, name, str(p), tree.hash, version=str(data.get("version") or ""), description=" ".join(str(data.get("description") or "").split()),
                        tree=tree, payload={"data": data}, parts=parts, dependencies=deps, warnings=warnings)

    def dependencies(self, p: Path, data: Dict[str, Any], items: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        deps = data.get("dependencies")
        for d in (deps.get("skills") if isinstance(deps, dict) and isinstance(deps.get("skills"), list) else []):
            name = d.get("name") if isinstance(d, dict) else d
            if not isinstance(name, str):
                continue
            check_name(name, "dependency name")
            rel = d.get("path") if isinstance(d, dict) and isinstance(d.get("path"), str) else ""
            src = (p / rel).resolve() if rel else None
            ok = bool(src and src.is_dir() and ((src / "SKILL.md").is_file() or (src / "skill.json").is_file()))
            out.append({"kind": "skill", "name": name, "why": str(d.get("why") or "")[:200] if isinstance(d, dict) else "", "source": str(src) if src else "",
                        "exists": ok, "imported": f"skill:{name}" in items})
        return out

    # -- what each part would do ---------------------------------------------------------------------------

    def plan_parts(self, snap: Snapshot, items: Dict[str, Any], own: str = "", adopt: bool = False) -> List[Dict[str, Any]]:
        """For each part: new (imported as a part), own (already a part of this plugin), reused (imported on its own and the same: left alone) or blocked."""
        out = []
        for part in snap.parts:
            key = f"{part.kind}:{part.name}"
            have = items.get(key)
            kind = self.imp.kinds[part.kind]
            if have is None:
                problems = kind.blockers(part, items, adopt=adopt)
                out.append({"snap": part, "key": key, "state": "blocked" if problems else "new", "problems": problems})
            elif have.get("parent") == own and own:
                out.append({"snap": part, "key": key, "state": "own", "problems": []})
            elif not have.get("parent") and have.get("hash") == part.hash:
                out.append({"snap": part, "key": key, "state": "reused", "problems": []})
            else:
                by = have.get("parent") or "on its own"
                out.append({"snap": part, "key": key, "state": "blocked",
                            "problems": [{"code": "part_exists", "message": f"The {part.kind} '{part.name}' is already imported ({by}) with other content. Re-import or delete it first."}]})
        return out

    def blockers(self, snap: Snapshot, items: Dict[str, Any], adopt: bool = False, **_: Any) -> List[Dict[str, Any]]:
        out = [p for row in self.plan_parts(snap, items, adopt=adopt) for p in row["problems"]]
        if self.folder(snap.name).exists() and f"plugin:{snap.name}" not in items:
            out.append({"code": "folder_exists", "message": f"The imported plugins folder already holds '{snap.name}' and the manifest does not know it."})
        return out

    def _bring_dependencies(self, txn: Any, snap: Snapshot, items: Dict[str, Any]) -> List[str]:
        done = []
        for d in snap.dependencies:
            if d["imported"] or not d["exists"]:
                continue
            dep = self.imp.kinds["skill"].snapshot(d["source"])
            if dep.name != d["name"]:
                raise Problem("bad_dependency", f"The dependency {d['name']} is a skill called {dep.name}.")
            problems = self.imp.kinds["skill"].blockers(dep, items)
            if problems:
                raise Problem("blocked", problems[0]["message"])
            self.imp.kinds["skill"].add(txn, dep, items)
            done.append(f"skill:{dep.name}")
        return done

    # -- writing -------------------------------------------------------------------------------------------

    def add(self, txn: Any, snap: Snapshot, items: Dict[str, Any], parent: str = "", adopt: bool = False, with_dependencies: bool = False, **_: Any) -> Dict[str, Any]:
        own = f"plugin:{snap.name}"
        brought = self._bring_dependencies(txn, snap, items) if with_dependencies else []
        claimed, reused = [], []
        for row in self.plan_parts(snap, items, adopt=adopt):
            if row["state"] == "blocked":
                raise Problem("blocked", row["problems"][0]["message"])
            if row["state"] == "reused":
                reused.append(row["key"])
                continue
            self.imp.kinds[row["snap"].kind].add(txn, row["snap"], items, parent=own, adopt=adopt)
            claimed.append(row["key"])
        staged = txn.temp("plugin")
        copy_tree(snap.tree, staged)
        txn.on_undo(lambda: shutil.rmtree(staged, ignore_errors=True))
        txn.install_dir(staged, self.folder(snap.name))
        self.imp.keep_original(txn, snap)
        entry = {"kind": KIND, "name": snap.name, "version": snap.version, "description": snap.description, "source": snap.source, "hash": snap.hash, "copy_hash": snap.tree.hash,
                 "imported_at": now_iso(), "updated_at": now_iso(), "files": len(snap.tree.files), "bytes": snap.tree.total, "parent": "", "parts": claimed,
                 "reused": reused, "dependencies": brought}
        items[own] = entry
        return entry

    def replace(self, txn: Any, snap: Snapshot, items: Dict[str, Any], entry: Dict[str, Any], adopt: bool = False, with_dependencies: bool = False, **_: Any) -> None:
        own = f"plugin:{snap.name}"
        brought = self._bring_dependencies(txn, snap, items) if with_dependencies else []
        rows = self.plan_parts(snap, items, own=own, adopt=adopt)
        for row in rows:
            if row["state"] == "blocked":
                raise Problem("blocked", row["problems"][0]["message"])
        keep = {r["key"] for r in rows if r["state"] in ("own", "new")}
        for key in [k for k in entry["parts"] if k not in keep and k in items]:                 # a part the plugin no longer has
            self.imp.kinds[items[key]["kind"]].drop(txn, items, items[key])
        claimed, reused = [], []
        for row in rows:
            part, key = row["snap"], row["key"]
            if row["state"] == "reused":
                reused.append(key)
            elif row["state"] == "own":
                if part.hash != items[key]["hash"] or self.imp.kinds[part.kind].local_changes(items[key]):
                    self.imp.kinds[part.kind].replace(txn, part, items, items[key])
                claimed.append(key)
            else:
                self.imp.kinds[part.kind].add(txn, part, items, parent=own, adopt=adopt)
                claimed.append(key)
        staged = txn.temp("plugin")
        copy_tree(snap.tree, staged)
        txn.on_undo(lambda: shutil.rmtree(staged, ignore_errors=True))
        txn.install_dir(staged, self.folder(snap.name))
        self.imp.keep_original(txn, snap)
        entry.update(version=snap.version, description=snap.description, source=snap.source, hash=snap.hash, copy_hash=snap.tree.hash, updated_at=now_iso(),
                     files=len(snap.tree.files), bytes=snap.tree.total, parts=claimed, reused=reused, dependencies=sorted({*entry.get("dependencies", []), *brought}))

    def drop(self, txn: Any, items: Dict[str, Any], entry: Dict[str, Any]) -> None:
        for key in entry.get("parts", []):
            if key in items:
                self.imp.kinds[items[key]["kind"]].drop(txn, items, items[key])
        txn.remove_dir(self.folder(entry["name"]))
        self.imp.drop_original(txn, KIND, entry["name"])
        items.pop(f"plugin:{entry['name']}", None)

    # -- an update that keeps what the person changed ------------------------------------------------------

    def keep_original(self, snap: Snapshot, dest: Path) -> None:
        for rel in snap.tree.files:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snap.tree.root / rel, target)

    def merge_units(self, snap: Snapshot, entry: Dict[str, Any], work: Path) -> List[Unit]:
        """The plugin folder, and each skill or server it brings that is already part of it (their own copies are where they are edited). Plans are not merged here."""
        name = entry["name"]
        ours, theirs = work / "ours", work / "theirs"
        ours.mkdir(parents=True)
        theirs.mkdir(parents=True)
        folder = self.folder(name)
        if folder.is_dir():
            shutil.copytree(folder, ours, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        for rel in snap.tree.files:
            target = theirs / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snap.tree.root / rel, target)
        base, base_from = work / "base", ""
        original = self.imp.originals(KIND, name)
        if original.is_dir():
            shutil.copytree(original, base)
            base_from = "original"
        elif folder.is_dir() and not self.local_changes_of_folder(entry):
            shutil.copytree(ours, base)                                  # the imported folder was never edited: it is the original
            base_from = "copy"
        units = [Unit(f"plugin:{name}", name, ours, theirs, base if base_from else None, base_from, Path(snap.source).name)]
        items = self.imp.manifest.load()
        for n, row in enumerate(self.plan_parts(snap, items, own=f"plugin:{name}")):
            if row["state"] == "own" and row["snap"].kind in ("skill", "mcp"):
                units += self.imp.kinds[row["snap"].kind].merge_units(row["snap"], items[row["key"]], work / f"part{n}")
        return units

    def local_changes_of_folder(self, entry: Dict[str, Any]) -> bool:
        try:
            return scan_tree(self.folder(entry["name"])).hash != entry.get("copy_hash")
        except Problem:
            return True

    def merged_snapshot(self, snap: Snapshot, finals: Dict[str, Path]) -> Snapshot:
        """The plugin as its merged folder holds it, with each part that was merged taken from its own merge and every other part as the source has it."""
        merged = self.snapshot(finals[f"plugin:{snap.name}"])
        for i, part in enumerate(merged.parts):
            source_part = next((p for p in snap.parts if (p.kind, p.name) == (part.kind, part.name)), None)
            if source_part is None:
                continue
            k = self.imp.kinds[part.kind]
            merged.parts[i] = k.merged_snapshot(source_part, finals) if f"{part.kind}:{part.name}" in finals and hasattr(k, "merged_snapshot") else source_part
        merged.hash, merged.source, merged.original = snap.hash, snap.source, snap
        return merged

    # -- what the copy is now ------------------------------------------------------------------------------

    def local_changes(self, entry: Dict[str, Any]) -> List[str]:
        out = []
        try:
            if scan_tree(self.folder(entry["name"])).hash != entry.get("copy_hash"):
                out.append("files")
        except Problem:
            out.append("missing")
        items = self.imp.manifest.load()
        for key in entry.get("parts", []):
            if key in items and self.imp.kinds[items[key]["kind"]].local_changes(items[key]):
                out.append(key)
        return out

    def preview(self, snap: Snapshot, entry: Dict[str, Any]) -> Dict[str, Any]:
        items = self.imp.manifest.load()
        own = f"plugin:{snap.name}"
        folder = self.folder(entry["name"])
        diff = diff_trees(scan_tree(folder) if folder.is_dir() else Tree(folder), snap.tree)
        rows = self.plan_parts(snap, items, own=own)
        parts = []
        for row in rows:
            part, key = row["snap"], row["key"]
            if row["state"] == "own":
                pv = self.imp.kinds[part.kind].preview(part, items[key])
                parts.append({"key": key, "state": "changed" if pv["source_changed"] else "same", "diff": pv["diff"]["counts"]})
            else:
                parts.append({"key": key, "state": row["state"], "problems": row["problems"]})
        keep = {r["key"] for r in rows}
        gone = [k for k in entry.get("parts", []) if k not in keep]
        changed = snap.hash != entry["hash"] or any(x["state"] in ("changed", "new") for x in parts) or bool(gone)
        return {"source_changed": changed, "diff": diff, "parts": parts, "parts_removed": gone, "dependencies": snap.dependencies,
                "blocked": [p for row in rows for p in row["problems"]]}
