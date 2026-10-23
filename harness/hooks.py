"""Event hooks for pre/post tool execution and safety enforcement."""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional


# Critical dangerous command patterns
DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+.*-[a-zA-Z]*r[a-zA-Z]*\s+([/~]|\.\.)", re.IGNORECASE),
    re.compile(r"\bformat\s+[c-z]:", re.IGNORECASE),
    re.compile(r"\bdel\s+/[fqs\s]+[c-z]:\\", re.IGNORECASE),
    re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.IGNORECASE),  # fork bomb
]


class HookManager:
    def __init__(self) -> None:
        self._pre_tool_hooks: List[Callable[[str, Dict[str, Any]], Optional[str]]] = []
        self._post_tool_hooks: List[Callable[[str, Dict[str, Any], Any], None]] = []

        # Register default security interceptor
        self.register_pre_tool_hook(self._safety_interceptor)

    def register_pre_tool_hook(self, hook: Callable[[str, Dict[str, Any]], Optional[str]]) -> None:
        self._pre_tool_hooks.append(hook)

    def register_post_tool_hook(self, hook: Callable[[str, Dict[str, Any], Any], None]) -> None:
        self._post_tool_hooks.append(hook)

    def _safety_interceptor(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        if tool_name == "shell_command":
            cmd = arguments.get("command", "")
            for pattern in DANGEROUS_PATTERNS:
                if pattern.search(cmd):
                    return f"Safety hook blocked prohibited dangerous command: '{cmd}'"
        return None

    def trigger_pre_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Runs all pre-tool hooks. If any returns an error message, execution is aborted."""
        for hook in self._pre_tool_hooks:
            err = hook(tool_name, arguments)
            if err:
                return err
        return None

    def trigger_post_tool(self, tool_name: str, arguments: Dict[str, Any], result: Any) -> None:
        for hook in self._post_tool_hooks:
            try:
                hook(tool_name, arguments, result)
            except Exception:
                pass
