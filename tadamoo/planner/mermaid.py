"""A plan as a Mermaid flowchart: text that can be pasted into a document or a page that draws it (Markdown viewers, wikis).

The shapes follow the plan view: an action is a box, a sensor a pill, a decision a diamond, a merge a hexagon. A step that is done, failed
or blocked is coloured by a class, and the words never rely on colour alone: the status is also written in the label. Titles come from
the person or the model, so they are put inside quotes with everything that could end the label escaped.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

SHAPES = {"action": ("[", "]"), "sensor": ("([", "])"), "decision": ("{", "}"), "merge": ("{{", "}}")}
CLASSES = {"completed": "done", "failed": "failed", "blocked": "blocked", "in_progress": "doing", "skipped": "skipped"}
STYLES = {
    "done": "fill:#1f6f43,stroke:#7bd88f,color:#ffffff",
    "failed": "fill:#7a1f2b,stroke:#ff8fa3,color:#ffffff",
    "blocked": "fill:#5a4a12,stroke:#e7c75a,color:#ffffff",
    "doing": "fill:#17406b,stroke:#6db3ff,color:#ffffff",
    "skipped": "fill:#333333,stroke:#999999,color:#dddddd",
}


def _label(text: Any, limit: int = 80) -> str:
    """Text inside a quoted Mermaid label: no quote, bracket or line break can end it early, and no markup is let through."""
    flat = " ".join(str(text or "").split())[:limit]
    flat = re.sub(r"[<>]", "", flat)
    return flat.replace("&", "#amp;").replace('"', "#quot;").replace("|", "#124;").replace("[", "#91;").replace("]", "#93;") \
               .replace("(", "#40;").replace(")", "#41;").replace("{", "#123;").replace("}", "#125;").replace("`", "#96;")


def _id(node_id: str, seen: Dict[str, str]) -> str:
    """A name Mermaid accepts for every node id, the same each time."""
    if node_id not in seen:
        base = re.sub(r"\W", "_", node_id)[:24] or "n"
        name, k = f"n_{base}", 1
        while name in seen.values():
            k += 1
            name = f"n_{base}_{k}"
        seen[node_id] = name
    return seen[node_id]


def to_mermaid(plan: Dict[str, Any]) -> str:
    """`plan` is `PlanDAG.to_dict()` (or the `plan` part of a snapshot)."""
    nodes = plan.get("nodes") or {}
    names: Dict[str, str] = {}
    lines: List[str] = [f"%% {_label(plan.get('title') or plan.get('plan_id'), 120)}", "flowchart TD"]
    for nid, n in nodes.items():
        left, right = SHAPES.get(n.get("type"), SHAPES["action"])
        status = n.get("status") or "pending"
        who = {"user": " (you)", "third_party": " (other)"}.get(n.get("assignee"), "")
        text = _label(n.get("title") or nid) + who + ("" if status == "pending" else f" · {status.replace('_', ' ')}")
        lines.append(f'    {_id(nid, names)}{left}"{text}"{right}')
    for nid, n in nodes.items():
        for child in n.get("outputs") or []:
            if child in nodes:
                lines.append(f"    {_id(nid, names)} --> {_id(child, names)}")
    used = set()
    for nid, n in nodes.items():
        cls = CLASSES.get(n.get("status"))
        if cls:
            lines.append(f"    class {_id(nid, names)} {cls}")
            used.add(cls)
    for cls in sorted(used):
        lines.append(f"    classDef {cls} {STYLES[cls]}")
    return "\n".join(lines) + "\n"
