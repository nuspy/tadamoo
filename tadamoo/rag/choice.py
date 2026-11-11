"""The choice of a conversation's attached knowledge archives: one rule for the tools, the slash command and the routes. Nothing is attached or
dropped on a guess: a name that fits two archives returns them and changes nothing."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


COMMAND = re.compile(r"^\s*/(?:rag|knowledge)(?:\s+(.*))?$", re.I | re.S)


def parse_command(text: Any) -> Optional[Tuple[str, str]]:
    """`(action, name)` of a message that is the `/rag` command: `/rag` (list), `/rag name` (attach), `/rag off name` (detach), `/rag off` (clear);
    None for any other text."""
    found = COMMAND.match(text) if isinstance(text, str) else None
    if not found:
        return None
    rest = (found.group(1) or "").strip()
    off = re.match(r"^off(?:\s+(.*))?$", rest, re.I | re.S)
    if off:
        name = (off.group(1) or "").strip()
        return ("detach", name) if name else ("clear", "")
    return ("attach", rest) if rest else ("list", "")


def reply(action: str, got: Dict[str, Any], name: str = "") -> str:
    """One line for the person about what `decide` did, for a client that has no page of its own to word it."""
    status, archive = got["status"], got.get("archive") or {}
    label = archive.get("name") or name
    return {
        "attached": f"Knowledge: {label} attached",
        "already": f"Knowledge: {label} is already attached",
        "this_project": f"Knowledge: {label or 'that archive'} belongs to this project and is always used",
        "detached": f"Knowledge: {label} dropped",
        "cleared": "Knowledge: all attached archives dropped" if got.get("dropped") else "Knowledge: no archive was attached",
        "not_attached": f"Knowledge: no attached archive matches \"{name}\"",
        "none": f"Knowledge: no archive matches \"{name}\". Send /rag to see them",
    }.get(status) or "Knowledge: several archives match (" + "; ".join(f"{c['name']} ({c['project']})" for c in got.get("candidates", [])) + "). Write more of the name"


def listing(rows: Sequence[Dict[str, Any]], project: str, attached: Sequence[str]) -> str:
    """The `/rag` list in one line: each archive with its project and whether this conversation uses it."""
    if not rows:
        return "Knowledge: there is no archive yet. Make one in the Knowledge tab"
    parts = []
    for r in rows:
        where = "this project" if r.get("project") == project else (r.get("project_name") or "?")
        parts.append(f"{r['name']} ({where}{', used here' if r['id'] in attached else ''})")
    return "Knowledge archives: " + "; ".join(parts)


def known(ids: Sequence[Any], rows: Sequence[Dict[str, Any]]) -> List[str]:
    """`ids` without the duplicates and the archives that do not exist."""
    there = {r["id"] for r in rows}
    return [i for i in dict.fromkeys(ids) if isinstance(i, str) and i in there]
