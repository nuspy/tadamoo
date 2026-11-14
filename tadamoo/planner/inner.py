"""What a command of the program that does a step of a plan in several stages says to the plan, so that after an interruption (even a restart of the PC) the
step goes on from the stage it had reached: "this stage is finished, and this is its result".

The command runs in the conversation that is doing the step, so it finds the step by the conversation (TADAMOO_SESSION_ID) and the server by TADAMOO_URL and
TADAMOO_API_KEY, which the conversation passes to its commands; or it is told the plan and the step. Nothing here ever stops the work: a plan that cannot be reached is a stage not recorded."""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from tadamoo.integrations.planner_client import PlannerClient

DEFAULT_URL = "http://127.0.0.1:7232"


def record(step: str, result: Optional[Dict[str, Any]] = None, *, plan_id: str = "", node_id: str = "", url: str = "", key: str = "", session: str = "") -> bool:
    """Records that the stage `step` of the current step is finished, with its essential result. True when the plan took it."""
    client = PlannerClient(url or os.environ.get("TADAMOO_URL") or DEFAULT_URL, key or os.environ.get("TADAMOO_API_KEY") or None, session_id=session)
    try:
        client.inner(step, result, plan_id, node_id)
    except Exception:
        return False
    return True


def phase_recorder(work: str = "", **where: Any) -> Callable[[Dict[str, Any]], None]:
    """A listener for the progress events of a production: each `phase` that ends is a stage finished; what it says of itself is its result, with the work folder."""
    def listen(event: Dict[str, Any]) -> None:
        if event.get("event") == "phase" and event.get("phase"):
            result = {k: v for k, v in event.items() if k not in ("event", "phase") and isinstance(v, (str, int, float, bool))}
            record(str(event["phase"]), {**({"folder": work} if work else {}), **result}, **where)
    return listen
