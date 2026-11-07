"""The routines of the agent (tadamoo/scheduler.py) as the server's API.

The scheduler is made when first needed and runs the routines through the server's conversation manager. Routes, under
/api/routines (the writes pass the same gates as the other /api/ writes):

  GET  /api/routines                  the routines, what is running or queued, the pending requests and the latest results
  GET  /api/routines/runs?id=&limit=  results, newest first
  POST /api/routines                  a new routine (enabled; it always runs in Automatic)
  POST /api/routines/<id>/update | enable | disable | run | remove
  POST /api/routines/<pending id>/dismiss   removes a pending request from the list
  POST /api/routines/cancel           stops the routines that are running
  POST /api/routines/stop             the kill switch ({"on": false} takes it off)
  POST /api/routines/<id>/improve     the script that would do what the last good run did (nothing changes)
  POST /api/routines/<id>/improve_apply   saves that script as a personal skill and makes the routine run it
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from tadamoo.energy import EnergyMeter
from tadamoo.personal_skills.store import SkillError
from tadamoo.personal_skills.values import resolve as resolve_values
from tadamoo.scheduler import RoutineError, Scheduler, SessionRoutineRunner

RECENT_RUNS = 20


class RoutinesApi:
    def __init__(self, directory: str | Path, sessions: Any, scheduler: Optional[Scheduler] = None,
                 power_source: Optional[Callable[[], Any]] = None, plan_paused: Optional[Callable[[str], bool]] = None,
                 plan_context: Optional[Callable[[str], Dict[str, Any]]] = None) -> None:
        """`power_source()` returns the GPUs' power readings (the server's telemetry); with it each result carries its energy.
        `plan_paused(plan_id)` says whether a "plan" delivery's plan has its schedule stopped; `plan_context(plan_id)` gives a "plan"
        delivery's message template the plan's title, ready step and shared values (serve/plans.py `PlanService`)."""
        self.sessions = sessions                          # its personal skills are what a skill routine runs
        meter = (lambda: EnergyMeter(power_source)) if power_source else None
        self.scheduler = scheduler or Scheduler(directory, SessionRoutineRunner(sessions, plan_paused=plan_paused, plan_context=plan_context),
                                                meter=meter).start()

    def overview(self) -> Dict[str, Any]:
        return {**self.scheduler.view(), "runs": self.scheduler.runs(limit=RECENT_RUNS)}

    def runs(self, routine_id: str = "", limit: Any = 50) -> Dict[str, Any]:
        try:
            count = int(limit)
        except (TypeError, ValueError):
            raise RoutineError("The limit is a number.", "bad_limit") from None
        return {"runs": self.scheduler.runs(routine_id, count)}

    def _skill_ok(self, slug: Any, values: Any) -> None:
        """A routine that runs a skill names one that exists on this PC, and gives values the skill takes (a date in words is read at each run;
        here it is only tried, so a wrong one is refused now and not at three in the morning)."""
        skills = getattr(self.sessions, "skills", None)
        if skills is None:
            raise RoutineError("The personal skills are not available.", "bad_skill")
        try:
            skill = skills.get(slug)
        except SkillError as exc:
            raise RoutineError(str(exc), "bad_skill") from None
        _, errors = resolve_values(skill.get("params") or [], values or {}, datetime.now())
        if errors:
            raise RoutineError("; ".join(f"{k}: {v}" for k, v in errors.items()), "bad_values")

    def create(self, req: Dict[str, Any]) -> Dict[str, Any]:
        if req.get("skill"):
            self._skill_ok(req.get("skill"), req.get("values"))
        return {"routine": self.scheduler.add(req.get("name"), req.get("prompt"), req.get("workspace"), req.get("schedule"),
                                              req.get("max_turns"), req.get("max_minutes"), req.get("skill") or "",
                                              req.get("values"), req.get("yield_s"), req.get("index_past", False), req.get("check") or "",
                                              req.get("ttl_min"), req.get("dedup_key") or "", req.get("deliver_to"), req.get("message") or "", req.get("link"))}

    def act(self, routine_id: str, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        s = self.scheduler
        if action == "enable":
            return {"routine": s.set_enabled(routine_id, True)}
        if action == "disable":
            return {"routine": s.set_enabled(routine_id, False)}
        if action == "run":
            return {"routine": s.run_now(routine_id)}
        if action == "remove":
            s.remove(routine_id)
            return {"removed": routine_id}
        if action == "dismiss":
            s.dismiss(routine_id)
            return {"dismissed": routine_id}
        if action == "improve":
            return self.improve(routine_id)
        if action == "improve_apply":
            return self.improve_apply(routine_id, req)
        if action == "update":
            fields = {k: v for k, v in req.items() if k != "id"}
            current = s.get(routine_id)
            slug = fields.get("skill", current.get("skill"))
            if slug:
                self._skill_ok(slug, fields.get("values", current.get("values")))
            return {"routine": s.update(routine_id, **fields)}
        raise RoutineError("Unknown action.", "unknown")

    def stop(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """The kill switch: nothing starts and what runs is stopped, until it is taken off (`{"on": false}`)."""
        self.scheduler.emergency_stop(bool(req.get("on", True)))
        return {"stopped": self.scheduler.stopped()}

    def improve(self, routine_id: str) -> Dict[str, Any]:
        """The script that would do what the routine's last good run did, for the person to read; nothing is changed."""
        routine = self.scheduler.get(routine_id)
        last = next((r for r in self.scheduler.runs(routine_id, 50) if r.get("status") == "done" and r.get("session")), None)
        if last is None:
            raise RoutineError("The routine has no finished run to learn from yet.", "no_run")
        from serve.agent_runs import RunError
        try:
            return {"routine": routine_id, "from_run": last["id"], **self.sessions.routine_skill(last["session"], routine["name"])}
        except RunError as exc:
            raise RoutineError(str(exc), "improve") from None

    def improve_apply(self, routine_id: str, req: Dict[str, Any]) -> Dict[str, Any]:
        """Saves the script as a personal skill and makes the routine run it, with each parameter's default as its value."""
        skills = getattr(self.sessions, "skills", None)
        if skills is None:
            raise RoutineError("The personal skills are not available.", "bad_skill")
        params = req.get("params") if isinstance(req.get("params"), list) else []
        try:
            skill = skills.save({"name": req.get("name"), "description": req.get("description"), "params": params, "source": {"routine": routine_id}}, str(req.get("script") or ""))
        except SkillError as exc:
            raise RoutineError(str(exc), "bad_skill") from None
        values = {p["name"]: p["default"] for p in params if isinstance(p, dict) and p.get("name") and p.get("default") is not None}
        return {"skill": skill["slug"], "routine": self.scheduler.update(routine_id, skill=skill["slug"], values=values)}

    def cancel(self) -> Dict[str, Any]:
        self.scheduler.cancel_running()
        return {"cancelled": True}

    def shutdown(self) -> None:
        self.scheduler.shutdown()
