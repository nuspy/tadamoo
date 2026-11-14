"""The page's side of the register of open work (Tasks tab, `/tasks`): tadamoo/taskboard.py for the folders the person added and the Chat's own folder.

    GET  /api/register?project=<folder>|all&state=<state>      the open tasks, a tree for each project, with plan lines read from their plans
    POST /api/register/add           {project, title, description?, parent?, state?, subtasks?}
    POST /api/register/update        {project, id, title?, description?, state?, done?, missing?, files?}
    POST /api/register/delete        {project, id}                 closes the task: it is deleted with its subtasks
    POST /api/register/move          {project, id, before?, parent?}
    POST /api/register/subtasks      {project, id, subtasks}       the subtasks become exactly this list
    POST /api/register/link-plan     {project, id, plan_id}        the plan commands the task from now on
    POST /api/register/resume        {project, id}                 the words that start a conversation on the task; nothing is changed until the agent takes it

A project must be a folder the person added (or the Chat's): nothing else is written. What the person changes is marked as theirs."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from tadamoo.taskboard import STATES, TaskBoard, TaskError

RESUME = ("Resume the task {id} (\"{title}\"): call task_resume with that id, read what is done and what is missing, and carry on from there. "
          "Keep the register true as you go, and close the task when it is finished.")
RESUME_PLAN = "Resume the plan \"{plan}\" ({plan_id}) that commands the task {id}: look at the plan (plan_get) and go on with its next step."


class RegisterApi:
    def __init__(self, projects: Callable[[], List[Dict[str, str]]], plans_dir: Any) -> None:
        """`projects()` gives `[{path, name}]`: the folders the person added and the Chat's."""
        self.projects, self.plans_dir = projects, plans_dir

    def _known(self) -> List[Dict[str, str]]:
        seen, out = set(), []
        for p in self.projects():
            key = os.path.normcase(str(p["path"]))
            if key not in seen and Path(p["path"]).is_dir():
                seen.add(key)
                out.append({"path": str(p["path"]), "name": p.get("name") or Path(p["path"]).name, **({"optional": True} if p.get("optional") else {})})
        return out

    def _board(self, project: Any) -> TaskBoard:
        for p in self._known():
            if os.path.normcase(p["path"]) == os.path.normcase(str(project or "")):
                return TaskBoard(p["path"], self.plans_dir)
        raise TaskError("That is not one of the folders of the Agentic coding tab.")

    def overview(self, project: str = "all", state: str = "") -> Dict[str, Any]:
        if state and state not in STATES:
            raise TaskError(f"state must be one of {', '.join(STATES)}.")
        known = self._known()
        chosen = known if project in ("", "all") else [p for p in known if os.path.normcase(p["path"]) == os.path.normcase(project)]
        if project not in ("", "all") and not chosen:
            raise TaskError("That is not one of the folders of the Agentic coding tab.")
        rows = [{**{k: v for k, v in p.items() if k != "optional"}, "tasks": TaskBoard(p["path"], self.plans_dir).view(state=state)} for p in chosen]
        rows = [r for r, p in zip(rows, chosen) if r["tasks"] or not p.get("optional")]                     # the data folder's register is shown only when it holds something
        every = [t for r in rows for t in r["tasks"]]
        return {"projects": rows, "states": list(STATES), "counts": {s: sum(1 for t in every if t["state"] == s) for s in STATES}, "waiting": sum(1 for t in every if t["state"] == "waiting")}

    def route(self, method: str, path: str, query: Dict[str, str], req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        action = path[len("/api/register"):].strip("/")
        try:
            if method == "GET" and not action:
                return 200, self.overview(query.get("project", "all"), query.get("state", ""))
            if method != "POST":
                return 404, {"error": {"message": "not found"}}
            return 200, self._post(action, req)
        except TaskError as exc:
            return 400, {"error": {"type": "register", "message": str(exc)}}

    def _post(self, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        board = self._board(req.get("project"))
        task_id = str(req.get("id") or "")
        text = lambda key, default=None: req[key] if isinstance(req.get(key), str) else default          # noqa: E731
        if action == "add":
            new = board.add(text("title", ""), text("description", ""), text("parent", ""), text("state", "queued"), "", "user", req.get("subtasks") if isinstance(req.get("subtasks"), list) else None,
                            [f for f in req["files"] if isinstance(f, str)] if isinstance(req.get("files"), list) else None, text("done", ""), text("missing", ""))
            return {"id": new, "task": board.get(new)}
        if action == "update":
            fields = {k: text(k) for k in ("title", "description", "state", "done", "missing")}
            if isinstance(req.get("files"), list):
                fields["files"] = req["files"]
            board.update(task_id, "user", **fields)
            return {"task": board.get(task_id)}
        if action == "delete":
            return {"deleted": board.close(task_id)}
        if action == "move":
            board.move(task_id, text("before", ""), req.get("parent") if isinstance(req.get("parent"), str) else None, "user")
            return {"ok": True}
        if action == "subtasks":
            if not isinstance(req.get("subtasks"), list):
                raise TaskError("Send `subtasks` as a list.")
            return {"ids": board.set_subtasks(task_id, req["subtasks"], "", "user")}
        if action == "link-plan":
            board.link_plan(task_id, text("plan_id", ""), "user")
            return {"task": board.get(task_id)}
        if action == "resume":
            row = board.get(task_id)
            plan = row.get("plan")
            words = RESUME_PLAN.format(plan=plan["title"], plan_id=row["links"]["plan"], id=task_id) if plan else RESUME.format(id=task_id, title=row["title"])
            return {"text": words, "project": str(board.project), "task": row}
        raise TaskError("not found")
