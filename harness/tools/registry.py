"""Tool definition and registry with effect classes.

Effect classes:
- PURE: read-only, deterministic, zero side effects
- REVERSIBLE: modifies files or local state, can be rolled back with checkpoints
- IRREVERSIBLE: executes external commands, state-changing network requests, or hardware calls

A tool marked `untrusted` returns content from outside the user's control (web pages, mail,
third-party servers); its results are fenced as data and taint the session's permissions.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

_JSON_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class EffectClass(str, enum.Enum):
    PURE = "pure"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class ToolArgumentError(ValueError):
    """The arguments do not match the tool's schema; the message says what to change."""


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    effect: EffectClass
    handler: Callable[..., Any]
    untrusted: bool = False


def validate_arguments(name: str, schema: Dict[str, Any], arguments: Any) -> Dict[str, Any]:
    """Checks `arguments` against a flat JSON Schema (required, type, enum, no unknown keys)."""
    if not isinstance(arguments, dict):
        raise ToolArgumentError(f"The arguments of '{name}' must be a JSON object.")
    props = schema.get("properties") or {}
    missing = [k for k in schema.get("required") or [] if k not in arguments]
    if missing:
        raise ToolArgumentError(f"Tool '{name}' is missing required argument(s): {', '.join(missing)}.")
    unknown = [k for k in arguments if k not in props]
    if unknown:
        raise ToolArgumentError(
            f"Tool '{name}' has no argument(s): {', '.join(unknown)}. Valid arguments: {', '.join(props) or 'none'}."
        )
    for key, value in arguments.items():
        spec = props[key]
        want = spec.get("type")
        py = _JSON_TYPES.get(want)
        if py is not None:
            wrong_bool = isinstance(value, bool) and want in ("number", "integer")
            if wrong_bool or not isinstance(value, py):
                raise ToolArgumentError(f"Argument '{key}' of '{name}' must be of type {want}, got {type(value).__name__}.")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolArgumentError(f"Argument '{key}' of '{name}' must be one of {spec['enum']}.")
    return arguments


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
        untrusted: bool = False,
    ) -> None:
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            effect=effect,
            handler=handler,
            untrusted=untrusted,
        )

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def schemas(self) -> List[Dict[str, Any]]:
        """Standard OpenAI/Basalt tool definitions, in a stable order so the prompt prefix stays identical."""
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    def validate(self, name: str, arguments: Any) -> Dict[str, Any]:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: '{name}'")
        return validate_arguments(name, tool.parameters, arguments)

    def execute(self, name: str, arguments: Dict[str, Any]) -> Any:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: '{name}'")
        validate_arguments(name, tool.parameters, arguments)
        return tool.handler(**arguments)
