"""The choice of a conversation's attached knowledge archives: one rule for the tools, the slash command and the routes. Nothing is attached or
dropped on a guess: a name that fits two archives returns them and changes nothing."""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from tadamoo.rag.match import find


def brief(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "project": row.get("project_name") or row.get("project") or ""}


def decide(rows: Sequence[Dict[str, Any]], project: str, attached: Sequence[str], action: str, text: Any = "") -> Dict[str, Any]:
    """What `attach`, `detach` or `clear` of the archive named `text` does to the attached list `attached` of a conversation whose own project is
    `project`: `{status, ids, archive?, candidates?}`. Statuses: attached, already, this_project, detached, not_attached, cleared, ambiguous, none."""
    current = list(dict.fromkeys(attached))
    if action == "clear":
        return {"status": "cleared", "ids": [], "dropped": len(current)}
    if action == "attach":
        found = find(rows, text)
    elif action == "detach":
        found = find([r for r in rows if r["id"] in current], text)
        if found["status"] == "none":
            own = find([r for r in rows if r.get("project") == project], text)
            return {"status": "this_project" if own["status"] != "none" else "not_attached", "ids": current}
    else:
        raise ValueError("attach, detach or clear")
    if found["status"] == "many":
        return {"status": "ambiguous", "ids": current, "candidates": [brief(r) for r in found["candidates"]]}
    if found["status"] == "none":
        return {"status": "none", "ids": current}
    row = found["archive"]
    if action == "attach":
        if row.get("project") == project:
            return {"status": "this_project", "ids": current, "archive": brief(row)}
        if row["id"] in current:
            return {"status": "already", "ids": current, "archive": brief(row)}
        return {"status": "attached", "ids": current + [row["id"]], "archive": brief(row)}
    return {"status": "detached", "ids": [i for i in current if i != row["id"]], "archive": brief(row)}


def known(ids: Sequence[Any], rows: Sequence[Dict[str, Any]]) -> List[str]:
    """`ids` without the duplicates and the archives that do not exist."""
    there = {r["id"] for r in rows}
    return [i for i in dict.fromkeys(ids) if isinstance(i, str) and i in there]
