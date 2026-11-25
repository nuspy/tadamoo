"""Cold tools: the rare tools with a long definition are listed with one line each, and the model asks `tool_help` for the rest before it uses one.

The tool list is part of the prompt's prefix and a changed list would be read again, so it never changes during a conversation: a cold tool is
listed the same way from the first request to the last, with its first sentence and the skeleton of its schema (the names and types of its
parameters, without descriptions: the grammar of a tool call writes only the parameters its schema names, and a free object let the model write
none). The full definition comes back as the result of a `tool_help` call, at the end of the conversation, where it costs the cache nothing. A call
is still checked against the real schema (the grammar no longer forces what is inside a parameter; the registry refuses a wrong call and says to ask tool_help). On unless
turned off (`--no-cold-tools`, Settings > General > Inference settings); which tools are cold is the skills' own choice, HotLoad or ColdLoad (Settings > Skills).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

from tadamoo.tools.registry import EffectClass

BASE = ("repo_diagram", "task_add", "task_subtasks", "task_update")      # cold whenever the option is on: no skill owns them


def cold_names(choices_file: Any = None) -> tuple:
    """BASE and the cold tools of every skill set to ColdLoad: the person's choice in `choices_file` (the skills' state in the user data folder), else the
    catalogue's own value. A tool that two skills list is cold only when both are."""
    from tadamoo.importer.common import Problem
    from tadamoo.skillmanager import catalogue
    try:
        entries = catalogue.load()
    except Problem:
        entries = []
    chosen: Any = {}
    if choices_file:
        try:
            chosen = json.loads(Path(choices_file).read_text(encoding="utf-8")).get("skills") or {}
        except (OSError, ValueError, AttributeError):
            chosen = {}
    votes: dict = {}
    for e in entries:
        mine = chosen.get(e["name"]) if isinstance(chosen, dict) else None
        load = (mine.get("load") if isinstance(mine, dict) else None) or e.get("load") or "hot"
        for tool in e.get("cold_tools") or []:
            votes.setdefault(tool, []).append(load == "cold")
    return BASE + tuple(tool for tool, cold in votes.items() if all(cold) and tool not in BASE)


def apply_cold(registry: Any, names: Optional[tuple] = None) -> List[str]:
    """Makes the tools of `names` (default: cold_names()) that exist cold and adds tool_help; returns the cold tools."""
    cold = registry.set_cold(list(cold_names() if names is None else names))
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
