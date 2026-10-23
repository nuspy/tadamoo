"""Tool definition and registry with effect classes.

Effect classes:
- PURE: read-only, deterministic, zero side effects
- REVERSIBLE: modifies files or local state, can be rolled back with checkpoints
- IRREVERSIBLE: executes external commands, network requests, or hardware calls
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class EffectClass(str, enum.Enum):
    PURE = "pure"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    effect: EffectClass
    handler: Callable[..., Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        effect: EffectClass,
        handler: Callable[..., Any],
    ) -> None:
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            effect=effect,
            handler=handler,
        )

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def schemas(self) -> List[Dict[str, Any]]:
        """Returns standard OpenAI/Basalt tool definitions list."""
        out = []
        for t in self._tools.values():
            out.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
                "effect": t.effect.value,
            })
        return out

    def execute(self, name: str, arguments: Dict[str, Any]) -> Any:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: '{name}'")
        return tool.handler(**arguments)
