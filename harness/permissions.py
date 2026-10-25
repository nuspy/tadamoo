"""Permission manager: autonomy levels, allow/deny rules, plan mode and the Rule of Two.

Evaluation order, first match wins:
  1. plan mode refuses everything that is not read-only;
  2. a matching deny rule refuses (allow rules never override it);
  3. a tool in `always_ask` needs a fresh confirmation every time, whatever the level or the rules;
  4. an allow rule permits (a shell command that chains or redirects never matches an allow rule);
  5. read-only tools pass; the level decides the rest.
"""
from __future__ import annotations

import enum
import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from harness.sandbox import command_words
from harness.tools.registry import EffectClass


class PermissionLevel(str, enum.Enum):
    READ_ONLY = "read_only"  # Level 1: read-only tools pass; anything else asks
    DEFAULT = "default"      # Level 2: reads and workspace edits pass; irreversible actions ask
    AUTO = "auto"            # Level 3: everything passes unless the Rule of Two is broken


DEFAULT_ALWAYS_ASK = frozenset({"phone_call"})
_BARE_VERB = re.compile(r"^([^\s*?\[\]]+)(?: \*)?$")      # "rm" or "rm *": the verb alone, whatever its arguments
_CHAINING = re.compile(r"&&|\|\||[;&|\n\r`<>]|\$\(|\$\{|%[A-Za-z_]+%")


@dataclass
class PermissionRule:
    pattern: str  # tool name or "shell_command:<command glob>", e.g. "shell_command:rm *"
    action: str   # "allow" or "deny"


def _targets(tool_name: str, arguments: Dict[str, Any]) -> Tuple[List[str], bool]:
    """The strings a rule can match, and whether the call is a single plain command."""
    if tool_name != "shell_command" or not isinstance(arguments.get("command"), str):
        return [tool_name], True
    command = arguments["command"].strip()
    segments = [s.strip() for s in re.split(r"&&|\|\||[;&|\n\r`<>]|\$\(", command) if s.strip()]
    chained = bool(_CHAINING.search(command))
    return [f"shell_command:{command}"] + [f"shell_command:{s}" for s in segments], not chained


def _matches(rule: PermissionRule, targets: List[str], arguments: Dict[str, Any]) -> bool:
    pattern = rule.pattern.lower()
    if any(fnmatch.fnmatchcase(t.lower(), pattern) for t in targets):
        return True
    # "shell_command:reg *" must also catch "cmd /c reg add" and "echo x & reg add"
    if pattern.startswith("shell_command:") and isinstance(arguments.get("command"), str):
        verb = _BARE_VERB.match(pattern[len("shell_command:"):])
        if verb:
            return verb.group(1) in command_words(arguments["command"])
    return False


