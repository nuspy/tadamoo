"""Plans: a plan file becomes a plan of the planner (the global plans space), and that plan is the one the app shows and runs. The source file
is only read again when the person asks to re-import.

A re-import swaps the steps for the source's and keeps what the plan did: its history, its journal and its values, and the state of every step
whose definition did not change."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

from .common import Problem, Snapshot, canonical, now_iso, sha_text

KIND = "plan"
MAX_PLAN = 5_000_000
RUNTIME = ("status", "state_variables", "position")
TOP_RUNTIME = ("created_at", "updated_at", "shared_state", "journal", "insight_card")


def _planner() -> Any:
    import tadamoo.planner as planner
    return planner


def node_hash(node: Any) -> str:
    from tadamoo.planner.dag import node_to_dict
    return sha_text(canonical({k: v for k, v in node_to_dict(node).items() if k not in RUNTIME}))


def def_hash(dag: Any) -> str:
    """A hash of what a plan is made of, without what running it wrote or where its steps sit on the canvas."""
    d = dag.to_dict()
    for k in TOP_RUNTIME:
        d.pop(k, None)
    d["nodes"] = {nid: {k: v for k, v in n.items() if k not in RUNTIME} for nid, n in d["nodes"].items()}
    return sha_text(canonical(d))


def _build(plan: Dict[str, Any]) -> Any:
    p = _planner()
    try:
        dag = p.PlanDAG.from_dict(plan)
        dag.validate_acyclic()
        dag.enforce_decisions()
    except (p.PlanError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Problem("bad_plan", f"That plan is not valid: {exc}") from None
    return dag


class PlanKind:
    kind = KIND

    def __init__(self, imp: Any) -> None:
        self.imp = imp

    def store(self) -> Any:
        return self.imp.plans()

    # -- reading a source ----------------------------------------------------------------------------------

    def snapshot(self, path: Any, **_: Any) -> Snapshot:
        p = Path(str(path or "").strip().strip('"'))
        if not p.is_file():
            raise Problem("no_source", f"There is no plan file at {p}.")
        try:
            if p.stat().st_size > MAX_PLAN:
                raise Problem("too_big", "That file is too large to be a plan.")
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError):
            raise Problem("bad_source", "That file is not valid JSON.") from None
        if not isinstance(data, dict):
            raise Problem("bad_plan", "That file is not a plan.")
        return self.snapshot_from(data, str(p))

    def snapshot_from(self, data: Dict[str, Any], source: str) -> Snapshot:
        from tadamoo.planner.bridge import PLAN_ID, RESERVED_IDS
        plan = data["plan"] if isinstance(data.get("plan"), dict) else data
        if not plan.get("plan_id") or not plan.get("title") or not isinstance(plan.get("nodes"), dict):
            raise Problem("bad_plan", "That file is not a plan: it needs a plan_id, a title and its steps.")
        dag = _build(plan)
        if not PLAN_ID.match(dag.plan_id) or dag.plan_id in RESERVED_IDS:
            raise Problem("bad_name", f"The plan id '{dag.plan_id[:40]}' is not usable: 1-64 letters, digits, dash or underscore, and not one the server keeps.")
        warnings = []
        if isinstance(data.get("schedule"), dict) and data["schedule"]:
            warnings.append("The schedule in the file is not imported: a schedule is set in the app, on the imported plan.")
        return Snapshot(KIND, dag.plan_id, source, def_hash(dag), description=dag.title,
                        payload={"plan": plan, "card": data.get("card") if isinstance(data.get("card"), dict) else None,
                                 "profile": data.get("profile") if isinstance(data.get("profile"), dict) else None},
                        warnings=warnings, extra={"title": dag.title, "steps": len(dag.nodes)})

    # -- writing -------------------------------------------------------------------------------------------

    def blockers(self, snap: Snapshot, items: Dict[str, Any], adopt: bool = False, **_: Any) -> List[Dict[str, Any]]:
        store = self.store()
        if f"plan:{snap.name}" in items or not store.exists(snap.name):
            return []
        try:
            store.get(snap.name)
        except KeyError:
            return [{"code": "plan_archived", "message": f"A plan '{snap.name}' is in the archive. Restore or delete it first."}]
        if adopt:
            return []
        return [{"code": "plan_exists", "message": f"A plan with the id '{snap.name}' is already there and was not imported (made in the app, or imported another way)."}]

    def _baseline(self, bridge: Any) -> Dict[str, Any]:
        return {"statuses": {nid: n.status for nid, n in bridge.dag.nodes.items()}, "journal": len(bridge.dag.journal)}

    def add(self, txn: Any, snap: Snapshot, items: Dict[str, Any], parent: str = "", adopt: bool = False, **_: Any) -> Dict[str, Any]:
        p, store = _planner(), self.store()
        adopted = ""
        if store.exists(snap.name):                                  # adopt: the plan that is there is taken as the imported copy, as it is
            bridge = store.get(snap.name)
            adopted = "same" if def_hash(bridge.dag) == snap.hash else "differs"
        else:
            card = p.PlanInsightCard.from_dict(snap.payload["card"]) if snap.payload["card"] else None
            profile = p.PlanProfile.from_dict(snap.payload["profile"])
            bridge = store.create(_build(snap.payload["plan"]), card, None, profile, "import")
            txn.on_undo(lambda: store.delete(snap.name))
            bridge.record("user", "import", f"Imported from {snap.source}"[:200])
            self.imp.plan_changed(snap.name)
            store.save(snap.name)
        entry = {"kind": KIND, "name": snap.name, "version": "", "description": snap.description, "source": snap.source, "hash": snap.hash,
                 "copy_hash": def_hash(bridge.dag), "imported_at": now_iso(), "updated_at": now_iso(), "parent": parent, "steps": len(bridge.dag.nodes),
                 "base": self._baseline(bridge), **({"adopted": adopted} if adopted else {})}
        items[f"plan:{snap.name}"] = entry
        return entry

    def replace(self, txn: Any, snap: Snapshot, items: Dict[str, Any], entry: Dict[str, Any], **_: Any) -> None:
        store = self.store()
        try:
            bridge = store.get(entry["name"])
        except KeyError:
            raise Problem("plan_archived", "This plan is in the archive: restore it in the Plans tab first.") from None
        new = _build(snap.payload["plan"])
        old = bridge.dag
        kept = reset = 0
        for nid, node in new.nodes.items():
            before = old.nodes.get(nid)
            if before is not None and node_hash(before) == node_hash(node):
                node.status, node.state_variables = before.status, dict(before.state_variables)
                kept += 1
            else:
                reset += 1
        new.shared_state, new.journal, new.created_at = copy.deepcopy(old.shared_state), copy.deepcopy(old.journal), old.created_at
        rev, history = bridge.rev, list(bridge.history)

        def undo() -> None:
            bridge.dag, bridge.rev = old, rev
            bridge.history.clear()
            bridge.history.extend(history)
            store.save(entry["name"])
        txn.on_undo(undo)
        bridge.replace_dag(new, "user", f"Re-imported from {snap.source}"[:200])
        store.save(entry["name"])
        self.imp.plan_changed(entry["name"])
        entry.update(description=snap.description, source=snap.source, hash=snap.hash, copy_hash=def_hash(new), updated_at=now_iso(), steps=len(new.nodes),
                     base=self._baseline(bridge), last_reimport={"kept": kept, "reset": reset})

    def drop(self, txn: Any, items: Dict[str, Any], entry: Dict[str, Any]) -> None:
        store, pid = self.store(), entry["name"]
        active = store.dir / f"{pid}.json"
        before = active.read_bytes() if active.is_file() else None
        store.delete(pid)
        if before is not None:
            txn.on_undo(lambda: active.write_bytes(before))
        txn.remove_file(store.dir / "archive" / f"{pid}.json")
        items.pop(f"plan:{pid}", None)

    # -- what the copy is now ------------------------------------------------------------------------------

    def state(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Whether the plan is there, was edited since the import, and has run or been answered."""
        store, pid = self.store(), entry["name"]
        try:
            bridge = store.get(pid)
        except KeyError:
            archived = any(r["plan_id"] == pid for r in store.list_archived())
            return {"missing": not archived, "archived": archived, "edited": False, "ran": False}
        base = entry.get("base") or {}
        ran = {nid: n.status for nid, n in bridge.dag.nodes.items()} != base.get("statuses") or len(bridge.dag.journal) != base.get("journal", 0)
        return {"missing": False, "archived": False, "edited": def_hash(bridge.dag) != entry.get("copy_hash"), "ran": ran}

    def local_changes(self, entry: Dict[str, Any]) -> List[str]:
        s = self.state(entry)
        return ["missing"] if s["missing"] else ["definition"] if s["edited"] else []

    def preview(self, snap: Snapshot, entry: Dict[str, Any]) -> Dict[str, Any]:
        try:
            old = self.store().get(entry["name"]).dag
        except KeyError:
            raise Problem("plan_archived", "This plan is in the archive: restore it in the Plans tab first.") from None
        new = _build(snap.payload["plan"])
        added = sorted(set(new.nodes) - set(old.nodes))
        removed = sorted(set(old.nodes) - set(new.nodes))
        changed = [{"file": nid} for nid in sorted(set(old.nodes) & set(new.nodes)) if node_hash(old.nodes[nid]) != node_hash(new.nodes[nid])]
        state = self.state(entry)
        return {"source_changed": snap.hash != entry["hash"], "ran": state["ran"],
                "diff": {"added": added, "removed": removed, "changed": changed, "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)}},
                "keeps": "history, journal, values and the state of every step that does not change"}
