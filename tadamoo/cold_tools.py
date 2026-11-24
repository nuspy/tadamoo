"""Cold tools: the rare tools with a long definition are listed with one line each, and the model asks `tool_help` for the rest before it uses one.

The tool list is part of the prompt's prefix and a changed list would be read again, so it never changes during a conversation: a cold tool is
listed the same way from the first request to the last, with its first sentence and a loose schema. The full definition comes back as the
result of a `tool_help` call, at the end of the conversation, where it costs the cache nothing. A call is still checked against the real schema
(the grammar no longer forces the parameters of a cold tool; the registry refuses a wrong call and says to ask tool_help). Off unless asked
for (`--cold-tools`): which tools are worth it, and whether the models take it, is measured with an A/B (docs/cold-tools.md).
"""
from __future__ import annotations

from typing import Any, List

from tadamoo.tools.registry import EffectClass

COLD = ("docx_edit", "docx_create", "xlsx_edit", "xlsx_create", "pptx_edit", "pptx_create", "pdf_edit", "repo_diagram", "task_add", "task_subtasks", "task_update")


def apply_cold(registry: Any, names: tuple = COLD) -> List[str]:
    """Makes the tools of `names` that exist cold and adds tool_help; returns the cold tools."""
    cold = registry.set_cold(list(names))
    if not cold:
        return []

    def tool_help(name: str) -> str:
        tool = registry.get(str(name).strip())
        if tool is None or not tool.cold:
            return f"'{name}' is not a cold tool: its full definition is in the tool list. The cold tools: {', '.join(registry.cold_names())}."
        return registry.describe(tool.name)

    registry.register("tool_help", "Gives the whole definition (description and parameters) of a tool whose list entry says to call tool_help first. Ask it before you use that tool the first time.",
                      {"type": "object", "properties": {"name": {"type": "string", "description": "The tool's name"}}, "required": ["name"]},
                      EffectClass.PURE, tool_help)
    return cold
