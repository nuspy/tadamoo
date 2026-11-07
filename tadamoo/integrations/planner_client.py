"""Client for the server's planner routes (/api/plans): what the Chat's planner tools use.

The plans live in the server (one store for the Chat and the Plans view). The agent process talks to them over the
loopback address, with the API key when there is one. Every change it sends names the Chat as its author, so the history
of a plan shows who did what.
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Dict, List, Optional

from ._http import ServiceError, request_json
from tadamoo import control_secret

ACTOR = "chat"


class PlannerClient:
    def __init__(self, endpoint: str, api_key: Optional[str] = None, timeout: float = 10.0, session_id: str = "") -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session_id = session_id or os.environ.get("TADAMOO_SESSION_ID", "")      # the plans this session makes are its own

    def _call(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        headers.update(control_secret.header_for(self.endpoint))
        return request_json(method, self.endpoint + path, payload, headers, self.timeout)

    @staticmethod
    def _id(plan_id: str) -> str:
        if not isinstance(plan_id, str) or not plan_id or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in plan_id):
            raise ValueError(f"not a plan id: {plan_id!r}")
        return urllib.parse.quote(plan_id)

    def list(self, archived: bool = False) -> List[Dict[str, Any]]:
        data = self._call("GET", "/api/plans" + ("?archived=1" if archived else ""))
        return data.get("plans", []) if isinstance(data, dict) else []

    def snapshot(self, plan_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/api/plans/{self._id(plan_id)}")

    def create(self, plan: Dict[str, Any], profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._call("POST", "/api/plans", {"plan": plan, "actor": ACTOR, "created_by": self.session_id, **({"profile": profile} if profile else {})})

    def set_paused(self, plan_id: str, value: bool) -> Dict[str, Any]:
        return self._call("POST", f"/api/plans/{self._id(plan_id)}/{'pause' if value else 'resume'}", {"actor": ACTOR})

    def set_schedule(self, plan_id: str, schedule: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", f"/api/plans/{self._id(plan_id)}/schedule", {"actor": ACTOR, "schedule": schedule})

    def remove_schedule(self, plan_id: str) -> Dict[str, Any]:
        return self._call("POST", f"/api/plans/{self._id(plan_id)}/schedule_remove", {"actor": ACTOR})

    def alignment(self, plan_id: str, model: bool = False) -> Dict[str, Any]:
        """How the plan sits with the others; with `model` the local model is asked to read what the plans mean together."""
        if model:
            return self._call("POST", f"/api/plans/{self._id(plan_id)}/alignment", {"model": True, "actor": ACTOR})
        return self._call("GET", f"/api/plans/{self._id(plan_id)}/alignment")

    def event(self, plan_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        """One bridge event (plan:mark, plan:propose_diff, plan:approve, ...), sent as the Chat."""
        return self._call("POST", f"/api/plans/{self._id(plan_id)}/events", {**event, "actor": ACTOR})

    def ingest(self, text: str, source: str, plan_id: Optional[str] = None) -> Dict[str, Any]:
        """One event from the world, for the planner's mailbox (its sensors and, with the pump on, its filter); for one plan, or for every plan."""
        return self._call("POST", "/api/plans/events", {"text": text, "source": source, **({"plan_id": plan_id} if plan_id else {})})

    def task(self, task_id: str, status: str, result: str = "", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The Tasks panel's line for a step being carried out (running, done, failed)."""
        body: Dict[str, Any] = {"id": task_id, "status": status, "result": result[:500]}
        if metadata is not None:
            body["metadata"] = metadata
        return self._call("POST", "/api/tasks", body)

    def archive(self, plan_id: str) -> Dict[str, Any]:
        return self._call("POST", f"/api/plans/{self._id(plan_id)}/archive", {"actor": ACTOR})


__all__ = ["PlannerClient", "ServiceError", "ACTOR"]
