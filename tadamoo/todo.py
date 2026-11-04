"""`todo_write`: a small checklist the model keeps for the conversation.

The model sends the whole list each time; it replaces the one before. Items are `pending`, `in_progress` or `completed`, with at most one
in progress. The list lives with the conversation's tool registry, not in a file, and the tool reads and writes nothing else, so it is
safe at every permission level. The tool is in the fixed group at the start of the tool list, so it never changes the cached prefix.
Before a turn ends with items still open the loop reminds the model once.
"""
from __future__ import annotations

from typing import Any, Dict, List

from tadamoo.tools.registry import EffectClass, ToolRegistry

STATUSES = ("pending", "in_progress", "completed")
MAX_ITEMS = 50
MAX_CHARS = 200
MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


class TodoList:
    def __init__(self) -> None:
        self.items: List[Dict[str, str]] = []

    def write(self, todos: Any) -> str:
        """Replaces the list. Returns a short summary; a list that cannot be used raises ValueError and leaves the old one as it was."""
        if not isinstance(todos, list):
            raise ValueError("todos must be a list of {content, status} items.")
        if len(todos) > MAX_ITEMS:
            raise ValueError(f"A checklist holds at most {MAX_ITEMS} items; group the small ones.")
        items: List[Dict[str, str]] = []
        for i, raw in enumerate(todos, 1):
            if isinstance(raw, str):
                raw = {"content": raw}
            if not isinstance(raw, dict) or not isinstance(raw.get("content"), str) or not raw["content"].strip():
                raise ValueError(f"Item {i} needs a text in 'content'.")
            status = raw.get("status", "pending")
            if status not in STATUSES:
                raise ValueError(f"Item {i}: 'status' must be one of {', '.join(STATUSES)}.")
            items.append({"content": " ".join(raw["content"].split())[:MAX_CHARS], "status": status})
        if sum(1 for it in items if it["status"] == "in_progress") > 1:
            raise ValueError("Only one item can be in_progress at a time; mark the others pending or completed.")
        self.items = items
        done = sum(1 for it in items if it["status"] == "completed")
        busy = sum(1 for it in items if it["status"] == "in_progress")
        return f"Checklist saved: {done} done, {busy} in progress, {len(items) - done - busy} to do.\n{self.render()}".rstrip()

    def open_items(self) -> List[Dict[str, str]]:
        return [it for it in self.items if it["status"] != "completed"]

    def render(self, open_only: bool = False) -> str:
        return "\n".join(f"{MARKS[it['status']]} {it['content']}" for it in (self.open_items() if open_only else self.items))


def register_todo_tool(registry: ToolRegistry) -> TodoList:
    """Adds `todo_write` and keeps the list on the registry as `registry.todo`."""
    todo = TodoList()
    registry.todo = todo                                   # type: ignore[attr-defined]
    registry.register(
        "todo_write",
        "Keeps a short checklist for a task of several steps. Send the whole list every time: it replaces the last one. Each item has "
        "'content' and a 'status' of pending, in_progress or completed, with at most one in_progress. Update it as you finish items; "
        "do not use it for a task of one or two steps.",
        {"type": "object",
         "properties": {"todos": {"type": "array", "description": "The whole checklist, in order",
                                  "items": {"type": "object",
                                            "properties": {"content": {"type": "string", "description": "What to do, in a few words"},
                                                           "status": {"type": "string", "enum": list(STATUSES)}},
                                            "required": ["content", "status"]}}},
         "required": ["todos"]},
        EffectClass.PURE, lambda todos: todo.write(todos))
    return todo
