"""The map of a project's development: one file, `docs/roadmap.json`, that the register (tadamoo/taskboard.py) writes and reads, and from which the text
`docs/roadmap.md` and the page that is published as an Artifact (`tools/roadmap/page_template.html`) are made.

    python -m tadamoo.roadmap write   [--project FOLDER]               the register -> docs/roadmap.json + docs/roadmap.md
    python -m tadamoo.roadmap load    [--project FOLDER]               docs/roadmap.json -> the register (what has the same id is updated)
    python -m tadamoo.roadmap render  [--project FOLDER] [--page FILE] docs/roadmap.json -> docs/roadmap.md (and the page, when asked), with no register

A node is a top-level task of the register (a program's own tasks, the `agent-`, `tg-` and `req-` ones, are not part of the map): a closed one is `done`. Its three notes are
the page's three lists: `solved` (what was done and the problems that came up), `decisions` (open decisions and points) and `risks` (problems foreseen and how they would
be met). Hidden nodes are left out. The text of a note is plain: one line is one item, `code` between backticks and **bold** between double asterisks."""
from __future__ import annotations

import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tadamoo.taskboard import NOTE_KEYS, PROGRAMS, TaskBoard, TaskError, git_links

SCHEMA = "roadmap-1"
FOLDER, JSON_FILE, MD_FILE = "docs", "roadmap.json", "roadmap.md"
TEMPLATE = Path(__file__).resolve().parents[1] / "tools" / "roadmap" / "page_template.html"
STATUS_OF = {"queued": "none", "running": "run", "waiting": "wait", "blocked": "wait", "review": "review", "decision": "decision"}
STATE_OF = {"none": "queued", "run": "running", "wait": "waiting", "review": "review", "decision": "decision"}
LABEL = {"done": "fatto", "run": "in corso", "review": "da committare", "decision": "decisione aperta", "wait": "in attesa", "none": "non iniziato"}
NO_GROUP = "Senza gruppo"
SECTIONS = (("solved", "Fatto e problemi risolti"), ("decisions", "Decisioni e punti aperti"), ("risks", "Problemi futuri previsti e come pensiamo di risolverli"))


def lines(text: Any) -> List[str]:
    """The items of a note: one for each non-empty line, a leading dash taken off."""
    return [re.sub(r"^\s*[-*•]\s+", "", ln).strip() for ln in str(text or "").splitlines() if ln.strip()]


def _program(row: Dict[str, Any]) -> bool:
    return row["id"].startswith(PROGRAMS) or bool(row["links"]["plan"])


def _paths(board: TaskBoard, folder: Optional[Path] = None) -> Dict[str, Path]:
    base = Path(folder) if folder else board.project / FOLDER
    return {"folder": base, "json": base / JSON_FILE, "md": base / MD_FILE}


# -- the register -> the map ----------------------------------------------------------------------------------------

