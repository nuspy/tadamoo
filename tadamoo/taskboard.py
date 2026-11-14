"""The register of the work the agent is carrying out now, one for each project folder.

It is not a history: a task that is done is closed, and closing deletes it (with its subtasks). What stays is what is open, so that work survives a
compaction, a cleared conversation, a restart of the PC and another conversation taking it over. The file is `<project>/.tadamoo/tasks.json`; the agent
(through the `task_*` tools, tadamoo/task_tools.py), the person (the Tasks tab, `/tasks`) and the server all write it, one at a time (a lock of the process
and a lock file across processes), the whole file replaced atomically.

A task has an id, a title, a description, a state (`queued`, `running`, `waiting` for the person, `blocked`), a parent (subtasks to any depth), an order among
its siblings, what is done so far and what is still missing, its links (the conversation that carries it, the plan that commands it, files), and who changed it
last and when. Where a plan exists the PLAN is the register: a task linked to a plan is one line, and its state, its progress and what is next are read from the
plan file each time and never stored; the nodes of a plan are never copied into the register, and a plan that has finished takes its line with it."""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tadamoo.audit import file_lock

FOLDER = ".tadamoo"
FILE = "tasks.json"
STATES = ("queued", "running", "waiting", "blocked")
WHO = ("agent", "user")
ID = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
REPORTED = {"running": "running", "in_progress": "running", "pending": "queued", "queued": "queued", "waiting": "waiting", "blocked": "blocked", "failed": "blocked"}
ENDS = frozenset({"done", "closed", "completed", "skipped"})
MAX_TASKS = 300
MAX_DEPTH = 10
TITLE, TEXT, NOTE = 200, 4000, 2000
SUMMARY_TOP, SUMMARY_LINE = 12, 140


class TaskError(ValueError):
    """What cannot be done, in words for the agent or the person."""


def _clip(value: Any, n: int) -> str:
    return " ".join(str(value or "").split())[:n] if n == TITLE else str(value or "").strip()[:n]


