"""The server's side of the planner: stored plans, the user's planner settings and the triage entry point.

The planner itself lives in tadamoo/planner and is imported only when used, so the server starts
without it. The Jev API key is not kept among these settings: it comes from the TYPESAFE_API_KEY
environment variable of the server process or from the Jev provider of the providers registry (where
`update_settings` saves it, in the credential store), and using it needs the user's explicit
cloud-triage consent saved here.
"""
from __future__ import annotations

import atexit
import copy
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Deque, Dict, List, Optional

DEFAULTS = {"cloud_triage": False, "pseudonymize": True, "card_fields": None, "gate_mode": "or", "event_pump": True, "auto_apply": True, "sources": [],
            "blocker_threshold": 0.8, "cloud_timeout": 5.0, "max_events": 20}
KEY_FIELDS = ("jev_key", "clear_jev_key")                 # acts on the key, which is not stored among the settings
EVENTS_MAX = 100
RESULT_KINDS = ("mark", "sensor", "timeout", "fallback", "no_route", "auto")      # the changes of a plan that are something coming out of it, not editing it
RESUME_LIMIT = 3                                         # times in a row a plan is taken up again after the program stopped without a step more being done
RECOVER_LIMIT = 3                                        # recovery runs for one failed step before it is left for the person


def _short(c: Dict[str, Any]) -> Dict[str, Any]:
    return {k: c[k] for k in ("kind", "severity", "plan_id", "plan_title", "with_plan_id", "with_title", "text", "data")}


class PlannerUnavailable(RuntimeError):
    """The planner package is not installed."""


def _planner():
    try:
        import tadamoo.planner as planner
    except ImportError as exc:                       # pragma: no cover - the harness ships with the server
        raise PlannerUnavailable("the planner (tadamoo/planner) is not installed") from exc
    return planner


