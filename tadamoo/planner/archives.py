"""The knowledge archives a step of a plan may use: those of the whole plan, those of the step itself and those of every RAG node connected to it.

Works on the plan as the server shows it (a snapshot's `plan` and `profile`), so the harness and the server count the same way. The ids it returns are
then given to the resolver (tadamoo/rag/resolve.py) with the function `plan_node`, which applies the cloud rule and names the archives."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def of_step(plan: Dict[str, Any], node_id: str, profile: Optional[Dict[str, Any]] = None) -> List[str]:
    """Archive ids for `node_id`: the plan's (`profile.rag_archives`), the step's own `spec.rag_archives`, and the `archives` of each RAG node that has the
    step among the steps it feeds; each once, in that order."""
    nodes = plan.get("nodes") or {}
    node = nodes.get(node_id)
    if not isinstance(node, dict) or node.get("type") != "action":
        return []
    ids: List[str] = list((profile or {}).get("rag_archives") or [])
    ids += (node.get("spec") or {}).get("rag_archives") or []
    for other in nodes.values():
        if other.get("type") == "rag" and node_id in ((other.get("routes") or {}).get("rag") or []):
            ids += (other.get("spec") or {}).get("archives") or []
    return list(dict.fromkeys(i for i in ids if isinstance(i, str)))


def feeders(plan: Dict[str, Any], node_id: str) -> List[str]:
    """The ids of the RAG nodes connected to a step."""
    return [nid for nid, n in (plan.get("nodes") or {}).items() if n.get("type") == "rag" and node_id in ((n.get("routes") or {}).get("rag") or [])]