def export(board: TaskBoard, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The map as data: groups in order, nodes in order (by group, then by number)."""
    doc = board._read_doc()
    groups = sorted(doc["groups"], key=lambda g: (g["order"], g["id"]))
    known = {g["id"] for g in groups}
    open_rows = [r for r in doc["tasks"] if not r["parent"] and not r["hidden"] and not _program(r)]
    done_rows = [r for r in doc["history"] if not r["parent"] and not r["hidden"] and not _program(r)]
    kids: Dict[str, List[Dict[str, Any]]] = {}
    for r in doc["tasks"]:
        if r["parent"]:
            kids.setdefault(r["parent"], []).append(r)
    nodes = []
    for r in sorted(open_rows + done_rows, key=lambda r: (r["num"] or 10 ** 6, r.get("closed", 0), r["created"])):
        solved, decisions, risks = (lines(r["notes"][k]) for k in NOTE_KEYS)
        node = {"id": r["id"], "num": r["num"], "group": r["group"] if r["group"] in known else "", "title": r["title"], "summary": r["description"],
                "status": "done" if "closed" in r else STATUS_OF.get(r["state"], "none"), "solved": solved or lines(r["done"]), "decisions": decisions or lines(r["missing"]),
                "risks": risks}
        if r["meta"].get("branch"):
            node["branch"] = True
        if r["links"].get("git_branch"):
            node["git_branch"] = r["links"]["git_branch"]
        if r["links"].get("commits"):
            node["commits"] = list(r["links"]["commits"])
        sub = sorted(kids.get(r["id"], []), key=lambda k: k["order"])
        if sub:
            node["subtasks"] = [{"title": k["title"], "status": STATUS_OF.get(k["state"], "none")} for k in sub]
        nodes.append(node)
    used = {n["group"] for n in nodes}
    out_groups = [{"id": g["id"], "title": g["title"]} for g in groups]
    if "" in used:
        out_groups.append({"id": "", "title": NO_GROUP})
    order = {g["id"]: i for i, g in enumerate(out_groups)}
    nodes.sort(key=lambda n: (order.get(n["group"], 99), n["num"] or 10 ** 6))
    return {"schema": SCHEMA, "project": board.project.name, "title": (meta or {}).get("title") or board.project.name, "intro": (meta or {}).get("intro") or "",
            "stamp": "", "groups": out_groups, "nodes": nodes}


def _same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    keys = ("schema", "project", "title", "intro", "groups", "nodes")
    return all(a.get(k) == b.get(k) for k in keys)


def write(board: TaskBoard, folder: Optional[Path] = None) -> Dict[str, Any]:
    """Writes `docs/roadmap.json` and `docs/roadmap.md` from the register; a file whose content would not change is left as it is (so that git sees no change)."""
    path = _paths(board, folder)
    old = _read_json(path["json"])
    data = export(board, old)
    data["stamp"] = (old or {}).get("stamp", "") if old and _same(old, data) else time.strftime("%d/%m/%Y %H:%M")
    path["folder"].mkdir(parents=True, exist_ok=True)
    changed = not (old and _same(old, data))
    if changed or not path["md"].exists():
        path["json"].write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        path["md"].write_text(to_markdown(data), encoding="utf-8")
    return {"json": str(path["json"]), "md": str(path["md"]), "nodes": len(data["nodes"]), "groups": len(data["groups"]), "changed": changed}


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


# -- the map -> the register ----------------------------------------------------------------------------------------

def load(board: TaskBoard, folder: Optional[Path] = None) -> Dict[str, Any]:
    """Reads `docs/roadmap.json` into the register: a group or a node that is already there (same id) is updated, any other is made. A `done` node goes to the history."""
    path = _paths(board, folder)
    data = _read_json(path["json"])
    if data is None:
        raise TaskError(f"{path['json']} is not there, or is not a roadmap file.")
    made = updated = 0
    now = time.time()

    def work(doc: Dict[str, Any]) -> None:
        nonlocal made, updated
        gids = {g["id"] for g in doc["groups"]}
        for g in data.get("groups") or []:
            if g.get("id") and g["id"] not in gids and isinstance(g.get("title"), str):
                doc["groups"].append({"id": g["id"], "title": g["title"], "order": 1 + max([x["order"] for x in doc["groups"]] + [0])})
                gids.add(g["id"])
            elif g.get("id") in gids:
                next(x for x in doc["groups"] if x["id"] == g["id"])["title"] = g["title"]
        for i, n in enumerate(data.get("nodes") or []):
            if not isinstance(n, dict) or not isinstance(n.get("title"), str):
                continue
            task_id = str(n.get("id") or f"rm{n.get('num') or i + 1}")
            done = n.get("status") == "done"
            fields = {"title": n["title"], "description": str(n.get("summary") or ""), "num": int(n.get("num") or 0), "group": n.get("group") if n.get("group") in gids else "",
                      "state": "queued" if done else STATE_OF.get(n.get("status"), "queued"), "notes": {k: "\n".join(n.get(k) or []) for k in NOTE_KEYS},
                      "meta": {"branch": True} if n.get("branch") else {}}
            git = git_links(n.get("git_branch"), n.get("commits"))
            row = next((r for r in doc["tasks"] + doc["history"] if r["id"] == task_id), None)
            if row is not None:
                row.update(fields)
                if "git_branch" in n or "commits" in n:                 # what the file does not say about the branch and the commits is kept
                    row["links"] = {**row["links"], **git}
                updated += 1
                if done and "closed" not in row:                       # closed in the file, still open here
                    doc["tasks"].remove(row)
                    doc["history"].append({**row, "closed": now + i / 1000, "closed_by": "user"})
                elif not done and "closed" in row:                     # open again in the file
                    doc["history"].remove(row)
                    row.pop("closed", None), row.pop("closed_by", None)
                    doc["tasks"].append(row)
                continue
            base = {"id": task_id, "parent": "", "order": 10 ** 6 + i, "done": "", "missing": "", "hidden": False, "links": {"session": "", "plan": "", "files": [], **git},
                    "by": "user", "created": now, "updated": now, **fields}
            if done:
                doc["history"].append({**base, "closed": now + i / 1000, "closed_by": "user"})
            else:
                doc["tasks"].append(base)
                for k, sub in enumerate(n.get("subtasks") or []):
                    if isinstance(sub, dict) and isinstance(sub.get("title"), str):
                        doc["tasks"].append({**base, "id": f"{task_id}.{k + 1}", "parent": task_id, "order": k + 1, "title": sub["title"], "description": "", "num": 0, "group": "",
                                             "state": STATE_OF.get(sub.get("status"), "queued"), "notes": {key: "" for key in NOTE_KEYS}, "meta": {}})
            made += 1
    board._change(work, doc=True)
    return {"made": made, "updated": updated}


# -- the map as text and as a page ----------------------------------------------------------------------------------

def ordered(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The nodes by group (in the order of the groups) and then by number, whatever order the file lists them in."""
    order = {g["id"]: i for i, g in enumerate(data.get("groups") or [])}
    return sorted(data.get("nodes") or [], key=lambda n: (order.get(n["group"], 99), n["num"] or 10 ** 6))


def to_markdown(data: Dict[str, Any]) -> str:
    out = [f"# {data.get('title') or data.get('project') or 'Roadmap'}", ""]
    if data.get("intro"):
        out += [data["intro"], ""]
    if data.get("stamp"):
        out += [f"_Aggiornato il {data['stamp']}. Generato da `docs/roadmap.json`: non si modifica a mano._", ""]
    titles = {g["id"]: g["title"] for g in data.get("groups") or []}
    count: Dict[str, int] = {}
    for n in data.get("nodes") or []:
        count[n["status"]] = count.get(n["status"], 0) + 1
    out += ["| Stato | Nodi |", "|---|---|"] + [f"| {LABEL[s]} | {count[s]} |" for s in LABEL if s in count] + [""]
    last = None
    for n in ordered(data):
        if n["group"] != last:
            out += [f"## {titles.get(n['group'], NO_GROUP)}", ""]
            last = n["group"]
        out += [f"### {n['num']}. {n['title']} ({LABEL.get(n['status'], n['status'])})", ""]
        if n.get("summary"):
            out += [n["summary"], ""]
        for key, head in SECTIONS:
            if n.get(key):
                out += [f"**{head}**", ""] + [f"- {x}" for x in n[key]] + [""]
        if n.get("git_branch") or n.get("commits"):
            out += ["**Ramo e commit**: " + " · ".join(([f"ramo `{n['git_branch']}`"] if n.get("git_branch") else []) + [f"`{c[:8]}`" for c in n.get("commits") or []]), ""]
        if n.get("subtasks"):
            out += ["**Sottotask**", ""] + [f"- {s['title']} ({LABEL.get(s['status'], s['status'])})" for s in n["subtasks"]] + [""]
    return "\n".join(out).rstrip() + "\n"


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def missing_list(data: Dict[str, Any], limit: int = 8) -> str:
    """The "what is missing now" block of the page: the nodes that are at work, waiting, to commit or to decide, one line each."""
    rows = [n for n in data.get("nodes") or [] if n["status"] in ("run", "review", "wait", "decision")][:limit]
    colour = {"run": "run", "review": "review", "wait": "wait", "decision": "decision"}
    return "\n".join(f'      <li><span class="dot" style="background:var(--status-{colour[n["status"]]})"></span><span class="t"><b>{html.escape(n["title"])}</b> '
                     f'{html.escape(n.get("summary") or "")}</span></li>' for n in rows) or '      <li><span class="t">Niente in sospeso.</span></li>'


def render_page(data: Dict[str, Any], template: Optional[str] = None) -> str:
    """The page for the Artifact: the template with the map in it."""
    text = template if template is not None else TEMPLATE.read_text(encoding="utf-8")
    titles = {g["id"]: g["title"] for g in data.get("groups") or []}
    nodes = []
    for n in ordered(data):
        sub = [f"Sottotask: {s['title']} ({LABEL.get(s['status'], s['status'])})" for s in n.get("subtasks") or []]
        nodes.append({"g": titles.get(n["group"], NO_GROUP), "num": str(n["num"]), "status": n["status"], "title": n["title"], "summary": n.get("summary") or "",
                      "done": n.get("solved") or [], "open": (n.get("decisions") or []) + sub, "future": n.get("risks") or [], "branch": bool(n.get("branch"))})
    stamp = f"Stato al {data['stamp']}" if data.get("stamp") else ""
    for key, value in (("{{TITLE}}", html.escape(data.get("title") or "Roadmap")), ("{{INTRO}}", html.escape(data.get("intro") or "")), ("{{STAMP}}", html.escape(stamp)),
                       ("{{MISSING}}", missing_list(data)), ("/*NODES*/[]", _json_for_script(nodes))):
        if key not in text:
            raise TaskError(f"The page template has no {key}.")
        text = text.replace(key, value)
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m tadamoo.roadmap", description=__doc__.split("\n\n")[1])
    ap.add_argument("cmd", choices=("write", "load", "render"))
    ap.add_argument("--project", default=".", help="the project folder (default: this one)")
    ap.add_argument("--page", help="render: also write the page for the Artifact to this file")
    a = ap.parse_args(argv)
    project = Path(a.project).resolve()
    try:
        if a.cmd == "render":
            data = _read_json(project / FOLDER / JSON_FILE)
            if data is None:
                raise TaskError(f"{project / FOLDER / JSON_FILE} is not there, or is not a roadmap file.")
            (project / FOLDER / MD_FILE).write_text(to_markdown(data), encoding="utf-8")
            if a.page:
                Path(a.page).write_text(render_page(data), encoding="utf-8")
            print(json.dumps({"nodes": len(data["nodes"]), "page": a.page or ""}))
            return 0
        from serve.userdata import user_data_dir
        board = TaskBoard(project, user_data_dir() / "planner" / "plans")
        print(json.dumps(write(board) if a.cmd == "write" else load(board), ensure_ascii=False))
    except TaskError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