class PermissionManager:
    def __init__(
        self,
        level: PermissionLevel = PermissionLevel.DEFAULT,
        plan_mode: bool = False,
        rules: Optional[List[PermissionRule]] = None,
        confirm_callback: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        always_ask: FrozenSet[str] = DEFAULT_ALWAYS_ASK,
        sensitive_data: "bool | Callable[[], bool]" = False,
    ) -> None:
        self.level = level
        self.plan_mode = plan_mode
        self.rules: List[PermissionRule] = list(rules or [])
        self.confirm_callback = confirm_callback
        self.always_ask = frozenset(always_ask)
        self._sensitive = sensitive_data           # the session can reach secrets or private data; a function is asked each time
        self.untrusted_input = False               # the model has read content from outside the user's control
        self._remembered: Set[str] = set()
        self.denied = 0                            # calls refused in this session, whatever the reason
        self.denied_by_tool: Dict[str, int] = {}

    @property
    def sensitive_data(self) -> bool:
        return bool(self._sensitive() if callable(self._sensitive) else self._sensitive)

    @sensitive_data.setter
    def sensitive_data(self, value: "bool | Callable[[], bool]") -> None:
        self._sensitive = value

    def add_rule(self, pattern: str, action: str) -> None:
        if action not in ("allow", "deny"):
            raise ValueError(f"Action must be 'allow' or 'deny', got '{action}'")
        self.rules.append(PermissionRule(pattern=pattern, action=action))

    def remove_rule(self, pattern: str, action: str) -> None:
        self.rules = [r for r in self.rules if not (r.pattern == pattern and r.action == action)]

    def note_untrusted_input(self) -> None:
        self.untrusted_input = True

    @property
    def rule_of_two_broken(self) -> bool:
        """Untrusted input, sensitive data and outward effects must never all be present unattended. Whether the session can
        reach sensitive data is looked at now, not when the agent was built: the person may add to the vault meanwhile."""
        return self.untrusted_input and self.sensitive_data

    def _ask(self, tool_name: str, arguments: Dict[str, Any], key: Optional[str]) -> bool:
        if key is not None and key in self._remembered:
            return True
        approved = bool(self.confirm_callback and self.confirm_callback(tool_name, arguments))
        if approved and key is not None:
            self._remembered.add(key)
        return approved

    def _refusal(self, reason: str) -> str:
        """The reason a call was not run, with what the model can still do: no one can answer a question in an unattended
        run, and a command that chains others is not covered by an allow rule for its parts."""
        text = reason
        if self.confirm_callback is None:
            text += ". Nobody can confirm in this run, so it was not run"
        allowed = [r.pattern for r in self.rules if r.action == "allow"]
        if allowed:
            text += (f". These can run without confirmation: {', '.join(allowed)}, each as one plain command (no &&, ;, |, "
                     "> or <). Use one of them or say what you could not do")
        return text

    def evaluate(self, tool_name: str, effect: EffectClass, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        """Returns (allowed, reason). Every refusal is counted."""
        allowed, reason = self._decide(tool_name, effect, arguments)
        if not allowed:
            self.denied += 1
            self.denied_by_tool[tool_name] = self.denied_by_tool.get(tool_name, 0) + 1
        return allowed, reason

    def _decide(self, tool_name: str, effect: EffectClass, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        if self.plan_mode and effect != EffectClass.PURE:
            return False, f"Plan mode active: execution of {tool_name} deferred until user approves plan"

        targets, plain = _targets(tool_name, arguments)

        for rule in self.rules:
            if rule.action == "deny" and _matches(rule, targets, arguments):
                return False, f"Execution denied by rule: '{rule.pattern}'"

        if tool_name in self.always_ask:
            if self._ask(tool_name, arguments, None):
                return True, f"User confirmed {tool_name}"
            return False, self._refusal(f"{tool_name} needs the user's explicit confirmation every time")

        for rule in self.rules:
            if rule.action == "allow" and plain and _matches(rule, targets, arguments):
                return True, f"Permitted by allow rule: '{rule.pattern}'"

        if effect == EffectClass.PURE:
            return True, "Pure read-only operation permitted"

        key = targets[0]
        if self.level == PermissionLevel.READ_ONLY:
            if self._ask(tool_name, arguments, key):
                return True, "User confirmed execution"
            return False, self._refusal(f"Execution of {tool_name} rejected: read-only level requires confirmation")

        if self.level == PermissionLevel.DEFAULT:
            if effect == EffectClass.REVERSIBLE:
                return True, "Reversible workspace modification permitted"
            if self._ask(tool_name, arguments, key):
                return True, "User confirmed irreversible execution"
            return False, self._refusal(f"Irreversible tool {tool_name} needs confirmation")

        if self.level == PermissionLevel.AUTO:
            if effect == EffectClass.IRREVERSIBLE and self.rule_of_two_broken:
                if self._ask(tool_name, arguments, key):
                    return True, "User confirmed after the Rule of Two check"
                return False, (f"{tool_name} needs confirmation: this session read untrusted content and can reach "
                               "sensitive data")
            return True, "Auto level permitted"

        return False, "Permission denied by default"
