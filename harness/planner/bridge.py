"""The canvas bridge: what the plan view and the planner say to each other, independent of transport.

Events in (the user edits a node, the model proposes a diff, the user approves, rejects or answers a
human-fallback offer), results out, and a numbered stream of what changed so a view can follow by
polling or by an event stream. Nothing a model proposes touches the plan until the user approves it.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from .card import PlanInsightCard
from .dag import Assignee, PlanDAG, PlanError, node_to_dict
from .horizon import PlanProfile
from .replan import ReplanProposal, Replanner
from .triage import TriageResult

PLAN_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
RESERVED_IDS = ("events", "telemetry")            # paths of the server's own routes under /api/plans
LOG_SIZE = 500
HISTORY_SIZE = 200
HISTORY_SHOWN = 30
# who made a change: the person at the page, the Chat for the person, the planner's own re-planning, the system
ACTORS = ("user", "chat", "planner", "system")
# events that write straight into the plan; they may carry `base_rev`, the revision the sender last read
DIRECT = ("plan:update_node", "plan:mark", "plan:rewind", "plan:update_meta", "plan:fallback_response")
TARGET_TYPES = ("constant", "occasional")
TITLE_CHARS = 200


def classify_diff(diff: Dict[str, Any]) -> Dict[str, List[str]]:
    """Which nodes a diff adds, removes and updates, for highlighting (edges mark both ends as updated)."""
    added = [n.get("id") for n in diff.get("add_nodes", []) if isinstance(n, dict)]
    removed = list(diff.get("remove_nodes", []))
    updated = list((diff.get("update_nodes") or {}).keys())
    for p, c in list(diff.get("add_edges", [])) + list(diff.get("remove_edges", [])):
        updated.extend([p, c])
    return {"added": added, "removed": removed, "updated": [u for u in dict.fromkeys(updated) if u not in added + removed]}


@dataclass
class Proposal:
    proposal_id: str
    title: str
    rationale: str
    diff: Dict[str, Any]
    source: str = "model"
    status: str = "pending"          # pending, applied, rejected, stale
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"proposal_id": self.proposal_id, "title": self.title, "rationale": self.rationale, "diff": self.diff,
                "source": self.source, "status": self.status, "created_at": self.created_at,
                "changes": classify_diff(self.diff)}


class PlanCanvasBridge:
    def __init__(self, dag: PlanDAG, card: Optional[PlanInsightCard] = None, replanner: Optional[Replanner] = None,
                 profile: Optional[PlanProfile] = None) -> None:
        self.dag = dag
        self.card = card
        self.profile = profile or PlanProfile()                # the plan's aims, budget, dates and cost: what it is compared on
        self.alignment: Dict[str, Any] = {}                    # the last reading of the model on how this plan sits with the long-term ones
        self.replanner = replanner
        self.proposals: Dict[str, Proposal] = {}
        self.offers: Dict[str, Dict[str, Any]] = {}      # human-fallback offers waiting for the user
        self.log: Deque[Dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self.history: Deque[Dict[str, Any]] = deque(maxlen=HISTORY_SIZE)    # what changed the plan, and who did it
        self.seq = 0
        self.rev = 0                                      # counts changes to the plan itself, not to proposals or offers
        self._actor = "user"
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._lock = threading.RLock()

    # -- stream ----------------------------------------------------------------------------------

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def _emit(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self.seq += 1
            event = {"seq": self.seq, "type": event_type, "plan_id": self.dag.plan_id, "rev": self.rev,
                     "actor": self._actor, **(payload or {})}
            self.log.append(event)
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                pass                                      # a slow or broken viewer must not stop the plan
        return event

    def events_since(self, seq: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self.log if e["seq"] > seq]

    def _bump(self, kind: str, summary: str) -> None:
        """A change to the plan: a new revision, and a line in the history naming who made it."""
        self.rev += 1
        self.history.append({"rev": self.rev, "t": time.time(), "actor": self._actor, "type": kind,
                             "summary": " ".join(str(summary).split())[:TITLE_CHARS]})

    def record(self, actor: str, kind: str, summary: str) -> None:
        """For what the store does around the plan (creating, restoring)."""
        with self._lock:
            previous, self._actor = self._actor, actor
            try:
                self._bump(kind, summary)
            finally:
                self._actor = previous

    def announce_archived(self, actor: str) -> None:
        """Tells the viewers that this plan is leaving the list."""
        with self._lock:
            previous, self._actor = self._actor, actor
            try:
                self._bump("archive", self.dag.title)
                self._emit("plan:archived", {})
            finally:
                self._actor = previous

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"plan": self.dag.to_dict(), "seq": self.seq, "rev": self.rev, "layers": self.dag.layers(),
                    "card": self.card.to_dict() if self.card else None, "profile": self.profile.to_dict(),
                    "review_due": self.dag.target_type == "constant" and self.profile.review_due(), "alignment": self.alignment,
                    "history": list(self.history)[-HISTORY_SHOWN:],
                    "proposals": [p.to_dict() for p in self.proposals.values() if p.status == "pending"],
                    "offers": [o for o in self.offers.values() if o["status"] == "offered"],
                    "ready": self.dag.ready_nodes()}

    # -- events ----------------------------------------------------------------------------------

    def handle(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Applies one event. Returns {"ok": True, ...} or {"ok": False, "error": ...}; never raises for bad input."""
        kind = event.get("type") if isinstance(event, dict) else None
        handlers = {
            "plan:get": self._get, "plan:update_node": self._update_node, "plan:mark": self._mark,
            "plan:rewind": self._rewind, "plan:rewind_impact": self._rewind_impact, "plan:note_effect": self._note_effect, "plan:propose_diff": self._propose, "plan:approve": self._approve,
            "plan:reject": self._reject, "plan:human_fallback": self._offer_fallback,
            "plan:fallback_response": self._answer_fallback, "plan:update_meta": self._update_meta, "plan:event": self._feed,
        }
        handler = handlers.get(kind)
        if handler is None:
            return {"ok": False, "error": f"unknown event type {kind!r}; expected one of {sorted(handlers)}"}
        actor = event.get("actor", "user")
        if actor not in ACTORS:
            return {"ok": False, "error": f"actor must be one of {', '.join(ACTORS)}"}
        try:
            with self._lock:
                stale = self._stale(kind, event)
                if stale:
                    return stale
                previous, self._actor = self._actor, actor
                try:
                    return {"ok": True, **handler(event)}
                finally:
                    self._actor = previous
        except KeyError as exc:
            return {"ok": False, "error": f"not found: {exc.args[0] if exc.args else exc}"}
        except (PlanError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def _stale(self, kind: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """A direct change made on an old reading of the plan is refused, so it cannot overwrite a newer one."""
        base = event.get("base_rev")
        if base is None or kind not in DIRECT:
            return None
        if isinstance(base, bool) or not isinstance(base, int):
            return {"ok": False, "error": "base_rev must be a whole number"}
        if base != self.rev:
            return {"ok": False, "stale": True, "rev": self.rev,
                    "error": f"the plan changed since you read it (now at revision {self.rev}, yours was {base}); read it again"}
        return None

    def _get(self, e: Dict[str, Any]) -> Dict[str, Any]:
        return self.snapshot()

    def _update_node(self, e: Dict[str, Any]) -> Dict[str, Any]:
        node = self.dag.update_node(e["node_id"], **(e.get("changes") or {}))
        self._bump("update_node", node.title)
        self._emit("plan:node_updated", {"node": node_to_dict(node)})
        return {"node": node_to_dict(node), "ready": self.dag.ready_nodes()}

    def _mark(self, e: Dict[str, Any]) -> Dict[str, Any]:
        retry = self.dag.mark(e["node_id"], e["status"], e.get("outputs"))
        node = self.dag.nodes[e["node_id"]]
        self._bump("mark", f"{node.title}: {node.status}" + (f", try {retry['attempt']} of {retry['of']} failed, tries again" if retry else ""))
        self._emit("plan:graph", {"seq_graph": True, **({"retry": {"node_id": node.id, **retry}} if retry else {})})
        return {"ready": self.dag.ready_nodes(), "shared_state": self.dag.shared_state, "status": node.status, **({"retry": retry} if retry else {})}

    def _feed(self, e: Dict[str, Any]) -> Dict[str, Any]:
        """An event from the world ({"text", "source", "at"}) offered to the plan's sensors; the ones it satisfies resolve."""
        if not isinstance(e.get("text"), str) or not e["text"].strip():
            raise ValueError("an event needs a text")
        resolved = self.dag.feed_event(e)
        if resolved:
            self._bump("sensor", ", ".join(self.dag.nodes[i].title for i in resolved))
            self._emit("plan:graph", {"resolved": resolved})
        return {"resolved": resolved, "ready": self.dag.ready_nodes()}

    def _rewind(self, e: Dict[str, Any]) -> Dict[str, Any]:
        from harness.rewind import plan_impact                 # imported here: the plan does not need the checkpoints
        impact = plan_impact(self.dag, e["node_id"])
        ids = self.dag.rewind(e["node_id"], e.get("accept_irreversible") is True)
        self._bump("rewind", self.dag.nodes[e["node_id"]].title)
        self._emit("plan:graph", {"rewound": ids})
        return {"rewound": ids, "not_undone": impact["not_undone"]}

    def _rewind_impact(self, e: Dict[str, Any]) -> Dict[str, Any]:
        from harness.rewind import plan_impact
        return plan_impact(self.dag, e["node_id"])

    def _note_effect(self, e: Dict[str, Any]) -> Dict[str, Any]:
        from harness.rewind import note_plan_effect
        entry = note_plan_effect(self.dag, e["node_id"], e.get("tool", ""), e.get("effect", ""), e.get("what", ""))
        if entry:
            self._bump("update_node", f"{self.dag.nodes[e['node_id']].title}: {entry['what']}")
            self._emit("plan:node_updated", {"node": node_to_dict(self.dag.nodes[e["node_id"]])})
        return {"effect": entry}

    def _propose(self, e: Dict[str, Any]) -> Dict[str, Any]:
        diff = e.get("diff")
        if not isinstance(diff, dict):
            raise ValueError("a proposal needs a diff object")
        self.dag.preview_diff(diff)                      # refuse at once what could never apply
        proposal = Proposal(f"pr_{secrets.token_hex(4)}", str(e.get("title") or "Proposed change"),
                            str(e.get("rationale", "")), diff, source=str(e.get("source", "model")))
        self.proposals[proposal.proposal_id] = proposal
        self._emit("plan:diff_proposed", {"proposal": proposal.to_dict()})
        return {"proposal_id": proposal.proposal_id, "changes": classify_diff(diff)}

    def _pending(self, e: Dict[str, Any]) -> Proposal:
        proposal = self.proposals.get(e["proposal_id"])
        if proposal is None:
            raise KeyError(e["proposal_id"])
        if proposal.status != "pending":
            raise ValueError(f"proposal {proposal.proposal_id} is already {proposal.status}")
        return proposal

    def _approve(self, e: Dict[str, Any]) -> Dict[str, Any]:
        proposal = self._pending(e)
        try:
            changed = self.dag.apply_diff(proposal.diff)
        except PlanError as exc:
            proposal.status = "stale"
            self._emit("plan:diff_stale", {"proposal_id": proposal.proposal_id})
            raise PlanError(f"the plan changed since this was proposed: {exc}") from None
        proposal.status = "applied"
        self._bump("apply", f"{proposal.title} ({proposal.source})")
        self._emit("plan:diff_applied", {"proposal_id": proposal.proposal_id, "changed": changed,
                                         "changes": classify_diff(proposal.diff)})
        return {"changed": changed}

    def _reject(self, e: Dict[str, Any]) -> Dict[str, Any]:
        proposal = self._pending(e)
        proposal.status = "rejected"
        self._emit("plan:diff_rejected", {"proposal_id": proposal.proposal_id})
        return {}

    def _update_meta(self, e: Dict[str, Any]) -> Dict[str, Any]:
        """Title, kind (constant or occasional) and card of the plan; all or nothing."""
        changes = e.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("send the changes to make: title, target_type or card")
        unknown = sorted(k for k in changes if k not in ("title", "target_type", "card", "profile"))
        if unknown:
            raise ValueError(f"only title, target_type, card and profile can be edited here, not: {', '.join(unknown)}")
        title, target, card, profile = self.dag.title, self.dag.target_type, self.card, self.profile
        if "title" in changes:
            title = changes["title"].strip() if isinstance(changes["title"], str) else ""
            if not title or len(title) > TITLE_CHARS:
                raise ValueError(f"the title is 1-{TITLE_CHARS} characters")
        if "target_type" in changes:
            target = changes["target_type"]
            if target not in TARGET_TYPES:
                raise ValueError("target_type must be 'constant' or 'occasional'")
        if "card" in changes:
            card = PlanInsightCard.from_dict(changes["card"])
        if "profile" in changes:
            profile = PlanProfile.from_dict(changes["profile"])
        self.dag.title, self.dag.target_type, self.card, self.profile = title, target, card, profile
        self.dag._touch()
        self._bump("update_meta", ", ".join(sorted(changes)))
        self._emit("plan:meta_updated", {"title": title, "target_type": target})
        return {"title": title, "target_type": target, "card": card.to_dict() if card else None, "profile": profile.to_dict()}

    # -- tasks the software cannot do ------------------------------------------------------------

    def _offer_fallback(self, e: Dict[str, Any]) -> Dict[str, Any]:
        node_id = e["node_id"]
        if node_id not in self.dag.nodes:
            raise KeyError(node_id)
        offer = {"offer_id": f"of_{secrets.token_hex(4)}", "node_id": node_id, "reason": str(e.get("reason", "")),
                 "alternative": str(e.get("offer", "")), "status": "offered"}
        self.offers[offer["offer_id"]] = offer
        self._emit("plan:fallback_offered", {"offer": offer})
        return {"offer": offer}

    def _answer_fallback(self, e: Dict[str, Any]) -> Dict[str, Any]:
        offer = self.offers.get(e["offer_id"])
        if offer is None or offer["status"] != "offered":
            raise KeyError(e["offer_id"])
        if e.get("accept_alternative") and offer["alternative"]:
            self.dag.update_node(offer["node_id"], description=offer["alternative"], assignee="agent")
            offer["status"] = "alternative_accepted"
        else:                                          # declined or insisted: the user's word is final
            self.dag.reassign_to_user(offer["node_id"], offer["reason"])
            offer["status"] = "assigned_to_user"
        self._bump("fallback", self.dag.nodes[offer["node_id"]].title)
        self._emit("plan:fallback_resolved", {"offer": offer, "node": node_to_dict(self.dag.nodes[offer["node_id"]])})
        return {"offer": offer, "ready": self.dag.ready_nodes()}

    # -- System 2 --------------------------------------------------------------------------------

    def replan(self, triage: TriageResult, events: List[str]) -> Dict[str, Any]:
        """Wakes the local model about a flagged batch. Its alternatives become proposals; human-fallback
        offers wait for the user. Raises if no replanner or card is set."""
        if self.replanner is None or self.card is None:
            raise ValueError("re-planning needs a model client and the plan card")
        result: ReplanProposal = self.replanner.propose(self.dag, self.card, triage, events)
        ids = []
        with self._lock:
            for i, alt in enumerate(result.alternatives):
                reply = self.handle({"type": "plan:propose_diff", "title": alt.title, "diff": alt.diff, "actor": "planner",
                                     "rationale": alt.description + (" (recommended)" if i == result.recommended else "")})
                if reply["ok"]:
                    ids.append(reply["proposal_id"])
            for fb in result.human_fallbacks:
                self.handle({"type": "plan:human_fallback", "actor": "planner", **fb})
            self._emit("plan:replan_ready", {"diagnosis": result.diagnosis, "affected_nodes": result.affected_nodes,
                                             "proposals": ids, "rejected": result.rejected})
        return {"diagnosis": result.diagnosis, "proposals": ids, "rejected": result.rejected,
                "human_fallbacks": result.human_fallbacks}

    # -- saving ----------------------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {"plan": self.dag.to_dict(), "card": self.card.to_dict() if self.card else None, "profile": self.profile.to_dict(), "alignment": self.alignment,
                "proposals": {k: {**v.to_dict()} for k, v in self.proposals.items()}, "offers": self.offers, "seq": self.seq,
                "rev": self.rev, "history": list(self.history)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any], replanner: Optional[Replanner] = None) -> "PlanCanvasBridge":
        bridge = cls(PlanDAG.from_dict(data["plan"]), PlanInsightCard.from_dict(data["card"]) if data.get("card") else None,
                     replanner, PlanProfile.from_dict(data.get("profile")))
        bridge.alignment = data.get("alignment") if isinstance(data.get("alignment"), dict) else {}
        for k, v in (data.get("proposals") or {}).items():
            bridge.proposals[k] = Proposal(k, v["title"], v.get("rationale", ""), v["diff"], v.get("source", "model"),
                                           v.get("status", "pending"), v.get("created_at", time.time()))
        bridge.offers = data.get("offers") or {}
        bridge.seq = int(data.get("seq", 0))
        bridge.rev = int(data.get("rev", 0))
        bridge.history.extend(h for h in (data.get("history") or []) if isinstance(h, dict))
        return bridge


