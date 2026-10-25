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
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULTS = {"cloud_triage": False, "pseudonymize": True, "card_fields": None, "gate_mode": "or"}
EVENTS_MAX = 100
EVENT_CHARS = 2000


def _short(c: Dict[str, Any]) -> Dict[str, Any]:
    return {k: c[k] for k in ("kind", "severity", "plan_id", "plan_title", "with_plan_id", "with_title", "text", "data")}


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
        self._gate = None                                  # the one gatekeeper of this process: its telemetry adds up
        self._replanning: Dict[str, float] = {}            # plans the local model is re-planning now, and since when

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
        card = _planner().card
        return {**merged, "key_present": bool(os.environ.get("TYPESAFE_API_KEY")) or self._jev_from_registry() is not None,
                "card_fields_default": list(card.DEFAULT_SHARED_FIELDS), "card_fields_personal": list(card.PERSONAL_FIELDS)}

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
        if "gate_mode" in changes:
            if changes["gate_mode"] not in _planner().triage.GATE_MODES:
                raise ValueError(f"gate_mode must be one of: {', '.join(_planner().triage.GATE_MODES)}")
            current["gate_mode"] = changes["gate_mode"]
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

    registry = None                                        # Basalt's providers (harness.llm): set by the server

    def gatekeeper(self):
        """The process's gatekeeper, set to the settings and the key as they are now. It is the same object every time, so
        its telemetry counts every judgement made since the server started."""
        s = self.settings()
        planner = _planner()
        found = self._jev_from_registry()
        with self._lock:
            if self._gate is None:
                self._gate = planner.JevTriageGatekeeper()
            key, endpoint, model = found if found else (os.environ.get("TYPESAFE_API_KEY") or None, planner.triage.DEFAULT_ENDPOINT, "jev-latest")
            self._gate.reconfigure(api_key=key, endpoint=endpoint, model=model, cloud_consent=bool(s["cloud_triage"]),
                                   pseudonymize=bool(s["pseudonymize"]), card_fields=s["card_fields"], gate_mode=s["gate_mode"])
            return self._gate

    def telemetry(self) -> Dict[str, Any]:
        """What the filter has done since the server started: judgements, where they were answered (the cloud, the local filter by
        choice, the local filter because the cloud failed), tokens, and which plans are being re-planned."""
        return {"triage": self.gatekeeper().telemetry.summary(), "replanning": sorted(self._replanning)}

    def _jev_from_registry(self):
        """(key, address, model) of the Jev provider the person pointed the triage at, when the registry allows it."""
        try:
            from harness import llm
            reg = self.registry if self.registry is not None else llm.registry()
        except Exception:
            return None
        try:
            data = reg.load()
            pid = (data.get("assignments") or {}).get("triage")
            prov = reg._find(data, pid) if pid else None
            if not prov or prov.get("type") != "jev" or not reg.check("triage", pid)[0] or not prov.get("api_key"):
                return None
            return prov["api_key"], (prov.get("base_url") or "https://api.typesafe.ai/v1").rstrip("/") + "/systemone", prov.get("model") or "jev-latest"
        except Exception:
            return None

    # -- plans -----------------------------------------------------------------------------------

    def list(self, archived: bool = False) -> List[Dict[str, Any]]:
        """The plans, each with what it is (long-term or not), whether a long-term one is due to be looked at, and the conflicts the dates and
        amounts show between it and the others."""
        if archived:
            return self.store.list_archived()
        views, due = self._views()
        found = _planner().alignment.rule_conflicts(views)
        out = []
        for row in self.store.list():
            mine = [c for c in found if row["plan_id"] in (c["plan_id"], c["with_plan_id"])]
            out.append({**row, "review_due": row["plan_id"] in due, "conflicts": [_short(c) for c in mine]})
        return out

    def _views(self):
        """Every plan as the comparison reads it, and which long-term ones are due to be looked at."""
        planner, views, due = _planner(), [], set()
        for row in self.store.list():
            try:
                bridge = self.store.get(row["plan_id"])
            except Exception:
                continue
            views.append(planner.alignment.view(bridge.dag.to_dict(), bridge.profile.to_dict(), bridge.card.to_dict() if bridge.card else None))
            if bridge.dag.target_type == "constant" and bridge.profile.review_due():
                due.add(row["plan_id"])
        return views, due

    def alignment(self, plan_id: str, model: bool = False) -> Dict[str, Any]:
        """How this plan sits with the others: the conflicts the dates and amounts show, and, when asked, the local model's reading of what the plans
        mean together (kept with the plan until it changes, and said to be out of date after that)."""
        planner = _planner()
        views, _ = self._views()
        subject = next((v for v in views if v["plan_id"] == plan_id), None)
        if subject is None:
            raise KeyError(plan_id)
        bridge = self.bridge(plan_id)
        if model:
            if self.client_factory is None:
                raise ValueError("no model is connected to read the plans")
            found = planner.alignment.meaning_conflicts(subject, views, planner.alignment.client_ask(self.client_factory()))
            bridge.alignment = {"checked_at": time.time(), "checked_rev": bridge.rev, "conflicts": found}
            self.store.save(plan_id)
            bridge._emit("plan:alignment", {"checks": len(found)})
        kept = bridge.alignment or {}
        rules = [c for c in planner.alignment.rule_conflicts(views) if plan_id in (c["plan_id"], c["with_plan_id"])]
        return {"plan_id": plan_id, "conflicts": rules, "checks": kept.get("conflicts", []), "checked_at": kept.get("checked_at"),
                "out_of_date": bool(kept) and kept.get("checked_rev") != bridge.rev}

    def _announce(self, plan_id: str) -> None:
        """After a plan is made or its dates, cost or kind change: if the arithmetic now shows a conflict, the plan's viewers are told."""
        try:
            conflicts = self.alignment(plan_id)["conflicts"]
            if conflicts:
                self.store.get(plan_id)._emit("plan:alignment", {"conflicts": [_short(c) for c in conflicts]})
        except Exception:
            pass                                             # telling is never worth failing the change that was asked for

    @staticmethod
    def _actor(body: Any) -> str:
        """Who is asking, as the sender says: a note on the history, not a proof of identity."""
        actor = body.get("actor", "user") if isinstance(body, dict) else "user"
        if actor not in _planner().bridge.ACTORS:
            raise ValueError(f"actor must be one of {', '.join(_planner().bridge.ACTORS)}")
        return actor

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
        profile = p.PlanProfile.from_dict(body.get("profile"))
        actor = self._actor(body)
        bridge = self.store.create(dag, card, self._replanner(), profile)
        bridge.record(actor, "create", dag.title)
        self.store.save(dag.plan_id)
        self._announce(dag.plan_id)
        return bridge.snapshot()

    def _replanner(self):
        if self.client_factory is None:
            return None
        return _planner().Replanner(self.client_factory())

    def snapshot(self, plan_id: str) -> Dict[str, Any]:
        snap = self.store.get(plan_id, self._replanner()).snapshot()
        snap["replanning"] = plan_id in self._replanning
        return snap

    def archive(self, plan_id: str, actor: str = "user") -> Dict[str, Any]:
        """Takes a plan out of the list, keeping it so it can be restored."""
        bridge = self.store.get(plan_id)
        bridge.announce_archived(actor)
        self.store.archive(plan_id)
        return {"archived": plan_id, "title": bridge.dag.title}

    def restore(self, plan_id: str, actor: str = "user") -> Dict[str, Any]:
        self.store.restore(plan_id)
        bridge = self.store.get(plan_id, self._replanner())
        bridge.record(actor, "restore", bridge.dag.title)
        self.store.save(plan_id)
        return bridge.snapshot()

    def delete(self, plan_id: str) -> bool:
        return self.store.delete(plan_id)

    def handle_event(self, plan_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        bridge = self.store.get(plan_id, self._replanner())
        result = bridge.handle(event)
        if result.get("ok"):
            self.store.save(plan_id)
            if event.get("type") == "plan:update_meta":
                self._announce(plan_id)
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
        out["replan_state"] = "not_asked" if not replan else "not_needed" if not result.wake_system_2 else None
        if out["replan_state"] is None:
            out["replan_state"] = self.start_replan(plan_id, bridge, result, texts)
            out["replanning"] = out["replan_state"] in ("started", "in_progress")
            if out["replan_state"] == "unavailable":
                out["replan_error"] = "no model is connected for re-planning"
        bridge._emit("plan:triage", {"result": out})
        return out

    def start_replan(self, plan_id: str, bridge: Any, result: Any, events: List[str]) -> str:
        """Starts the local model re-planning in the background, one plan at a time: "started", "in_progress" when it is already
        at work on this plan (nothing new is started, so no duplicate proposals), or "unavailable" without a model."""
        if bridge.replanner is None:
            return "unavailable"
        with self._lock:
            if plan_id in self._replanning:
                return "in_progress"
            self._replanning[plan_id] = time.time()
        try:
            threading.Thread(target=self._replan, args=(plan_id, bridge, result, events), daemon=True, name=f"replan-{plan_id}").start()
        except BaseException:
            with self._lock:
                self._replanning.pop(plan_id, None)
            raise
        return "started"

    def _replan(self, plan_id: str, bridge: Any, result: Any, events: List[str]) -> None:
        try:
            bridge.replan(result, events)
            self.store.save(plan_id)
        except Exception as exc:
            bridge._emit("plan:replan_failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            with self._lock:
                self._replanning.pop(plan_id, None)
