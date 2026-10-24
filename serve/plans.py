"""The server's side of the planner: stored plans, the user's planner settings and the triage entry point.

The planner itself lives in harness/planner and is imported only when used, so the server starts
without it. The Jev API key is never read from a request or a setting: it comes from the
TYPESAFE_API_KEY environment variable of the server process, and using it needs the user's explicit
cloud-triage consent saved here.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULTS = {"cloud_triage": False, "pseudonymize": True, "card_fields": None}
EVENTS_MAX = 100
EVENT_CHARS = 2000


class PlannerUnavailable(RuntimeError):
    """The planner package is not installed."""


def _planner():
    try:
        import harness.planner as planner
    except ImportError as exc:                       # pragma: no cover - the harness ships with the server
        raise PlannerUnavailable("the planner (harness/planner) is not installed") from exc
    return planner


class PlanService:
    def __init__(self, data_dir: str | Path, client_factory: Optional[Callable[[], Any]] = None) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory
        self._store = None
        self._settings_path = self.dir / "planner.json"
        self._lock = threading.RLock()

    @property
    def store(self):
        with self._lock:
            if self._store is None:
                self._store = _planner().PlanStore(self.dir / "plans")
            return self._store

    # -- settings --------------------------------------------------------------------------------

    def settings(self) -> Dict[str, Any]:
        try:
            saved = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        merged = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
        return {**merged, "key_present": bool(os.environ.get("TYPESAFE_API_KEY"))}

    def update_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("send a JSON object")
        unknown = [k for k in changes if k not in DEFAULTS]
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(unknown)}")
        current = {k: v for k, v in self.settings().items() if k in DEFAULTS}
        for key in ("cloud_triage", "pseudonymize"):
            if key in changes:
                if not isinstance(changes[key], bool):
                    raise ValueError(f"{key} must be true or false")
                current[key] = changes[key]
        if "card_fields" in changes:
            fields = changes["card_fields"]
            allowed = _planner().card.CARD_FIELDS
            if fields is not None and (not isinstance(fields, list) or any(f not in allowed for f in fields)):
                raise ValueError(f"card_fields must be null or a list of: {', '.join(allowed)}")
            current["card_fields"] = fields
        tmp = self._settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current), encoding="utf-8")
        os.replace(tmp, self._settings_path)
        return self.settings()

    def gatekeeper(self):
        s = self.settings()
        return _planner().JevTriageGatekeeper.from_environment(
            cloud_consent=bool(s["cloud_triage"]), pseudonymize=bool(s["pseudonymize"]), card_fields=s["card_fields"])

    # -- plans -----------------------------------------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        return self.store.list()

    def create(self, body: Dict[str, Any]) -> Dict[str, Any]:
        p = _planner()
        if not isinstance(body, dict):
            raise ValueError("send a JSON object")
        plan = body.get("plan", body)
        dag = p.PlanDAG.from_dict(plan) if plan.get("nodes") is not None and plan.get("plan_id") else p.PlanDAG(
            plan_id=str(plan.get("plan_id") or ""), title=str(plan.get("title") or ""),
            target_type=plan.get("target_type", "occasional"))
        if not dag.plan_id or not dag.title:
            raise ValueError("a plan needs a plan_id and a title")
        card = p.PlanInsightCard.from_dict(body["card"]) if body.get("card") else None
        bridge = self.store.create(dag, card, self._replanner())
        return bridge.snapshot()

    def _replanner(self):
        if self.client_factory is None:
            return None
        return _planner().Replanner(self.client_factory())

    def snapshot(self, plan_id: str) -> Dict[str, Any]:
        return self.store.get(plan_id, self._replanner()).snapshot()

    def delete(self, plan_id: str) -> bool:
        return self.store.delete(plan_id)

    def handle_event(self, plan_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        bridge = self.store.get(plan_id, self._replanner())
        result = bridge.handle(event)
        if result.get("ok"):
            self.store.save(plan_id)
        return result

    def events_since(self, plan_id: str, seq: int) -> List[Dict[str, Any]]:
        return self.store.get(plan_id).events_since(seq)

    def bridge(self, plan_id: str):
        return self.store.get(plan_id, self._replanner())

    def triage(self, plan_id: str, events: List[Any], replan: bool = False) -> Dict[str, Any]:
        """Judges incoming events against the plan; when the filter says wake and `replan` is true, the local
        model re-plans in the background and the outcome arrives as events on the plan's stream."""
        if not isinstance(events, list) or not events or len(events) > EVENTS_MAX:
            raise ValueError(f"send between 1 and {EVENTS_MAX} events as a list of strings")
        texts = [str(e)[:EVENT_CHARS] for e in events if isinstance(e, (str, int, float))]
        bridge = self.bridge(plan_id)
        if bridge.card is None:
            raise ValueError("this plan has no card to judge events against")
        gatekeeper = self.gatekeeper()
        result = gatekeeper.triage_event_batch(bridge.card, texts)
        out = {"wake_system_2": result.wake_system_2, "has_blocker_prob": result.has_blocker_prob,
               "impact_scope": result.impact_scope, "max_impact": result.max_impact, "source": result.source,
               "sent_to_cloud": result.sent_to_cloud, "reasons": result.reasons, "latency_ms": result.latency_ms,
               "token_usage": result.token_usage, "replanning": False}
        bridge._emit("plan:triage", {"result": out})
        if result.wake_system_2 and replan:
            if bridge.replanner is None:
                out["replan_error"] = "no model is connected for re-planning"
            else:
                out["replanning"] = True
                threading.Thread(target=self._replan, args=(plan_id, bridge, result, texts), daemon=True,
                                 name=f"replan-{plan_id}").start()
        return out

    def _replan(self, plan_id: str, bridge: Any, result: Any, events: List[str]) -> None:
        try:
            bridge.replan(result, events)
            self.store.save(plan_id)
        except Exception as exc:
            bridge._emit("plan:replan_failed", {"error": f"{type(exc).__name__}: {exc}"})
