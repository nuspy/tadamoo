"""The `task_*` tools: how the agent keeps the register of its open work (tadamoo/taskboard.py).

The register is for free work in a conversation: a long job with several steps, or work that can be interrupted and has to survive a compaction, a cleared
conversation or a restart. It is not for a short question, and not for what a plan already commands: a plan is its own list of steps. The agent writes it
freely: when a subtask is no longer current, is wrong or has to be done another way, it changes it. Closing a task takes it out of the open work (the history keeps it), so what is open stays short.

The tools are in the session's own group of the tool list (after the fixed ones), so they never move the cached prefix. They write only the harness's own
file in the project folder (`.tadamoo/tasks.json`)."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from tadamoo.taskboard import STATES, TaskBoard, TaskError
from tadamoo.tools.registry import EffectClass, ToolRegistry

SUB = {"type": "object", "description": "A subtask; it may hold subtasks of its own",
       "properties": {"id": {"type": "string", "description": "Only to keep a subtask that is already there (task_subtasks)"}, "title": {"type": "string"},
                      "description": {"type": "string"}, "state": {"type": "string", "enum": list(STATES)}, "done": {"type": "string"}, "missing": {"type": "string"},
                      "subtasks": {"type": "array", "items": {"type": "object"}}},
       "required": ["title"]}


def closing_text(row: Dict[str, Any], result: str, by: str) -> str:
    """The message that tells whoever assigned a task that it is closed, with what came of it."""
    said = " ".join(str(result or row.get("done") or "").split())
    return f'The task you assigned to me is closed ({by}): "{row["title"]}".' + (f" Result: {said}" if said else " No result was written.")


class TaskTools:
    def __init__(self, board: TaskBoard, session_id: Callable[[], str]) -> None:
        self.board, self.session_id = board, session_id
        self.on_write: Optional[Callable[[], None]] = None            # the conversation learns that the change is its own, so it is not told of it as news
        self.on_assigned_close: Optional[Callable[[str, str, str], None]] = None     # (who assigned it, the task, the words): the result of a task another session assigned goes back

    def _wrote(self) -> None:
        if self.on_write is not None:
            self.on_write()

    def _mine(self) -> str:
        return self.session_id() or ""


def register_task_tools(registry: ToolRegistry, board: TaskBoard, session_id: Callable[[], str]) -> TaskTools:
    tt = TaskTools(board, session_id)
    registry.task_tools = tt                                                   # type: ignore[attr-defined]

    def add(title: str, description: str = "", subtasks: Optional[List[Dict[str, Any]]] = None, parent: str = "", state: str = "queued", files: Optional[List[str]] = None,
            done: str = "", missing: str = "") -> str:
        task_id = board.add(title, description, parent, state, tt._mine(), "agent", subtasks, files, done, missing)
        tt._wrote()
        return f"Task {task_id} opened.\n" + board.render(board.subtree(task_id), False)

    def update(id: str, title: Optional[str] = None, description: Optional[str] = None, state: Optional[str] = None, done: Optional[str] = None,
               missing: Optional[str] = None, files: Optional[List[str]] = None) -> str:
        row = board.update(id, "agent", title=title, description=description, state=state, done=done, missing=missing, files=files)
        tt._wrote()
        return f"Task {row['id']} updated: [{row['state']}] {row['title']}"

    def subtasks(id: str, subtasks: List[Dict[str, Any]]) -> str:
        ids = board.set_subtasks(id, subtasks, tt._mine())
        tt._wrote()
        return f"Task {id} now has {len(ids)} subtasks: {', '.join(ids)}"

    def move(id: str, before: str = "", parent: Optional[str] = None) -> str:
        board.move(id, before, parent)
        tt._wrote()
        return f"Task {id} moved."

    def close(id: str, result: str = "") -> str:
        row = next((r for r in board.view() if r["id"] == id), None)
        gone = board.close(id, "agent")
        tt._wrote()
        said = f"Closed (kept in the history): {', '.join(gone)}."
        giver = ((row or {}).get("meta") or {}).get("assigned_by")
        if giver and tt.on_assigned_close is not None:                         # a task another session assigned: what came of it goes back to them by itself
            try:
                tt.on_assigned_close(giver["id"], id, closing_text(row, result, "closed by me"))
                said += f' The result was sent to "{giver["name"]}".'
            except Exception as exc:
                said += f' The result could not be sent to "{giver["name"]}": {exc}'
        return said

    def lst(scope: str = "mine", state: str = "") -> str:
        if scope not in ("mine", "project"):
            raise TaskError("scope is 'mine' (this conversation's tasks) or 'project' (every open task of this project).")
        rows = board.view(session=tt._mine() if scope == "mine" else None, state=state)
        return board.render(rows, True) if rows else "No open tasks" + (" in this conversation." if scope == "mine" else " in this project.")

    def next_(scope: str = "mine") -> str:
        row = board.next_task(tt._mine())
        return board.render([row], True) if row else "Nothing is queued in this conversation."

    def resume(id: str) -> str:
        board.take(id, tt._mine())
        tt._wrote()
        row = board.get(id)
        tree = board.subtree(id)
        head = f'Task {id} is now carried by this conversation. Read what is done and what is missing, then continue; keep the register true as you go.\n'
        if row.get("plan"):
            head += f'It is commanded by the plan "{row["plan"]["title"]}" ({row["links"]["plan"]}): go on with the plan (plan_get, plan_step_start), not with subtasks.\n'
        return head + board.render(tree, True)

    def link_plan(id: str, plan_id: str) -> str:
        board.link_plan(id, plan_id)
        tt._wrote()
        return f"Task {id} is now one line for the plan {plan_id}: its state and progress are the plan's, and its subtasks are gone."

    P = {"type": "object", "properties": {}}
    reg = lambda name, desc, params, fn: registry.register(name, desc, params, EffectClass.PURE, fn)          # noqa: E731
    reg("task_add", "Opens a task in the register of this project's open work: for a LONG job (several steps, or work that may be interrupted), never for a short question or "
        "a one-step request, and not for work a plan already commands. Give it the subtasks you can see now (they may hold subtasks of their own); you can change them later. "
        "The register survives a compaction, a cleared conversation and a restart; a closed task leaves it for the history, so keep only what is open.",
        {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string", "description": "What the task is and what finished means"},
                                          "subtasks": {"type": "array", "items": SUB}, "parent": {"type": "string", "description": "The id of a task to make this a subtask of"},
                                          "state": {"type": "string", "enum": list(STATES)}, "files": {"type": "array", "items": {"type": "string"}},
                                          "done": {"type": "string"}, "missing": {"type": "string"}}, "required": ["title"]}, add)
    reg("task_update", "Changes a task: its title, description, state (queued, running, waiting for the person, blocked), what is done so far, what is still missing, its files. "
        "Keep done and missing true as you work: they are what you (or another conversation) will read to resume.",
        {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}, "state": {"type": "string", "enum": list(STATES)},
                                          "done": {"type": "string"}, "missing": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}}, "required": ["id"]}, update)
    reg("task_subtasks", "Makes the subtasks of a task exactly this list, in this order. Rewrite them freely when one is no longer current, is wrong or has to be done another "
        "way: an item with the id of a present subtask keeps it, any other is new, and a present one you leave out is removed.",
        {"type": "object", "properties": {"id": {"type": "string"}, "subtasks": {"type": "array", "items": SUB}}, "required": ["id", "subtasks"]}, subtasks)
    reg("task_move", "Reorders: puts a task before another one (`before`), or under a parent (`parent`, an id; empty for the top level).",
        {"type": "object", "properties": {"id": {"type": "string"}, "before": {"type": "string"}, "parent": {"type": "string"}}, "required": ["id"]}, move)
    reg("task_close", "Closes a task that is finished (or no longer wanted): it leaves the open work with its subtasks (the history keeps it). Check the result against what was asked first. A task another session "
        "assigned to you (it says who in task_list): give the `result`, and it goes back to them by itself.",
        {"type": "object", "properties": {"id": {"type": "string"}, "result": {"type": "string", "description": "what came of it, for whoever assigned the task"}}, "required": ["id"]}, close)
    reg("task_list", "Lists the open tasks with their notes: this conversation's own (`mine`) or all of the project's (`project`, to find a task to take over), optionally by state.",
        {"type": "object", "properties": {"scope": {"type": "string", "enum": ["mine", "project"]}, "state": {"type": "string", "enum": list(STATES)}}}, lst)
    reg("task_next", "The next queued task or subtask of this conversation, in order.", P, next_)
    reg("task_resume", "Takes over a task (yours after a cleared conversation, or another conversation's): it is handed to you with its subtasks and its notes, and you continue from what "
        "is missing. Use it when the person asks to resume a task, or when your summary of open tasks shows work that is not finished.",
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, resume)
    reg("task_link_plan", "Hands a task to a plan you made (plan_create): from then on the plan commands the work, the task is one line whose state and progress are read from the plan, "
        "and its own subtasks are dropped. Never copy a plan's steps into the register.",
        {"type": "object", "properties": {"id": {"type": "string"}, "plan_id": {"type": "string"}}, "required": ["id", "plan_id"]}, link_plan)
    return tt
