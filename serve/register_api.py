"""The page's side of the register of open work (Tasks tab, `/tasks`): tadamoo/taskboard.py for the folders the person added and the Chat's own folder.

    GET  /api/register?project=<folder>|all&state=<state>&hidden=1&session=<id>
                                     the open tasks, a tree for each project, with plan lines read from their plans and the groups; the hidden nodes only with hidden=1;
                                     only the ones a conversation carries with session=
    GET  /api/register/history?project=<folder>|all&session=<id>   the closed tasks, the newest first
    POST /api/register/add           {project, title, description?, parent?, state?, subtasks?, group?, solved?, decisions?, risks?}
    POST /api/register/update        {project, id, title?, description?, state?, done?, missing?, files?, group?, solved?, decisions?, risks?}
    POST /api/register/close         {project, id}                 the task is done: it leaves the open work and goes to the history
    POST /api/register/delete        {project, id}                 removes the task (or a row of the history) for good, with its subtasks
    POST /api/register/reopen        {project, id}                 a closed task goes back among the open ones
    POST /api/register/hide | show   {project, id}                 the node leaves the person's views (and comes back); nothing is lost
    POST /api/register/group-add | group-rename | group-delete   {project, title?, id?}
    POST /api/register/roadmap-write {project}                     docs/roadmap.json and docs/roadmap.md are written from the register
    POST /api/register/roadmap-load  {project}                     docs/roadmap.json is read into the register (what has the same id is updated)
    POST /api/register/move          {project, id, before?, parent?}
    POST /api/register/subtasks      {project, id, subtasks}       the subtasks become exactly this list
    POST /api/register/link-plan     {project, id, plan_id}        the plan commands the task from now on
    POST /api/register/resume        {project, id}                 the words that start a conversation on the task; nothing is changed until the agent takes it

A project must be a folder the person added (or the Chat's): nothing else is written. What the person changes is marked as theirs."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tadamoo.taskboard import ALL_STATES, NOTE_KEYS, STATES, TaskBoard, TaskError

RESUME = ("Resume the task {id} (\"{title}\"): call task_resume with that id, read what is done and what is missing, and carry on from there. "
          "Keep the register true as you go, and close the task when it is finished.")
RESUME_PLAN = "Resume the plan \"{plan}\" ({plan_id}) that commands the task {id}: look at the plan (plan_get) and go on with its next step."


class RegisterApi:
    def __init__(self, projects: Callable[[], List[Dict[str, str]]], plans_dir: Any, on_close: Optional[Callable[[Dict[str, Any]], Any]] = None) -> None:
        """`projects()` gives `[{path, name}]`: the folders the person added and the Chat's. `on_close(row)` is told of a task the person deleted (one another session
        assigned sends its result back)."""
        self.projects, self.plans_dir, self.on_close = projects, plans_dir, on_close

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

    def overview(self, project: str = "all", state: str = "", hidden: bool = False, session: Optional[str] = None) -> Dict[str, Any]:
        if state and state not in ALL_STATES:
            raise TaskError(f"state must be one of {', '.join(ALL_STATES)}.")
        known = self._known()
        chosen = known if project in ("", "all") else [p for p in known if os.path.normcase(p["path"]) == os.path.normcase(project)]
        if project not in ("", "all") and not chosen:
            raise TaskError("That is not one of the folders of the Agentic coding tab.")
        rows = []
        for p in chosen:
            board = TaskBoard(p["path"], self.plans_dir)
            every_row = board.view(state=state, session=session)
            tasks = every_row if hidden else [r for r in every_row if not r["hidden"]]
            rows.append({**{k: v for k, v in p.items() if k != "optional"}, "tasks": tasks, "groups": board.groups(), "hidden": sum(1 for r in every_row if r["hidden"]),
                         "closed": len(board.history(session=session, limit=10 ** 6))})
        rows = [r for r, p in zip(rows, chosen) if r["tasks"] or r["closed"] or r["groups"] or not p.get("optional")]                 # the data folder's register is shown only when it holds something
        every = [t for r in rows for t in r["tasks"]]
        return {"projects": rows, "states": list(ALL_STATES), "counts": {s: sum(1 for t in every if t["state"] == s) for s in ALL_STATES},
                "waiting": sum(1 for t in every if t["state"] == "waiting"), "hidden": sum(r["hidden"] for r in rows)}

    def history(self, project: str = "all", session: Optional[str] = None) -> Dict[str, Any]:
        known = self._known()
        chosen = known if project in ("", "all") else [p for p in known if os.path.normcase(p["path"]) == os.path.normcase(project)]
        if project not in ("", "all") and not chosen:
            raise TaskError("That is not one of the folders of the Agentic coding tab.")
        return {"projects": [{**{k: v for k, v in p.items() if k != "optional"}, "history": TaskBoard(p["path"], self.plans_dir).history(session=session)} for p in chosen]}

    def route(self, method: str, path: str, query: Dict[str, str], req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        action = path[len("/api/register"):].strip("/")
        try:
            session = query.get("session") if query.get("session") else None
            if method == "GET" and not action:
                return 200, self.overview(query.get("project", "all"), query.get("state", ""), query.get("hidden") in ("1", "true"), session)
            if method == "GET" and action == "history":
                return 200, self.history(query.get("project", "all"), session)
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
            notes = {k: text(k, "") for k in NOTE_KEYS}
            new = board.add(text("title", ""), text("description", ""), text("parent", ""), text("state", "queued"), "", "user", req.get("subtasks") if isinstance(req.get("subtasks"), list) else None,
                            [f for f in req["files"] if isinstance(f, str)] if isinstance(req.get("files"), list) else None, text("done", ""), text("missing", ""),
                            text("group", ""), notes)
            return {"id": new, "task": board.get(new)}
        if action == "update":
            fields = {k: text(k) for k in ("title", "description", "state", "done", "missing", "group", *NOTE_KEYS)}
            if isinstance(req.get("files"), list):
                fields["files"] = req["files"]
            board.update(task_id, "user", **fields)
            return {"task": board.get(task_id)}
        if action in ("close", "delete"):
            open_row = next((r for r in board.view() if r["id"] == task_id), None)
            gone = board.close(task_id, "user") if action == "close" else board.delete(task_id)
            if open_row is not None and self.on_close is not None:
                self.on_close(open_row)
            return {"closed" if action == "close" else "deleted": gone}
        if action == "reopen":
            return {"reopened": board.reopen(task_id, "user")}
        if action in ("hide", "show"):
            board.hide(task_id, action == "hide", "user")
            return {"task": board.get(task_id)}
        if action == "group-add":
            return {"id": board.add_group(text("title", "")), "groups": board.groups()}
        if action == "group-rename":
            board.rename_group(task_id, text("title", ""))
            return {"groups": board.groups()}
        if action == "group-delete":
            board.delete_group(task_id)
            return {"groups": board.groups()}
        if action == "roadmap-write":
            from tadamoo import roadmap
            return roadmap.write(board)
        if action == "roadmap-load":
            from tadamoo import roadmap
            return roadmap.load(board)
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
