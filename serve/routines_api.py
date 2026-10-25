"""The routines of the agent (harness/scheduler.py) as the server's API.

The scheduler is made when first needed and runs the routines through the server's conversation manager. Routes, under
/api/routines (the writes pass the same gates as the other /api/ writes):

  GET  /api/routines                  the routines, what is running or queued, and the latest results
  GET  /api/routines/runs?id=&limit=  results, newest first
  POST /api/routines                  a new routine (disabled)
  POST /api/routines/<id>/update | enable | disable | run | remove
  POST /api/routines/cancel           stops the routine that is running
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from harness.energy import EnergyMeter
from harness.scheduler import RoutineError, Scheduler, SessionRoutineRunner

RECENT_RUNS = 20


class RoutinesApi:
    def __init__(self, directory: str | Path, sessions: Any, scheduler: Optional[Scheduler] = None,
                 power_source: Optional[Callable[[], Any]] = None) -> None:
        """`power_source()` returns the GPUs' power readings (the server's telemetry); with it each result carries its energy."""
        meter = (lambda: EnergyMeter(power_source)) if power_source else None
        self.scheduler = scheduler or Scheduler(directory, SessionRoutineRunner(sessions), meter=meter).start()

    def overview(self) -> Dict[str, Any]:
        return {**self.scheduler.view(), "runs": self.scheduler.runs(limit=RECENT_RUNS)}

    def runs(self, routine_id: str = "", limit: Any = 50) -> Dict[str, Any]:
        try:
            count = int(limit)
        except (TypeError, ValueError):
            raise RoutineError("The limit is a number.", "bad_limit") from None
        return {"runs": self.scheduler.runs(routine_id, count)}

    def create(self, req: Dict[str, Any]) -> Dict[str, Any]:
        return {"routine": self.scheduler.add(req.get("name"), req.get("prompt"), req.get("workspace"), req.get("schedule"),
                                              req.get("mode", "read_only"), req.get("allow"), req.get("max_turns", 10),
                                              req.get("confirm_auto") is True)}

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
        if action == "update":
            return {"routine": s.update(routine_id, **{k: v for k, v in req.items() if k != "id"})}
        raise RoutineError("Unknown action.", "unknown")

    def cancel(self) -> Dict[str, Any]:
        self.scheduler.cancel_running()
        return {"cancelled": True}

    def shutdown(self) -> None:
        self.scheduler.shutdown()