class PlanStore:
    """Plans saved as JSON files in one folder, written atomically."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._bridges: Dict[str, PlanCanvasBridge] = {}
        self._lock = threading.RLock()

    def _path(self, plan_id: str) -> Path:
        if not PLAN_ID.match(plan_id):
            raise ValueError("a plan id is 1-64 letters, digits, - or _")
        return self.dir / f"{plan_id}.json"

    def _archived_path(self, plan_id: str) -> Path:
        self._path(plan_id)                                # the same check of the id
        return self.dir / "archive" / f"{plan_id}.json"

    def create(self, dag: PlanDAG, card: Optional[PlanInsightCard] = None, replanner: Optional[Replanner] = None,
               profile: Optional[PlanProfile] = None) -> PlanCanvasBridge:
        with self._lock:
            if dag.plan_id in RESERVED_IDS:
                raise ValueError(f"'{dag.plan_id}' is kept for the server's own routes: choose another plan id")
            if self._path(dag.plan_id).exists() or dag.plan_id in self._bridges:
                raise ValueError(f"plan '{dag.plan_id}' already exists")
            if self._archived_path(dag.plan_id).exists():
                raise ValueError(f"plan '{dag.plan_id}' is in the archive; restore it or choose another id")
            dag.validate_acyclic()
            bridge = PlanCanvasBridge(dag, card, replanner, profile)
            self._bridges[dag.plan_id] = bridge
            self.save(dag.plan_id)
            return bridge

    def get(self, plan_id: str, replanner: Optional[Replanner] = None) -> PlanCanvasBridge:
        with self._lock:
            path = self._path(plan_id)
            if plan_id not in self._bridges:
                if not path.is_file():
                    raise KeyError(plan_id)
                self._bridges[plan_id] = PlanCanvasBridge.from_dict(json.loads(path.read_text(encoding="utf-8")), replanner)
            bridge = self._bridges[plan_id]
            if replanner is not None:
                bridge.replanner = replanner
            return bridge

    def save(self, plan_id: str) -> None:
        with self._lock:
            path = self._path(plan_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._bridges[plan_id].to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)

    @staticmethod
    def _summaries(folder: Path) -> List[Dict[str, Any]]:
        out = []
        for p in sorted(folder.glob("*.json")):
            try:
                plan = json.loads(p.read_text(encoding="utf-8"))["plan"]
                nodes = plan.get("nodes", {})
                out.append({"plan_id": plan["plan_id"], "title": plan["title"], "target_type": plan.get("target_type", "occasional"),
                            "updated_at": plan.get("updated_at"),
                            "nodes": len(nodes),
                            "completed": sum(1 for n in nodes.values() if n.get("status") == "completed")})
            except (OSError, ValueError, KeyError):
                continue
        return out

    def list(self) -> List[Dict[str, Any]]:
        return self._summaries(self.dir)

    def list_archived(self) -> List[Dict[str, Any]]:
        return self._summaries(self.dir / "archive")

    def archive(self, plan_id: str) -> None:
        """Takes a plan out of the list without erasing it; restore() brings it back."""
        with self._lock:
            path = self._path(plan_id)
            if not path.is_file():
                raise KeyError(plan_id)
            dest = self._archived_path(plan_id)
            if dest.exists():
                raise ValueError(f"a plan with the id '{plan_id}' is already in the archive")
            if plan_id in self._bridges:
                self.save(plan_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)
            self._bridges.pop(plan_id, None)

    def restore(self, plan_id: str) -> None:
        with self._lock:
            dest = self._archived_path(plan_id)
            if not dest.is_file():
                raise KeyError(plan_id)
            if self._path(plan_id).exists():
                raise ValueError(f"plan '{plan_id}' already exists; archive or rename it first")
            os.replace(dest, self._path(plan_id))

    def delete(self, plan_id: str) -> bool:
        with self._lock:
            self._bridges.pop(plan_id, None)
            path = self._path(plan_id)
            if path.is_file():
                path.unlink()
                return True
            return False