class PlanService:
    def __init__(self, data_dir: str | Path, client_factory: Optional[Callable[[], Any]] = None, vault_factory: Optional[Callable[[], Any]] = None,
                 pump_batch: int = 10, pump_window: float = 300.0, pump_poll: float = 1.0) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        from tadamoo.planner import nodes as plan_nodes
        plan_nodes.GLOBALS = plan_nodes.GlobalValues(self.dir / "globals.json")      # the values every plan can read and set
        self.client_factory = client_factory
        self.vault_factory = vault_factory                 # the vault the sources' credentials are read from; the server's own by default
        self.pump_batch, self.pump_window, self.pump_poll = pump_batch, pump_window, pump_poll
        self._inbox = None                                 # events waiting to be judged, per plan; exists while the event pump is on
        self._pump = None
        self._runner = None
        self._booted = False
        self._sync_lock = threading.RLock()                # one start or stop of the pump and the sources at a time
        self._events_in = 0                                # events the mailbox has taken since the server started
        self._store = None
        self._due: Dict[str, Optional[float]] = {}             # per plan, when a step it works out by itself next comes due (None: nothing is waiting)
        self._due_scanned = False
        self._judgments: Dict[str, Deque[float]] = {}          # per plan, when a wait in words was last judged (the hourly cap)
        self._degraded: Dict[str, str] = {}                    # per plan, why a wait in words is not being judged: no_judge or rate_limit
        self._pool: Optional[ThreadPoolExecutor] = None
        self.launcher: Optional[Callable[[str, str, str], Any]] = None      # (plan_id, name, text): starts an Automatic conversation of the agent that carries the plan on
        self.run_active: Optional[Callable[[str], bool]] = None             # plan_id -> whether such a conversation is at work on it now
        self._starts: Dict[str, List[Dict[str, Any]]] = {}      # per plan, its interval starts and when each is next due
        self._offered: Dict[str, set] = {}                     # per plan, the steps for the agent that a conversation has been started for
        self._human_loaded = False
        self._resumed = False
        self.judge_runner: Callable[[Callable[[], None]], Any] = self._submit      # runs a judgement off the clock's thread (a test runs it in place)
        self._attention: Dict[str, Any] = {"t": 0.0}           # the latest result of a plan that was not the person's own edit
        self._settings_path = self.dir / "planner.json"
        self._lock = threading.RLock()
        self._gate = None                                  # the one gatekeeper of this process: its telemetry adds up
        self._replanning: Dict[str, float] = {}            # plans the local model is re-planning now, and since when

    @property
    def store(self):
        with self._lock:
            if self._store is None:
                self._store = _planner().PlanStore(self.dir / "plans")
            store = self._store
        if not self._booted:                               # saved as "on", the pump and the sources start when the planner is first used
            with self._sync_lock:
                if not self._booted:
                    self.sync_events()
        return store

    def _saved(self, plan_id: str) -> None:
        self.store.save(plan_id)
        self._index(plan_id)

    def start(self) -> None:
        """Brings the pump and the sources up as the settings say; the server calls it as it starts."""
        _ = self.store

    # -- settings --------------------------------------------------------------------------------

    def settings(self) -> Dict[str, Any]:
        try:
            saved = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        merged = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
        card = _planner().card
        usable = bool(os.environ.get("TYPESAFE_API_KEY")) or self._jev_from_registry() is not None
        return {**merged, "key_present": usable, "key_saved": usable or self._saved_jev_key(),
                "card_fields_default": list(card.DEFAULT_SHARED_FIELDS), "card_fields_personal": list(card.PERSONAL_FIELDS)}

    def update_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("send a JSON object")
        unknown = [k for k in changes if k not in DEFAULTS and k not in KEY_FIELDS]
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(unknown)}")
        current = {k: v for k, v in self.settings().items() if k in DEFAULTS}
        for key in ("cloud_triage", "pseudonymize", "auto_apply"):
            if key in changes:
                if not isinstance(changes[key], bool):
                    raise ValueError(f"{key} must be true or false")
                current[key] = changes[key]
        if "event_pump" in changes:
            if not isinstance(changes["event_pump"], bool):
                raise ValueError("event_pump must be true or false")
            current["event_pump"] = changes["event_pump"]
        if "sources" in changes:
            current["sources"] = _planner().sources.clean_sources(changes["sources"])
        if "gate_mode" in changes:
            if changes["gate_mode"] not in _planner().triage.GATE_MODES:
                raise ValueError(f"gate_mode must be one of: {', '.join(_planner().triage.GATE_MODES)}")
            current["gate_mode"] = changes["gate_mode"]
        if "blocker_threshold" in changes:
            v = changes["blocker_threshold"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
                raise ValueError("blocker_threshold must be a number from 0 to 1")
            current["blocker_threshold"] = float(v)
        if "cloud_timeout" in changes:
            v = changes["cloud_timeout"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
                raise ValueError("cloud_timeout must be a number of seconds above 0")
            current["cloud_timeout"] = float(v)
        if "max_events" in changes:
            v = changes["max_events"]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError("max_events must be a whole number from 1")
            current["max_events"] = v
        if "jev_key" in changes and (not isinstance(changes["jev_key"], str) or not changes["jev_key"].strip()):
            raise ValueError("jev_key must be the key, as text")
        if "card_fields" in changes:
            fields = changes["card_fields"]
            allowed = _planner().card.CARD_FIELDS
            if fields is not None and (not isinstance(fields, list) or any(f not in allowed for f in fields)):
                raise ValueError(f"card_fields must be null or a list of: {', '.join(allowed)}")
            current["card_fields"] = fields
        tmp = self._settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current), encoding="utf-8")
        os.replace(tmp, self._settings_path)
        if changes.get("clear_jev_key") is True:
            self._clear_jev_key()
        if "jev_key" in changes:
            self._set_jev_key(changes["jev_key"])
        if "event_pump" in changes or "sources" in changes:
            self.sync_events()
        return self.settings()

    registry = None                                        # TaDaMoo's providers (tadamoo.llm): set by the server

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
                                   pseudonymize=bool(s["pseudonymize"]), card_fields=s["card_fields"], gate_mode=s["gate_mode"],
                                   blocker_threshold=s["blocker_threshold"], timeout=s["cloud_timeout"], max_events=s["max_events"])
            return self._gate

    def telemetry(self) -> Dict[str, Any]:
        """What the filter has done since the server started: judgements, where they were answered (the cloud, the local filter by
        choice, the local filter because the cloud failed), tokens, and which plans are being re-planned."""
        return {"triage": self.gatekeeper().telemetry.summary(), "replanning": sorted(self._replanning), "events": self.events_status()}

    def _providers(self):
        from tadamoo import llm
        return self.registry if self.registry is not None else llm.registry()

    def _saved_jev_key(self) -> bool:
        """Whether the providers registry holds a key for Jev, used or not."""
        try:
            return any(p.get("type") == "jev" and (p.get("api_key") or "").strip() for p in self._providers().load().get("providers", []))
        except Exception:
            return False

    def _set_jev_key(self, key: str) -> None:
        """Keeps the key where the providers registry keeps every key (the credential store) and, when the triage has no provider yet,
        points it at Jev. If the registry asks for the person's yes for this function first, the key stays saved and unused until it is given."""
        try:
            reg = self._providers()
            prov = reg.set_api_key_for("jev", key.strip())
            if not (reg.load().get("assignments") or {}).get("triage"):
                try:
                    reg.assign_checked("triage", prov["id"])
                except ValueError:
                    pass
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"the key could not be saved ({type(exc).__name__})") from None

    def _clear_jev_key(self) -> None:
        try:
            reg = self._providers()
            data = reg.load()
            refs = []
            for prov in data.get("providers", []):
                if prov.get("type") == "jev":
                    refs.append(prov.pop("key_ref", None))
                    prov["api_key"] = ""
            reg.save(data)
            for ref in filter(None, refs):
                reg.credentials.delete(ref)
        except Exception as exc:
            raise ValueError(f"the key could not be removed ({type(exc).__name__})") from None

    def _jev_from_registry(self):
        """(key, address, model) of the Jev provider the person pointed the triage at, when the registry allows it."""
        try:
            from tadamoo import llm
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
            self._saved(plan_id)
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
        created_by = str(body.get("created_by") or "")
        bridge = self.store.create(dag, card, self._replanner(), profile, created_by)
        bridge.record(actor, "create", dag.title)
        self.tick(dag.plan_id)
        self._saved(dag.plan_id)
        self._announce(dag.plan_id)
        return bridge.snapshot()

    def plan_changed(self, plan_id: str) -> None:
        """After the import service made or replaced a plan: the steps it works out by itself are worked out, and the viewers are told."""
        self.tick(plan_id)
        self._saved(plan_id)
        self._announce(plan_id)

    def _replanner(self):
        if self.client_factory is None:
            return None
        return _planner().Replanner(self.client_factory())

    def snapshot(self, plan_id: str) -> Dict[str, Any]:
        snap = self.store.get(plan_id, self._replanner()).snapshot()
        snap["replanning"] = plan_id in self._replanning
        snap["watch_degraded"] = self._degraded.get(plan_id, "")
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
        self._saved(plan_id)
        return bridge.snapshot()

    def delete(self, plan_id: str) -> bool:
        return self.store.delete(plan_id)

    def set_paused(self, plan_id: str, actor: str, value: bool) -> Dict[str, Any]:
        bridge = self.store.get(plan_id)
        out = bridge.set_paused(actor, value)
        self._saved(plan_id)
        if not value:
            self.tick(plan_id)                              # what came due while it was paused is worked out now
        return out

    def set_schedule(self, plan_id: str, actor: str, schedule: Dict[str, Any]) -> Dict[str, Any]:
        bridge = self.store.get(plan_id)
        out = bridge.set_schedule(actor, schedule)
        self._saved(plan_id)
        return out

    def remove_schedule(self, plan_id: str, actor: str) -> Dict[str, Any]:
        bridge = self.store.get(plan_id)
        out = bridge.clear_schedule(actor)
        self._saved(plan_id)
        return out

    def template_context(self, plan_id: str) -> Dict[str, Any]:
        """What a schedule's message template can use for this plan: its title, its first ready step, and its own shared values
        (so {bilancio} and the like are already there once something writes them)."""
        try:
            bridge = self.store.get(plan_id)
        except KeyError:
            return {}
        ready = bridge.dag.ready_nodes()
        return {"plan_title": bridge.dag.title, "ready_step": ready[0] if ready else "", **bridge.dag.shared_state}

    def is_paused(self, plan_id: str) -> bool:
        """Whether a plan a routine speaks for has been paused; False for a plan that is gone (its schedule should be removed too)."""
        try:
            return self.store.get(plan_id).paused
        except KeyError:
            return False

    def duplicate(self, plan_id: str, actor: str = "user", title: Any = None) -> Dict[str, Any]:
        """A copy of a plan under a new id: its steps, links, card and dates, with a history of its own."""
        bridge = self.store.get(plan_id)
        plan = copy.deepcopy(bridge.dag.to_dict())
        new_title = " ".join(str(title).split())[:200] if isinstance(title, str) and title.strip() else f"{plan['title']} (copy)"
        base = re.sub(r"_[0-9a-f]{4}$", "", plan_id)[:40]
        new_id = ""
        while not new_id or self.store.exists(new_id):
            new_id = f"{base}_{secrets.token_hex(2)}"
        card = bridge.card.to_dict() if bridge.card else None
        if card and card.get("titolo") == plan["title"]:
            card["titolo"] = new_title
        plan.update(plan_id=new_id, title=new_title)
        return self.create({"plan": plan, "card": card, "profile": bridge.profile.to_dict(), "actor": actor})

    def attention(self) -> Dict[str, Any]:
        """When a plan last moved by someone other than the person (a step done, a sensor read, a timeout), for the tab that tells them something new came out."""
        return dict(self._attention)

    def tick(self, plan_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """Lets a plan work out the steps it resolves by itself and keeps when the next one comes due; saved when something changed."""
        bridge = self.store.get(plan_id, self._replanner())
        report = bridge.tick(now)
        moment = time.time() if now is None else now
        self._due[plan_id] = report["next_due"]
        for nid in [*(report.get("failed") or []), *(report.get("timed_out") or [])]:       # a step the plan works out itself failed or a wait ran out of time
            self._recover(plan_id, nid, moment)
        for call in report.get("calls") or []:
            retry_at = self._call(plan_id, bridge, call, moment)
            if retry_at is not None:
                report["changed"] = True
                self._due[plan_id] = retry_at if self._due[plan_id] is None else min(self._due[plan_id], retry_at)
        self._judge_waits(plan_id, bridge, report, moment)
        if report["changed"]:
            self._saved(plan_id)
            if any(line["kind"] in ("output", "log") or "timed out" in line["text"] for line in report["lines"]):
                last = report["lines"][-1]
                self._attention = {"t": last["t"], "plan": plan_id, "title": bridge.dag.title, "summary": last["text"]}
        return report

    # -- starting plans ----------------------------------------------------------------------------------------

    def _index(self, plan_id: str, now: Optional[float] = None) -> None:
        """Keeps, for a plan, when each of its interval starts is next due: counted from the last time it fired, or from when it was first seen with this schedule."""
        from tadamoo.scheduler import RoutineError, Schedule
        moment = time.time() if now is None else now
        try:
            bridge = self.store.get(plan_id)
        except (KeyError, ValueError):
            self._starts.pop(plan_id, None)
            return
        rows, armed_now = [], False
        for n in bridge.dag.start_nodes():
            spec = n.spec
            if spec.get("launch") != "interval" or not spec.get("schedule") or n.state_variables.get("draft"):
                continue
            armed = n.state_variables.get("armed")
            if not isinstance(armed, dict) or armed.get("schedule") != spec["schedule"]:
                armed = n.state_variables["armed"] = {"schedule": spec["schedule"], "at": moment}
                n.state_variables.pop("last_fire", None)
                armed_now = True
            try:
                due = Schedule.parse(spec["schedule"]).next_after(max(float(n.state_variables.get("last_fire") or 0), float(armed["at"])))
            except RoutineError:
                continue
            rows.append({"node": n.id, "number": spec.get("number", 1), "schedule": spec["schedule"], "due": due})
        if rows:
            self._starts[plan_id] = rows
        else:
            self._starts.pop(plan_id, None)
        if armed_now:
            self.store.save(plan_id)

    def _fire_start(self, plan_id: str, row: Dict[str, Any], now: float) -> None:
        """An interval start has come due: the plan is started at it, unless it is running or paused (that time is skipped, and the next one is counted from now)."""
        try:
            bridge = self.store.get(plan_id, self._replanner())
            node = bridge.dag.nodes.get(row["node"])
            if node is None or node.spec.get("schedule") != row["schedule"]:
                self._index(plan_id, now)
                return
            node.state_variables["last_fire"] = now
            try:
                if bridge.paused:
                    raise ValueError("the plan is paused")
                self.start_plan(plan_id, row["number"], "its schedule", "system", now=now)
            except (_planner().PlanError, ValueError) as exc:
                bridge.dag.note(node.id, "auto", f"{node.title}: not started: {exc}", now)
            self._saved(plan_id)
        except Exception:                                           # a plan that cannot be read does not stop the others
            self._starts.pop(plan_id, None)

    def start_plan(self, plan_id: str, number: Any = 1, cause: str = "the person", actor: str = "user", caller: Optional[Dict[str, str]] = None,
                   now: Optional[float] = None) -> Dict[str, Any]:
        """Starts a plan at the start with this number. The steps the plan works out itself are worked out, and when some are for the agent a conversation is
        started to do them. Raises PlanError for a number the plan has not or a plan that is running."""
        bridge = self.store.get(plan_id, self._replanner())
        self._offered.pop(plan_id, None)
        out = bridge.begin(number, cause, actor, now, caller)
        self._saved(plan_id)
        self.tick(plan_id, now)
        self._offer(plan_id, f"it was started at start {number} by {cause}", now)
        return out

    def _call(self, plan_id: str, bridge: Any, call: Dict[str, Any], now: float) -> Optional[float]:
        """A step that starts another plan: the other plan is started and the step is done; when it cannot be, the reason is kept and the step tries again
        after its wait. Returns when it tries again, if it does."""
        try:
            if call["plan"] == plan_id:
                raise ValueError("a plan cannot start itself")
            self.start_plan(call["plan"], call["start"], f"the plan {plan_id}", "system", {"plan": plan_id, "node": call["node"]}, now)
        except KeyError:
            reason = f"there is no plan '{call['plan']}'"
        except (_planner().PlanError, ValueError) as exc:
            reason = str(exc)
        else:
            bridge.settle_call(call["node"], now, f"started {call['plan']} at start {call['start']}")
            self._saved(plan_id)
            return None
        retry = bridge.fail_call(call["node"], now, reason)
        self._saved(plan_id)
        node = bridge.dag.nodes.get(call["node"])
        if node is not None and node.status == "failed":
            self._recover(plan_id, call["node"], now)
        return retry["retry_at"] if retry else None

    # -- carrying a plan on with the agent -----------------------------------------------------------------------

    def _ready_for_agent(self, bridge: Any) -> set:
        dag = bridge.dag
        return {i for i in dag.ready_nodes() if dag.nodes[i].assignee.value == "agent" and dag.nodes[i].type.value in ("action", "decision")}

    def _run_text(self, bridge: Any, reason: str, ready: List[str]) -> str:
        dag = bridge.dag
        steps = "; ".join(f"{i} ({dag.nodes[i].title})" for i in ready[:10])
        return (f'Carry on the plan "{dag.title}" (id {dag.plan_id}); {reason}. Read it with plan_get first. Steps ready for you: {steps}. '
                "Do each one: plan_step_start, do the work with your tools, then plan_mark it completed with its outputs, or failed with the reason. "
                "Go on with whatever becomes ready. A step that asks the person something (a question or an approval) is answered by the person on their screen: leave it. "
                "Stop when nothing is ready for you.")

    def _offer(self, plan_id: str, reason: str, now: Optional[float] = None) -> bool:
        """Starts a conversation of the agent for the steps of a running plan that are ready for it and that no conversation was started for yet. Nothing
        is started while one is at work on the plan, or when no launcher is set. True when one was started."""
        if self.launcher is None:
            return False
        try:
            bridge = self.store.get(plan_id, self._replanner())
        except (KeyError, ValueError):
            return False
        if bridge.paused or not bridge.run.get("active"):
            return False
        ready = self._ready_for_agent(bridge)
        seen = self._offered.get(plan_id, set()) & ready
        new = ready - seen
        self._offered[plan_id] = seen
        if not new or (self.run_active is not None and self.run_active(plan_id)):
            return False
        try:
            self.launcher(plan_id, f"Plan: {bridge.dag.title}"[:80], self._run_text(bridge, reason, sorted(ready)))
        except Exception as exc:
            bridge.dag.note("", "auto", f"could not start a conversation for the plan: {type(exc).__name__}: {exc}"[:300], now)
            return False
        self._offered[plan_id] = seen | new
        return True

    def resume_interrupted(self, now: Optional[float] = None) -> List[str]:
        """When the program starts: a plan that was running (started and not finished, or with a step an agent had begun) takes up again where it stopped.
        The steps already done stay done with what they wrote; a step that was being done goes back to waiting its turn; a conversation of the agent
        is started for what is ready. A plan that was taken up again several times without one step more done is left for the person. Returns the plans taken up."""
        moment = time.time() if now is None else now
        out: List[str] = []
        if self._resumed:
            return out
        self._resumed = True                                       # once for each time the program starts
        for row in self.store.list():
            plan_id = row["plan_id"]
            try:
                bridge = self.store.get(plan_id, self._replanner())
            except Exception:
                continue
            dag = bridge.dag
            interrupted = [n for n in dag.nodes.values() if n.status == "in_progress"]
            stuck = [n for n in dag.nodes.values() if self._stuck(n)] if bridge.run.get("active") else []
            if bridge.paused or not (bridge.run.get("active") or interrupted):
                continue
            if dag.run_state() == "finished":
                bridge.run = {}
                self._saved(plan_id)
                continue
            done = bridge._done_count()
            run = dict(bridge.run) if bridge.run else {"active": True, "since": moment}
            resumes = int(run.get("resumes", 0)) + 1 if done <= int(run.get("progress", -1)) else 1
            if resumes > RESUME_LIMIT:
                run.update(stalled=True, resumes=resumes)
                bridge.run = run
                dag.note("", "auto", f"{dag.title}: taken up again {RESUME_LIMIT} times without a step more done: left for you", moment)
                self._saved(plan_id)
                continue
            for n in interrupted:
                n.status = "pending"
                n.state_variables["interrupted"] = {"at": moment, "resume": resumes}
                dag.note(n.id, "auto", f"{n.title}: interrupted, taken up again", moment)
            dag.refresh_blocking()
            bridge.run = {**run, "active": True, "resumes": resumes, "progress": done, "stalled": False}
            bridge.record("system", "resume", f"{dag.title}: taken up again after the program stopped")
            self._saved(plan_id)
            self.tick(plan_id, moment)
            self._offered.pop(plan_id, None)
            self._offer(plan_id, "the program stopped while it was running and it is taken up again where it was", moment)
            for n in stuck:                                         # a recovery that was going on when the program stopped goes on, counted with the step's other recoveries
                self._recover(plan_id, n.id, moment)
            out.append(plan_id)
        return out

    @staticmethod
    def _stuck(node: Any) -> str:
        """Why a step needs recovering: "failed" (it used its tries), "timed out" (it ran out of time), or "" when it does not. What is for the person is not recovered here."""
        if node.assignee.value == "user":
            return ""
        if node.status == "failed":
            return "failed"
        if node.state_variables.get("timed_out") and node.status not in ("completed", "skipped"):
            return "timed out"
        return ""

    def _recover(self, plan_id: str, node_id: str, now: Optional[float] = None) -> bool:
        """A step failed for good or ran out of time (any kind of step: one the agent does, one the plan works out itself, a wait): a conversation of the agent is
        started to find the cause (it may write code) and to force a way that passes. After a few recoveries of the same step it is left for the person.
        True when a recovery was started."""
        if self.launcher is None:
            return False
        bridge = self.store.get(plan_id, self._replanner())
        node = bridge.dag.nodes.get(node_id)
        why = self._stuck(node) if node is not None else ""
        if not why or bridge.paused:
            return False
        moment = time.time() if now is None else now
        used = int(node.state_variables.get("recoveries", 0))
        if used >= RECOVER_LIMIT:
            bridge.dag.note(node.id, "auto", f"{node.title}: {why} after {used} recoveries: left for you", moment)
            self._saved(plan_id)
            return False
        node.state_variables["recoveries"] = used + 1
        node.state_variables["attempts"] = 0
        reasons = "; ".join(f["reason"] for f in node.state_variables.get("failures") or [])
        kind = node.type.value
        own = f" It is a '{kind}' step that the plan works out itself, from {json.dumps(node.spec, ensure_ascii=False)[:500]}." if kind in _planner().nodes.AUTO else ""
        cause = ("it used up its tries" if why == "failed" else
                 "it waited longer than it may (the thing it waits for did not happen, or the agent running it took too long)")
        text = (f'The step "{node.title}" (id {node.id}) of the plan "{bridge.dag.title}" (id {plan_id}) {why}: {cause}. This is recovery {used + 1} of {RECOVER_LIMIT}.{own} '
                f"What it was to do: {node.description or node.title}. What is known: {reasons or 'no reason was written'}. "
                "Read the plan with plan_get and the step's own data, find the real cause (the error, the files, the outputs, the tools it used, what it was waiting for), and fix it: "
                "you may write or change code or scripts, change the step with plan_propose_change, set the values it needs, or force a different way that passes, even doing by a script "
                "what the model could not. When it is in place mark the step pending with plan_mark (it then waits afresh and has its tries again) and do it again, or mark it "
                "completed with its outputs (or skipped if it can be left out). If it cannot be fixed, say why with plan_mark failed and a reason. "
                "Do not ask the person unless only they can settle it.")
        self._saved(plan_id)
        try:
            self.launcher(plan_id, f"Plan recovery: {node.title}"[:80], text)
        except Exception as exc:
            bridge.dag.note(node.id, "auto", f"{node.title}: the recovery could not start: {type(exc).__name__}: {exc}"[:300], moment)
            return False
        return True

    # -- what the plans ask the person --------------------------------------------------------------------------

    def human_requests(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Everything the plans ask the person right now (an approval, a value), oldest first, each with its plan; the Plans page, the other tabs and the app show it."""
        if not self._human_loaded:
            self._human_loaded = True
            for row in self.store.list():
                try:
                    self.store.get(row["plan_id"], self._replanner())
                except Exception:
                    continue
        out: List[Dict[str, Any]] = []
        for bridge in self.store.loaded():
            out.extend(bridge.human_requests(now))
        return sorted(out, key=lambda r: r["since"])

    # -- waits written in words ------------------------------------------------------------------------------

    def _submit(self, work: Callable[[], None]) -> None:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plan-wait")      # one judgement at a time
        self._pool.submit(work)

    def _judge_waits(self, plan_id: str, bridge: Any, report: Dict[str, Any], now: float) -> None:
        """The waits in words that are due for a reading go to the technical session, off the clock's thread: when the values they watch changed,
        after the pause, and not more than the cap an hour (tadamoo/side_uses.py `plan_wait`)."""
        if not report["judge"]:
            self._degraded.pop(plan_id, None)
            return
        from serve import plan_wait
        from tadamoo import side_uses
        per_hour = int(side_uses.param("plan_wait", "max_per_hour"))
        for node_id in report["judge"]:
            node = bridge.dag.nodes.get(node_id)
            if node is None:
                continue
            values = plan_wait.watched(bridge.dag, node.spec)
            if not values:                                  # nothing to read yet
                continue
            sig = plan_wait.signature(values)
            if not plan_wait.due_for_judgement(node.state_variables.get("judged"), sig, now, node.spec.get("poll_s", 300)):
                continue
            stamps = self._judgments.setdefault(plan_id, deque())
            if not plan_wait.under_the_cap(stamps, now, per_hour):
                self._degraded[plan_id] = "rate_limit"
                continue
            stamps.append(now)
            self.judge_runner(lambda n=node_id, d=node.spec["describe"], v=values, s=sig: self._judge_one(plan_id, n, d, v, s, now))

    def _judge_one(self, plan_id: str, node_id: str, describe: str, values: Dict[str, Any], sig: str, now: float) -> None:
        from serve import plan_wait
        bridge = self.store.get(plan_id, self._replanner())
        lines = [j["text"] for j in bridge.dag.journal]
        answer = plan_wait.ask(self.client_factory() if self.client_factory else None, describe, values, lines)
        if answer is None:
            self._degraded[plan_id] = "no_judge"
            return
        self._degraded.pop(plan_id, None)
        node = bridge.dag.nodes.get(node_id)
        if node is None:
            return
        node.state_variables["judged"] = plan_wait.next_state(node.state_variables.get("judged"), answer, sig, now)
        if answer == "yes":
            bridge.settle_wait(node_id, now, "judged to hold")
        self._saved(plan_id)
        if answer == "yes":
            self.tick(plan_id)                              # what comes after the wait starts now

    def watch_tick(self, now: float) -> None:
        """Called by the clock of the routines: the plans whose next step of this kind has come due are looked at; the others cost one comparison.
        The first call looks at every plan once, to find the ones with something waiting."""
        if not self._due_scanned:
            self._due_scanned = True
            for row in self.store.list():
                self._index(row["plan_id"], now)
                self._safe_tick(row["plan_id"], now)
        for plan_id, rows in list(self._starts.items()):
            for row in list(rows):
                if row["due"] <= now:
                    self._fire_start(plan_id, row, now)
        for plan_id, due in list(self._due.items()):
            if due is not None and due <= now:
                self._safe_tick(plan_id, now)

    def _safe_tick(self, plan_id: str, now: float) -> None:
        try:
            report = self.tick(plan_id, now)
            if report.get("changed"):                          # a wait ended by itself: what comes after it may be for the agent
                self._offer(plan_id, "a wait of the plan ended", now)
        except Exception:                                       # a plan that cannot be read does not stop the others
            self._due.pop(plan_id, None)

    def handle_event(self, plan_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        bridge = self.store.get(plan_id, self._replanner())
        before = bridge.rev
        result = bridge.handle(event)
        if result.get("ok"):
            self.tick(plan_id)
            if bridge.rev != before and bridge.history and bridge.history[-1]["actor"] != "user" and bridge.history[-1]["type"] in RESULT_KINDS:
                self._attention = {"t": bridge.history[-1]["t"], "plan": plan_id, "title": bridge.dag.title, "summary": bridge.history[-1]["summary"]}
            self._saved(plan_id)
            if event.get("type") == "plan:update_meta":
                self._announce(plan_id)
            if result.get("parked"):
                self._replan_parked(plan_id, bridge, event, result)
            self._after_event(plan_id, bridge, event, result)
        return result

    def _after_event(self, plan_id: str, bridge: Any, event: Dict[str, Any], result: Dict[str, Any]) -> None:
        """What an event sets going: a plan the person started or an event started, a request the person answered, and a step that failed for good. What the
        agent itself does in a conversation (its own marks, its own start) starts nothing: it carries on by itself."""
        kind, mine = event.get("type"), event.get("actor", "user") == "chat"
        if (kind == "plan:start" and not mine) or (kind == "plan:event" and result.get("started")):
            self._offered.pop(plan_id, None)
            self._offer(plan_id, "it was started" if kind == "plan:start" else "an event started it")
        elif kind in ("plan:answer", "plan:answers") and not mine:
            self._offer(plan_id, "the person answered what it asked")
        elif (kind == "plan:mark" and event.get("status") == "failed" and not result.get("retry")) or kind == "plan:timeout":
            self._recover(plan_id, event.get("node_id", ""))

    def _replan_parked(self, plan_id: str, bridge: Any, event: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Steps were parked below a failed or timed-out one: the planner is asked for another way for that branch."""
        from tadamoo.planner.triage import TriageResult
        names = ", ".join(bridge.dag.nodes[i].title for i in result["parked"] if i in bridge.dag.nodes)
        source = bridge.dag.nodes.get(event.get("node_id"))
        why = f"{source.title if source else event.get('node_id')} {'timed out' if event.get('type') == 'plan:timeout' else 'failed'}; parked: {names}"
        verdict = TriageResult(True, 1.0, "MIXED_SOME_IMPACTFUL", [why], 0.0)
        self.start_replan(plan_id, bridge, verdict, [why])

    def events_since(self, plan_id: str, seq: int) -> List[Dict[str, Any]]:
        return self.store.get(plan_id).events_since(seq)

    def bridge(self, plan_id: str):
        return self.store.get(plan_id, self._replanner())

    def triage(self, plan_id: str, events: List[Any], replan: bool = False) -> Dict[str, Any]:
        """Judges incoming events against the plan; when the filter says wake and `replan` is true, the local
        model re-plans in the background and the outcome arrives as events on the plan's stream."""
        if not isinstance(events, list) or not events or len(events) > EVENTS_MAX:
            raise ValueError(f"send between 1 and {EVENTS_MAX} events as a list of strings")
        texts = [str(e) for e in events if isinstance(e, (str, int, float))]
        bridge = self.bridge(plan_id)
        result = self.gatekeeper().triage_event_batch(bridge.card, texts)
        return self._report(plan_id, bridge, result, texts, replan)

    def _report(self, plan_id: str, bridge: Any, result: Any, texts: List[str], replan: bool, **extra: Any) -> Dict[str, Any]:
        """Tells the plan's viewers a verdict and, if it says wake and re-planning was asked for, starts the local model."""
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
        bridge._emit("plan:triage", {"result": out, **extra})
        return out

    # -- events from the world ---------------------------------------------------------------------------

    def ingest(self, body: Any) -> Dict[str, Any]:
        """The mailbox: events ({"text", "source", "at", "plan_id"?} or a list of them under "events"). Each goes to the sensors of
        the plan it is for (every plan, when none is named) and, with the event pump on, into that plan's buffer to be judged."""
        if isinstance(body, dict) and "events" in body:
            body = body["events"]
        items = body if isinstance(body, list) else [body]
        if not 1 <= len(items) <= EVENTS_MAX:
            raise ValueError(f"send between 1 and {EVENTS_MAX} events")
        events = []
        for e in items:
            e = {"text": e} if isinstance(e, str) else e
            if not isinstance(e, dict) or not isinstance(e.get("text"), str) or not e["text"].strip():
                raise ValueError("an event is a text, or an object with a text, a source and an instant")
            at = e.get("at")
            if at is not None and (isinstance(at, bool) or not isinstance(at, (int, float))):
                raise ValueError("at is a number of seconds since 1970")
            plan = e.get("plan_id")
            if plan is not None:
                self.store._path(plan)                       # a malformed id is refused here
                self.store.get(plan)                         # and an unknown one is a 404
            events.append({"text": e["text"].strip(), "source": str(e.get("source") or "api")[:100], "at": at, "plan_id": plan})
        return self._deliver(events)

    def _deliver(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        resolved: Dict[str, List[str]] = {}
        buffered: Dict[str, int] = {}
        every = [row["plan_id"] for row in self.store.list()]
        inbox = self._inbox
        pump_on = inbox is not None
        for ev in events:
            for pid in ([ev["plan_id"]] if ev["plan_id"] else every):
                try:
                    bridge = self.bridge(pid)
                except KeyError:
                    continue
                res = bridge.handle({"type": "plan:event", "text": ev["text"], "source": ev["source"], "at": ev["at"] or time.time(), "actor": "system"})
                if res.get("ok") and res.get("resolved"):
                    resolved.setdefault(pid, []).extend(res["resolved"])
                    self._saved(pid)
                    if res.get("started"):
                        self._offered.pop(pid, None)
                        self._offer(pid, "an event started it")
                if pump_on and bridge.active():
                    inbox.add(pid, f"[{ev['source']}] {ev['text']}")
                    buffered[pid] = buffered.get(pid, 0) + 1
        self._events_in += len(events)
        return {"accepted": len(events), "resolved": resolved, "buffered": buffered, "pump": pump_on}

    def _pump_result(self, plan_id: str, result: Any, events: List[str]) -> None:
        """A batch the pump judged: shown to the plan's viewers and, if it says wake, the local model re-plans (proposals only)."""
        bridge = self.bridge(plan_id)
        self._report(plan_id, bridge, result, events, True, via="pump", events=len(events))

    def _card_for(self, plan_id: str):
        try:
            return self.bridge(plan_id).card
        except (KeyError, ValueError):
            return None

    # -- the pump and the sources ------------------------------------------------------------------------

    def _vault(self):
        if self.vault_factory is not None:
            return self.vault_factory()
        from tadamoo.vault import Vault
        from serve import userdata
        return Vault(userdata.path("vault"))

    def _reveal(self, entry: str, field: str) -> str:
        return self._vault().reveal(entry, field)

    def _state_path(self) -> Path:
        return self.dir / "sources_state.json"

    def _load_state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, source: Any) -> None:
        with self._lock:
            data = self._load_state()
            data[source.id] = source.state
            tmp = self._state_path().with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._state_path())

    def _deliver_from(self, cfg: Dict[str, Any], events: List[Any]) -> None:
        self._deliver([{"text": e.text, "source": e.source, "at": e.at, "plan_id": e.plan_id or cfg.get("plan_id")} for e in events])

    def _plan_dates(self) -> List[Dict[str, Any]]:
        """The dates of the plans that still have something to protect, for the deadlines source."""
        out = []
        for row in self.store.list():
            try:
                bridge = self.store.get(row["plan_id"])
            except Exception:
                continue
            if bridge.active() and (bridge.profile.starts_on or bridge.profile.ends_on):
                out.append({"plan_id": row["plan_id"], "title": row["title"], "starts_on": bridge.profile.starts_on, "ends_on": bridge.profile.ends_on})
        return out

    def sync_events(self) -> None:
        """Starts or stops the event pump and the read-only sources to match the settings. With the pump on and nothing to do its
        thread sleeps; the person can turn it off."""
        with self._sync_lock:
            self._booted = True
            s = self.settings()
            self._stop_events()
            if not s["event_pump"]:
                return
            p = _planner()
            inbox = p.EventInbox(self.pump_batch, self.pump_window)
            pump = p.InboxPump(inbox, self.gatekeeper, self._card_for, self._pump_result, self.pump_poll)
            saved = self._load_state()
            built = [(cfg, p.sources.build_source(cfg, self._reveal, saved.get(cfg["id"]), self._plan_dates)) for cfg in s["sources"] if cfg.get("enabled", True)]
            by_id = {src.id: cfg for cfg, src in built}
            runner = p.sources.SourceRunner([src for _, src in built], lambda src, evs: self._deliver_from(by_id[src.id], evs), self._save_state)
            with self._lock:
                self._inbox, self._pump, self._runner = inbox, pump, runner
            pump.start()
            runner.start()
            if not getattr(self, "_atexit", False):
                self._atexit = True
                atexit.register(self.close)

    def _stop_events(self) -> None:
        """Takes the pump and the sources out of use and ends their threads (outside the lock: a thread may be waiting for it)."""
        with self._lock:
            runner, pump = self._runner, self._pump
            self._runner = self._pump = self._inbox = None
        if runner is not None:
            runner.stop()
        if pump is not None:
            pump.stop()

    def close(self) -> None:
        """Ends the pump and the sources' threads; the server calls it as it stops."""
        self._stop_events()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def events_status(self) -> Dict[str, Any]:
        pump, inbox, runner = self._pump, self._inbox, self._runner
        return {"pump": pump is not None and pump.running, "accepted": self._events_in,
                "pending": inbox.pending() if inbox else {}, "dropped": inbox.dropped() if inbox else 0,
                "batches": pump.batches if pump else 0, "errors": list(pump.errors)[-5:] if pump else [],
                "sources": runner.status() if runner else []}

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
            bridge.replan(result, events, auto_apply=self.settings()["auto_apply"] is True)
            self._saved(plan_id)
        except Exception as exc:
            bridge._emit("plan:replan_failed", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            with self._lock:
                self._replanning.pop(plan_id, None)