def plan_state(plans_dir: Optional[Path], plan_id: str) -> Optional[Dict[str, Any]]:
    """What a plan says about itself, read from its file: `{title, state, done, total, next}`; None when the plan is not there or has finished."""
    if not plans_dir or not plan_id:
        return None
    try:
        plan = json.loads((Path(plans_dir) / f"{plan_id}.json").read_text(encoding="utf-8"))["plan"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    nodes = [n for n in (plan.get("nodes") or {}).values() if isinstance(n, dict) and n.get("type") != "start"]
    open_nodes = [n for n in nodes if n.get("status") not in ("completed", "skipped")]
    if nodes and not open_nodes:
        return None
    mine = lambda status: [n for n in open_nodes if n.get("status") == status]        # noqa: E731
    state = ("waiting" if any(n.get("assignee") == "user" for n in mine("in_progress")) else "blocked" if mine("failed") or mine("blocked")
             else "running" if mine("in_progress") else "queued")
    return {"title": str(plan.get("title") or plan_id), "state": state, "done": len(nodes) - len(open_nodes), "total": len(nodes),
            "next": [str(n.get("title") or n.get("id")) for n in (mine("in_progress") or mine("pending"))[:3]]}


class TaskBoard:
    def __init__(self, project: str | Path, plans_dir: Optional[str | Path] = None) -> None:
        self.project = Path(project)
        self.path = self.project / FOLDER / FILE
        self.plans_dir = Path(plans_dir) if plans_dir else None
        self._lock = threading.RLock()

    # -- the file ---------------------------------------------------------------------------------------------

    def _read(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = data.get("tasks") if isinstance(data, dict) else None
        out = []
        for t in rows if isinstance(rows, list) else []:
            if isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("title"), str):
                out.append({"id": t["id"], "title": t["title"], "description": str(t.get("description") or ""), "state": t.get("state") if t.get("state") in STATES else "queued",
                            "parent": str(t.get("parent") or ""), "order": int(t.get("order") or 0), "done": str(t.get("done") or ""), "missing": str(t.get("missing") or ""),
                            "links": {"session": str((t.get("links") or {}).get("session") or ""), "plan": str((t.get("links") or {}).get("plan") or ""),
                                      "files": [str(f) for f in ((t.get("links") or {}).get("files") or []) if isinstance(f, str)][:30]},
                            "meta": t["meta"] if isinstance(t.get("meta"), dict) else {},
                            "by": t.get("by") if t.get("by") in WHO else "agent", "created": float(t.get("created") or 0), "updated": float(t.get("updated") or 0)})
        ids = {t["id"] for t in out}
        for t in out:
            if t["parent"] not in ids or t["parent"] == t["id"]:
                t["parent"] = ""
        return out

    def _save(self, tasks: List[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tasks.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"schema": "1.0", "tasks": tasks}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, str(self.path))
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _change(self, work: Any) -> Any:
        """Reads, lets `work(tasks)` change the list, renumbers the order of the siblings and writes; one writer at a time, in this process and across them."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, file_lock(self.path.parent / ".tasks.lock"):
            tasks = self._read()
            out = work(tasks)
            self._renumber(tasks)
            self._save(tasks)
            return out

    @staticmethod
    def _renumber(tasks: List[Dict[str, Any]]) -> None:
        seen: Dict[str, int] = {}
        for t in sorted(tasks, key=lambda x: (x["order"], x["created"])):
            seen[t["parent"]] = seen.get(t["parent"], 0) + 1
            t["order"] = seen[t["parent"]]

    # -- reading ----------------------------------------------------------------------------------------------

    @staticmethod
    def _by_id(tasks: List[Dict[str, Any]], task_id: str) -> Dict[str, Any]:
        found = next((t for t in tasks if t["id"] == str(task_id).strip()), None)
        if found is None:
            raise TaskError(f"There is no open task '{str(task_id)[:40]}': it may have been closed. task_list shows what is open.")
        return found

    @staticmethod
    def _children(tasks: List[Dict[str, Any]], parent: str) -> List[Dict[str, Any]]:
        return sorted((t for t in tasks if t["parent"] == parent), key=lambda t: t["order"])

    def sweep(self) -> List[str]:
        """Takes out the tasks whose plan has finished (or is gone) with what hangs from them; returns their ids."""
        with self._lock:
            if not any(t["links"]["plan"] for t in self._read()):
                return []
            gone: List[str] = []

            def work(tasks: List[Dict[str, Any]]) -> None:
                for t in [t for t in tasks if t["links"]["plan"] and plan_state(self.plans_dir, t["links"]["plan"]) is None]:
                    gone.extend(self._remove(tasks, t["id"]))
            self._change(work)
            return gone

    def view(self, session: Optional[str] = None, state: str = "") -> List[Dict[str, Any]]:
        """The open tasks in tree order, each with its depth and, for a task that a plan commands, the plan's own state and progress in place of its own."""
        self.sweep()
        tasks = self._read()
        out: List[Dict[str, Any]] = []

        def walk(parent: str, depth: int) -> None:
            for t in self._children(tasks, parent):
                row = {**t, "depth": depth, "children": len([c for c in tasks if c["parent"] == t["id"]]), "project": str(self.project)}
                if t["links"]["plan"]:
                    p = plan_state(self.plans_dir, t["links"]["plan"])
                    row["plan"] = p
                    if p:
                        row.update(state=p["state"], done=f"{p['done']}/{p['total']} steps", missing="next: " + "; ".join(p["next"]) if p["next"] else "")
                out.append(row)
                walk(t["id"], depth + 1)
        walk("", 0)
        if session is not None:
            mine = {r["id"] for r in out if r["links"]["session"] == session and not r["parent"]}
            keep = set(mine)
            for r in out:
                if r["parent"] in keep:
                    keep.add(r["id"])
            out = [r for r in out if r["id"] in keep]
        return [r for r in out if not state or r["state"] == state]

    def subtree(self, task_id: str) -> List[Dict[str, Any]]:
        """A task and what hangs from it, in tree order."""
        ids, out = {task_id}, []
        for r in self.view():
            if r["id"] == task_id or r["parent"] in ids:
                ids.add(r["id"])
                out.append(r)
        return out

    def get(self, task_id: str) -> Dict[str, Any]:
        return next(r for r in self.view() if r["id"] == self._by_id(self._read(), task_id)["id"])

    # -- changing ---------------------------------------------------------------------------------------------

    def _new(self, tasks: List[Dict[str, Any]], spec: Dict[str, Any], parent: str, session: str, by: str, depth: int, now: float, task_id: str = "") -> Dict[str, Any]:
        title = _clip(spec.get("title"), TITLE)
        if not title:
            raise TaskError("A task needs a title.")
        if depth > MAX_DEPTH:
            raise TaskError(f"Subtasks go at most {MAX_DEPTH} levels deep.")
        if len(tasks) >= MAX_TASKS:
            raise TaskError(f"The register holds at most {MAX_TASKS} open tasks: close the ones that are done.")
        state = spec.get("state") or "queued"
        if state not in STATES:
            raise TaskError(f"state must be one of {', '.join(STATES)}.")
        if not task_id:
            task_id = "t-" + secrets.token_hex(3)
            while any(t["id"] == task_id for t in tasks):
                task_id = "t-" + secrets.token_hex(3)
        task = {"id": task_id, "meta": {}, "title": title, "description": _clip(spec.get("description"), TEXT), "state": state, "parent": parent, "order": 10 ** 6 + len(tasks),
                "done": _clip(spec.get("done"), NOTE), "missing": _clip(spec.get("missing"), NOTE),
                "links": {"session": session, "plan": "", "files": [str(f) for f in (spec.get("files") or []) if isinstance(f, str)][:30]}, "by": by, "created": now, "updated": now}
        tasks.append(task)
        for sub in spec.get("subtasks") or []:
            if isinstance(sub, dict):
                self._new(tasks, sub, task_id, session, by, depth + 1, now)
        return task

    def _depth(self, tasks: List[Dict[str, Any]], task: Dict[str, Any]) -> int:
        n, cur = 0, task
        while cur["parent"] and n < 50:
            cur, n = self._by_id(tasks, cur["parent"]), n + 1
        return n

    def add(self, title: str, description: str = "", parent: str = "", state: str = "queued", session: str = "", by: str = "agent",
            subtasks: Optional[Sequence[Dict[str, Any]]] = None, files: Optional[Sequence[str]] = None, done: str = "", missing: str = "") -> str:
        """Opens a task (with its subtasks, to any depth); returns its id."""
        def work(tasks: List[Dict[str, Any]]) -> str:
            depth = self._depth(tasks, self._by_id(tasks, parent)) + 1 if parent else 0
            if parent and self._by_id(tasks, parent)["links"]["plan"]:
                raise TaskError("That task is commanded by a plan: its steps are the plan's, not subtasks.")
            return self._new(tasks, {"title": title, "description": description, "state": state, "subtasks": subtasks, "files": files, "done": done, "missing": missing},
                             parent, session, by, depth, time.time())["id"]
        return self._change(work)

    def update(self, task_id: str, by: str = "agent", **fields: Any) -> Dict[str, Any]:
        """Changes the title, description, state, done, missing, files or the conversation of a task. A task a plan commands takes no state of its own."""
        def work(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
            t = self._by_id(tasks, task_id)
            for key, value in fields.items():
                if value is None:
                    continue
                if key == "title":
                    if not _clip(value, TITLE):
                        raise TaskError("A task needs a title.")
                    t["title"] = _clip(value, TITLE)
                elif key == "description":
                    t["description"] = _clip(value, TEXT)
                elif key in ("done", "missing"):
                    t[key] = _clip(value, NOTE)
                elif key == "state":
                    if value not in STATES:
                        raise TaskError(f"state must be one of {', '.join(STATES)}.")
                    t["state"] = value
                elif key == "files":
                    t["links"]["files"] = [str(f) for f in value if isinstance(f, str)][:30]
                elif key == "session":
                    t["links"]["session"] = str(value)
                else:
                    raise TaskError(f"A task has no '{key}' to change.")
            t["by"], t["updated"] = by if by in WHO else "agent", time.time()
            return dict(t)
        return self._change(work)

    def set_subtasks(self, task_id: str, subtasks: Sequence[Dict[str, Any]], session: str = "", by: str = "agent") -> List[str]:
        """Makes the subtasks of a task exactly this list, in this order: an item with the `id` of a present subtask keeps it (and changes what it names), any other is new,
        and a present subtask that is not listed is closed with what hangs from it."""
        def work(tasks: List[Dict[str, Any]]) -> List[str]:
            parent = self._by_id(tasks, task_id)
            if parent["links"]["plan"]:
                raise TaskError("That task is commanded by a plan: its steps are the plan's, not subtasks.")
            depth, now, ids = self._depth(tasks, parent) + 1, time.time(), []
            keep = {s.get("id") for s in subtasks if isinstance(s, dict) and s.get("id")}
            for old in [c for c in self._children(tasks, parent["id"]) if c["id"] not in keep]:
                self._remove(tasks, old["id"])
            for n, spec in enumerate(subtasks, 1):
                if not isinstance(spec, dict):
                    raise TaskError("Each subtask is an object with a title.")
                cur = next((c for c in tasks if c["id"] == spec.get("id") and c["parent"] == parent["id"]), None)
                if cur is None:
                    cur = self._new(tasks, spec, parent["id"], session or parent["links"]["session"], by, depth, now)
                else:
                    for key in ("title", "description", "state", "done", "missing"):
                        if key in spec:
                            cur[key] = _clip(spec[key], TITLE if key == "title" else TEXT if key == "description" else NOTE) if key != "state" else (spec[key] if spec[key] in STATES else cur["state"])
                    cur["by"], cur["updated"] = by, now
                cur["order"] = n
                ids.append(cur["id"])
            return ids
        return self._change(work)

    def move(self, task_id: str, before: str = "", parent: Optional[str] = None, by: str = "agent") -> None:
        """Puts a task before another (in that one's family), at the end of a parent's subtasks (`parent`, "" for the top), or, with neither, at the end of its own."""
        def work(tasks: List[Dict[str, Any]]) -> None:
            t = self._by_id(tasks, task_id)
            target = self._by_id(tasks, before) if before else None
            new_parent = target["parent"] if target else (t["parent"] if parent is None else str(parent))
            if new_parent:
                holder = self._by_id(tasks, new_parent)
                if holder["links"]["plan"]:
                    raise TaskError("That task is commanded by a plan: its steps are the plan's, not subtasks.")
                if new_parent == t["id"] or t["id"] in self._ancestors(tasks, holder):
                    raise TaskError("A task cannot go inside itself.")
                if self._depth(tasks, holder) + 1 > MAX_DEPTH:
                    raise TaskError(f"Subtasks go at most {MAX_DEPTH} levels deep.")
            t["parent"] = new_parent
            t["order"] = (target["order"] - 0.5) if target else 10 ** 9
            t["by"], t["updated"] = by if by in WHO else "agent", time.time()
        self._change(work)

    def _ancestors(self, tasks: List[Dict[str, Any]], task: Dict[str, Any]) -> List[str]:
        out, cur = [], task
        while cur["parent"] and len(out) < 50:
            cur = self._by_id(tasks, cur["parent"])
            out.append(cur["id"])
        return out + [task["id"]]

    def _remove(self, tasks: List[Dict[str, Any]], task_id: str) -> List[str]:
        gone = {task_id}
        while True:
            more = {t["id"] for t in tasks if t["parent"] in gone} - gone
            if not more:
                break
            gone |= more
        tasks[:] = [t for t in tasks if t["id"] not in gone]
        return sorted(gone)

    def close(self, task_id: str) -> List[str]:
        """Closes a task: it is deleted, with its subtasks. Nothing is kept of it."""
        return self._change(lambda tasks: self._remove(tasks, self._by_id(tasks, task_id)["id"]))

    def link_plan(self, task_id: str, plan_id: str, by: str = "agent") -> Dict[str, Any]:
        """Hands a task to a plan: its subtasks are dropped (the plan's steps are the steps), and from then on the task is one line read from the plan."""
        if plan_state(self.plans_dir, plan_id) is None:
            raise TaskError(f"There is no running plan '{str(plan_id)[:40]}'.")

        def work(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
            t = self._by_id(tasks, task_id)
            for c in self._children(tasks, t["id"]):
                self._remove(tasks, c["id"])
            t["links"]["plan"], t["by"], t["updated"] = plan_id, by if by in WHO else "agent", time.time()
            return dict(t)
        return self._change(work)

    def take(self, task_id: str, session: str, by: str = "agent") -> Dict[str, Any]:
        """Another conversation (or this one, after a clear) takes the task over: it is now carried by `session`, and running unless it waits for the person or is blocked."""
        def work(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
            t = self._by_id(tasks, task_id)
            t["links"]["session"], t["by"], t["updated"] = session, by if by in WHO else "agent", time.time()
            if t["state"] == "queued":
                t["state"] = "running"
            return dict(t)
        return self._change(work)

    # -- what the programs report ----------------------------------------------------------------------------

    def report(self, task_id: str, status: str, result: str = "", children: Optional[Sequence[str]] = None, metadata: Optional[Dict[str, Any]] = None,
               session: str = "", by: str = "agent") -> Optional[Dict[str, Any]]:
        """The progress of a piece of work that something else carries out (an agent run, a target and its tasks, a large request and its parts), under the id the reporter chose.
        `running` and `pending` are states of the task; `blocked` and `failed` are blocked, with `result` as what is missing; `waiting` waits for the person; `done`, `closed` and
        `skipped` end it: the task is deleted with its subtasks (nothing is kept, and ending one that is not there is no error). For the others `result` is what is done so far.
        `metadata` gives the title (`title` or `goal`) and is kept under `meta`; `children` are the ids of its subtasks, which are put under it whenever they exist."""
        if not isinstance(task_id, str) or not ID.match(task_id):
            raise TaskError("A task id is 1-80 letters, digits, dot, dash, underscore or colon.")
        if status in ENDS:
            def end(tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
                if any(x["id"] == task_id for x in tasks):
                    self._remove(tasks, task_id)
                return None
            return self._change(end)
        if status not in REPORTED:
            raise TaskError(f"status must be one of {', '.join(sorted(set(REPORTED) | set(ENDS)))}.")
        meta = {k: v for k, v in (metadata or {}).items() if k not in ("title", "goal", "description") and isinstance(k, str) and len(json.dumps(v, default=str)) <= 1500}

        def work(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
            now, state = time.time(), REPORTED[status]
            t = next((x for x in tasks if x["id"] == task_id), None)
            title = _clip((metadata or {}).get("title") or (metadata or {}).get("goal") or (t or {}).get("title") or result or task_id, TITLE)
            if t is None:
                t = self._new(tasks, {"title": title}, "", session, by, 0, now, task_id)
            t["title"], t["state"], t["updated"], t["by"] = title, state, now, by if by in WHO else "agent"
            if (metadata or {}).get("description"):
                t["description"] = _clip(metadata["description"], TEXT)
            if status == "failed" or state == "blocked":
                t["missing"] = _clip(result, NOTE) or t["missing"]
            elif result:
                t["done"] = _clip(result, NOTE)
            t["meta"] = {**t.get("meta", {}), **meta}
            if children is not None:
                t["meta"]["children"] = [str(c) for c in children][:MAX_TASKS]
            if session and not t["links"]["session"]:
                t["links"]["session"] = session
            self._relink(tasks)
            return dict(t)
        return self._change(work)

    @staticmethod
    def _relink(tasks: List[Dict[str, Any]]) -> None:
        """A task that names its subtasks (`meta.children`) is their parent as soon as they exist, whichever was reported first."""
        by_id = {t["id"]: t for t in tasks}
        for t in tasks:
            for c in t.get("meta", {}).get("children", []):
                kid = by_id.get(c)
                if kid is not None and kid is not t and kid["parent"] != t["id"] and t["parent"] != kid["id"]:
                    kid["parent"] = t["id"]

    # -- what the agent is shown -----------------------------------------------------------------------------

    @staticmethod
    def line(row: Dict[str, Any], detail: bool = False) -> str:
        plan = row.get("plan")
        head = (f"{'  ' * row['depth']}- {row['id']} [{row['state']}] " + (f'Plan "{plan["title"]}" ({row["links"]["plan"]})' if plan else row["title"]))
        notes = [x for x in (("done: " + row["done"]) if row["done"] else "", ("missing: " + row["missing"]) if row["missing"] else "") if x]
        if detail:
            notes = [x for x in (("what: " + row["description"]) if row["description"] else "", *notes, ("files: " + ", ".join(row["links"]["files"])) if row["links"]["files"] else "") if x]
            return head + ("\n" + "\n".join(f"{'  ' * row['depth']}    {n}" for n in notes) if notes else "")
        return head + (" -- " + "; ".join(n[:SUMMARY_LINE] for n in notes) if notes else "")

    def render(self, rows: List[Dict[str, Any]], detail: bool = False) -> str:
        return "\n".join(self.line(r, detail) for r in rows)

    def summary(self, session: str) -> str:
        """The open tasks of this conversation in a few lines (the top ones and their subtasks' titles), the same text while nothing changed."""
        rows = self.view(session=session)
        if not rows:
            return ""
        lines, tops, show = [], 0, False
        for r in rows:
            if r["depth"] == 0:
                tops += 1
                show = tops <= SUMMARY_TOP
            if show and r["depth"] <= 2:
                lines.append(self.line(r))
        if tops > SUMMARY_TOP:
            lines.append(f"- and {tops - SUMMARY_TOP} more: task_list shows all of them")
        return "\n".join(lines)

    def signature(self, session: str) -> str:
        """What the conversation's own tasks are as stored (not what a plan's progress adds on reading), to tell whether someone changed them. Empty when there are none."""
        tasks = self._read()
        mine = {t["id"] for t in tasks if t["links"]["session"] == session and not t["parent"]}
        for t in tasks:
            if t["parent"] in mine:
                mine.add(t["id"])
        rows = sorted((t["id"], t["title"], t["state"], t["parent"], t["order"], t["done"], t["missing"], t["links"]["plan"]) for t in tasks if t["id"] in mine)
        return json.dumps(rows, ensure_ascii=False) if rows else ""

    def next_task(self, session: str) -> Optional[Dict[str, Any]]:
        """The first task or subtask of this conversation that is queued and has no unfinished subtask, in the order of the register."""
        rows = self.view(session=session)
        parents = {r["parent"] for r in rows}
        return next((r for r in rows if r["state"] == "queued" and r["id"] not in parents and not r["links"]["plan"]), None) or next((r for r in rows if r["state"] == "queued"), None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m tadamoo.taskboard [--project FOLDER] list | add TITLE | close ID | update ID [--state S] [--done T] [--missing T]`: the register from a shell."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m tadamoo.taskboard", description=main.__doc__)
    ap.add_argument("--project", default=".", help="the project folder (default: this one)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("add").add_argument("title")
    sub.add_parser("close").add_argument("id")
    up = sub.add_parser("update")
    up.add_argument("id")
    for name in ("state", "done", "missing", "title"):
        up.add_argument(f"--{name}")
    a = ap.parse_args(argv)
    try:
        from serve.userdata import user_data_dir
        board = TaskBoard(Path(a.project).resolve(), user_data_dir() / "planner" / "plans")
        if a.cmd == "list":
            print(board.render(board.view(), True) or "No open tasks.")
        elif a.cmd == "add":
            print(board.add(a.title, by="user"))
        elif a.cmd == "close":
            print("closed: " + ", ".join(board.close(a.id)))
        else:
            board.update(a.id, "user", title=a.title, state=a.state, done=a.done, missing=a.missing)
            print("updated")
    except TaskError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
