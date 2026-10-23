"""Event hooks: deterministic checks before and after a tool runs, and session lifecycle events.

A pre-tool hook returns an error message to stop the call; a hook that raises also stops it, because a
broken safety check must never read as approval. Post-tool and event hooks cannot affect the outcome;
their failures are collected in `errors`.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

EVENTS = ("session_start", "turn_end", "session_end")

# A path that must never be the target of a recursive delete: a drive root, the filesystem root, the
# home folder, the parent folder, the current folder or everything in it.
_BROAD = r"(?:[A-Za-z]:[\\/]?|/\*?|~[\\/]?|\.\.[\\/]?|\*|\.|\$HOME|%USERPROFILE%|%SYSTEMROOT%|%WINDIR%)"
_END = r"(?=\s|$|[\"';&|])"
_ARGS = r"[^\n;&|]*"

DANGEROUS_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    rf"\brm\b{_ARGS}\s(?:-[A-Za-z]*r[A-Za-z]*|--recursive)\b{_ARGS}\s{_BROAD}{_END}",
    rf"\brm\b{_ARGS}\s{_BROAD}{_END}{_ARGS}\s(?:-[A-Za-z]*r[A-Za-z]*|--recursive)\b",
    rf"\b(?:del|erase)\b{_ARGS}\s/[sqf]\b{_ARGS}\s{_BROAD}{_END}",
    rf"\b(?:rd|rmdir)\b{_ARGS}\s/s\b{_ARGS}\s{_BROAD}{_END}",
    rf"\b(?:Remove-Item|ri|rm)\b{_ARGS}-Recurse{_ARGS}\s{_BROAD}{_END}",
    rf"\b(?:Remove-Item|ri)\b{_ARGS}\s{_BROAD}{_END}{_ARGS}-Recurse",
    r"\bformat(?:\.com)?\s+[A-Za-z]:",
    r"\bmkfs(?:\.\w+)?\b",
    r"\bdd\b[^\n;&|]*\bof=/dev/",
    r"\bdiskpart\b",
    r"\b(?:shutdown|Stop-Computer|Restart-Computer)\b",
    r"\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(?:sh|bash|zsh|iex|Invoke-Expression|pwsh|powershell)\b",
    r"\bdrop\s+database\b",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
)]


def dangerous_command(command: str) -> Optional[str]:
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return pattern.pattern
    return None


class HookManager:
    def __init__(self, safety_interceptor: bool = True) -> None:
        self._pre_tool_hooks: List[Callable[[str, Dict[str, Any]], Optional[str]]] = []
        self._post_tool_hooks: List[Callable[[str, Dict[str, Any], Any], None]] = []
        self._event_hooks: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {e: [] for e in EVENTS}
        self.errors: List[str] = []
        if safety_interceptor:
            self.register_pre_tool_hook(self._safety_interceptor)

    def register_pre_tool_hook(self, hook: Callable[[str, Dict[str, Any]], Optional[str]]) -> None:
        self._pre_tool_hooks.append(hook)

    def register_post_tool_hook(self, hook: Callable[[str, Dict[str, Any], Any], None]) -> None:
        self._post_tool_hooks.append(hook)

    def register_event_hook(self, event: str, hook: Callable[[Dict[str, Any]], None]) -> None:
        if event not in self._event_hooks:
            raise ValueError(f"Unknown hook event '{event}'; expected one of {EVENTS}")
        self._event_hooks[event].append(hook)

    @staticmethod
    def _safety_interceptor(tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        if tool_name == "shell_command":
            cmd = arguments.get("command", "")
            if isinstance(cmd, str) and dangerous_command(cmd):
                return f"Safety hook blocked prohibited dangerous command: '{cmd}'"
        return None

    def trigger_pre_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Runs all pre-tool hooks. If any returns or raises an error, execution is aborted."""
        for hook in self._pre_tool_hooks:
            try:
                err = hook(tool_name, arguments)
            except Exception as exc:
                return f"Pre-tool hook failed ({type(exc).__name__}: {exc}); the call was not made"
            if err:
                return err
        return None

    def trigger_post_tool(self, tool_name: str, arguments: Dict[str, Any], result: Any) -> None:
        for hook in self._post_tool_hooks:
            try:
                hook(tool_name, arguments, result)
            except Exception as exc:
                self.errors.append(f"post-tool hook: {type(exc).__name__}: {exc}")

    def trigger_event(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        for hook in self._event_hooks.get(event, []):
            try:
                hook(payload or {})
            except Exception as exc:
                self.errors.append(f"{event} hook: {type(exc).__name__}: {exc}")
