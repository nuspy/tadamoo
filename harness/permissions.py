"""Permission manager: implements security tiers, allow/deny rules, and plan mode."""
from __future__ import annotations

import enum
import fnmatch
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.tools.registry import EffectClass


class PermissionLevel(str, enum.Enum):
    READ_ONLY = "read_only"  # Level 1: Pure tools allowed; asks for any write/exec
    DEFAULT = "default"      # Level 2: Pure & reversible allowed; irreversible asks/rules
    AUTO = "auto"            # Level 3: All workspace-confined tools allowed


@dataclass
class PermissionRule:
    pattern: str  # Tool name or command pattern, e.g. "shell_command:rm *"
    action: str   # "allow" or "deny"


class PermissionManager:
    def __init__(
        self,
        level: PermissionLevel = PermissionLevel.DEFAULT,
        plan_mode: bool = False,
        rules: Optional[List[PermissionRule]] = None,
        confirm_callback: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
    ) -> None:
        self.level = level
        self.plan_mode = plan_mode
        self.rules: List[PermissionRule] = list(rules or [])
        self.confirm_callback = confirm_callback

    def add_rule(self, pattern: str, action: str) -> None:
        if action not in ("allow", "deny"):
            raise ValueError(f"Action must be 'allow' or 'deny', got '{action}'")
        self.rules.append(PermissionRule(pattern=pattern, action=action))

    def evaluate(self, tool_name: str, effect: EffectClass, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        """Evaluates whether a tool execution is permitted.

        Returns (allowed: bool, reason: str).
        """
        # Plan mode: block any non-pure tool
        if self.plan_mode and effect != EffectClass.PURE:
            return False, f"Plan mode active: execution of {tool_name} deferred until user approves plan"

        # Check explicit rules first
        target_str = tool_name
        if tool_name == "shell_command" and "command" in arguments:
            target_str = f"shell_command:{arguments['command']}"

        for rule in self.rules:
            if fnmatch.fnmatch(target_str, rule.pattern):
                if rule.action == "deny":
                    return False, f"Execution denied by rule: '{rule.pattern}'"
                if rule.action == "allow":
                    return True, f"Permitted by allow rule: '{rule.pattern}'"

        # Pure tools are always allowed in any mode
        if effect == EffectClass.PURE:
            return True, "Pure read-only operation permitted"

        # Read-only tier asks for all non-pure actions
        if self.level == PermissionLevel.READ_ONLY:
            if self.confirm_callback and self.confirm_callback(tool_name, arguments):
                return True, "User confirmed execution"
            return False, f"Execution of {tool_name} rejected: Read-only tier requires confirmation"

        # Default tier: allows reversible within workspace, asks for irreversible
        if self.level == PermissionLevel.DEFAULT:
            if effect == EffectClass.REVERSIBLE:
                return True, "Reversible workspace modification permitted"
            # Irreversible (shell, external)
            if self.confirm_callback:
                if self.confirm_callback(tool_name, arguments):
                    return True, "User confirmed irreversible execution"
                return False, f"Irreversible tool {tool_name} rejected by user"
            return False, f"Confirmation required for irreversible tool: {tool_name}"

        # Auto tier: allows everything within workspace confinement
        if self.level == PermissionLevel.AUTO:
            return True, "Auto tier permitted"

        return False, "Permission denied by default"
