"""What in a plan reaches beyond the person's own work, step by step: other people, a call to prepare, bookings, payments and resources the
software does not control. Those steps are the named exceptions: a change that touches one is left for the person, while a change that touches
none goes through. An effect that cannot be taken back is not an exception by itself. The facts come from the plan's data, not from a
guess about its words."""
from __future__ import annotations

import copy
from typing import Any, Dict, List, NamedTuple

PEOPLE = "a step for someone else"
CALL = "a phone call to prepare"
EFFECTS = "a booking, a payment or a resource out of the software's control, already done"
EXCEPTION_KINDS = ("booking", "payment", "uncontrolled")       # the `kind` a recorded effect can carry
PAYMENT_TOOLS = ("request_payment",)


def _holds_back(effect: Any) -> bool:
    return isinstance(effect, dict) and (effect.get("kind") in EXCEPTION_KINDS or effect.get("tool") in PAYMENT_TOOLS)


def step_facts(node: Dict[str, Any]) -> List[str]:
    """Why one step is an exception, from its own data; empty when it is not."""
    out: List[str] = []
    if node.get("assignee") == "third_party":
        out.append(PEOPLE)
    if node.get("call_request"):
        out.append(CALL)
    if any(_holds_back(e) for e in (node.get("state_variables") or {}).get("effects") or []):
        out.append(EFFECTS)
    return out


class Split(NamedTuple):
    safe: Dict[str, Any]           # the part of the diff that touches no exception
    parked: Dict[str, Any]         # the part that does, and what depends on it
    reasons: List[str]             # one line per step that makes it an exception


def _empty(diff: Dict[str, Any]) -> bool:
    return not any(diff.get(k) for k in ("add_nodes", "remove_nodes", "update_nodes", "add_edges", "remove_edges"))


def split_diff(nodes: Dict[str, Dict[str, Any]], diff: Dict[str, Any]) -> Split:
    """Divides a diff into what can be applied without the person and what waits for them. A new step that depends on a parked new step
    is parked with it; a link is parked when an end of it is a parked new step, or when both ends exist and one of them is an exception."""
    added = {n.get("id"): n for n in diff.get("add_nodes") or [] if isinstance(n, dict)}
    reasons: List[str] = []
    parked_ids = set()

    def note(label: str, facts: List[str]) -> bool:
        reasons.extend(f'"{label}": {f}' for f in facts if f"\"{label}\": {f}" not in reasons)
        return bool(facts)

    for nid, n in added.items():
        if note(str(n.get("title") or nid)[:60], step_facts(n)):
            parked_ids.add(nid)
    grew = True
    while grew:                                            # what waits for a parked new step waits too
        grew = False
        for nid, n in added.items():
            if nid not in parked_ids and any(d in parked_ids for d in n.get("dependencies") or []):
                parked_ids.add(nid)
                grew = True
                reasons.append(f'"{str(n.get("title") or nid)[:60]}": waits for a step of that kind')
    parked_remove = [i for i in diff.get("remove_nodes") or [] if i in nodes and note(str(nodes[i].get("title") or i)[:60], step_facts(nodes[i]))]
    parked_update: Dict[str, Any] = {}
    for i, fields in (diff.get("update_nodes") or {}).items():
        before = nodes.get(i) or {}
        after = {**before, **fields} if isinstance(fields, dict) else before
        if note(str(before.get("title") or i)[:60], list(dict.fromkeys(step_facts(before) + step_facts(after)))):
            parked_update[i] = fields

    def edge_parked(edge: Any) -> bool:
        a, b = edge
        if a in parked_ids or b in parked_ids:
            return True
        return a in nodes and b in nodes and bool(note(str(nodes[a].get("title") or a)[:60], step_facts(nodes[a]))
                                                  or note(str(nodes[b].get("title") or b)[:60], step_facts(nodes[b])))

    safe: Dict[str, Any] = {"add_nodes": [], "remove_nodes": [i for i in diff.get("remove_nodes") or [] if i not in parked_remove],
                            "update_nodes": {i: f for i, f in (diff.get("update_nodes") or {}).items() if i not in parked_update},
                            "add_edges": [], "remove_edges": []}
    parked: Dict[str, Any] = {"add_nodes": [], "remove_nodes": parked_remove, "update_nodes": parked_update, "add_edges": [], "remove_edges": []}
    for key in ("add_edges", "remove_edges"):
        for edge in diff.get(key) or []:
            (parked if isinstance(edge, (list, tuple)) and len(edge) == 2 and edge_parked(edge) else safe)[key].append(edge)
    for nid, n in added.items():
        if nid in parked_ids:
            parked["add_nodes"].append(n)
            continue
        kept = copy.deepcopy(n)
        later = [c for c in kept.get("outputs") or [] if c in parked_ids]
        if later:                                          # a link to a parked new step is parked with it
            kept["outputs"] = [c for c in kept["outputs"] if c not in parked_ids]
            parked["add_edges"].extend([nid, c] for c in later)
        safe["add_nodes"].append(kept)
    return Split({k: v for k, v in safe.items() if v}, {k: v for k, v in parked.items() if v}, reasons)
