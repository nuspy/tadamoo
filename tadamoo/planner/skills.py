"""The skills a step of a plan is given: those of the whole plan and those of the step itself.

Works on the plan as the server shows it (a snapshot's `plan` and `profile`). A skill of the whole plan is for every step of it, used or not; a skill of a step is
for that step only. The harness hands their instructions to the step when it starts (tadamoo/planner_tools.py `plan_step_start`), wherever the step runs."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def of_step(plan: Dict[str, Any], node_id: str, profile: Optional[Dict[str, Any]] = None) -> Tuple[List[str], List[str]]:
    """(the plan's skills, the step's own) for `node_id`, each once; a skill the step also has of its own is only the plan's. A step that is not an action has none."""
    node = (plan.get("nodes") or {}).get(node_id)
    if not isinstance(node, dict) or node.get("type") != "action":
        return [], []
    whole = list(dict.fromkeys(s for s in (profile or {}).get("skills") or [] if isinstance(s, str)))
    own = [s for s in dict.fromkeys((node.get("spec") or {}).get("skills") or []) if isinstance(s, str) and s not in whole]
    return whole, own
