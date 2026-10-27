"""Permission manager: autonomy levels, allow/deny rules, plan mode and the Rule of Two.

Evaluation order, first match wins:
  1. plan mode refuses everything that is not read-only;
  2. a matching deny rule refuses (allow rules never override it);
  2b. at the Auto level everything else passes, nothing is asked;
  3. a tool in `always_ask` needs a fresh confirmation every time, whatever the rules;
  4. an allow rule permits (a command that chains others is permitted when every statement of it is covered by a rule);
  5. read-only tools pass; the level decides the rest.
"""
from __future__ import annotations

import enum
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from harness.rule_of_two import RuleOfTwo
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


_RUNTIME_EXPANSION = re.compile(r"\$\(|\$\{|`|%[A-Za-z_]+%")      # text that becomes a command only when the shell runs it
_VERB_OK = re.compile(r"[A-Za-z0-9_.\-]+")


def statements(command: str) -> Optional[List[str]]:
    """The statements of a command line (split on ; & | && || and line ends outside quotes), or None when part of it is built at run time
    (`$(...)`, a backtick, `${...}`, `%VAR%`), so what it would run cannot be told from its text. A redirection (`2>&1`, `> out`) stays in its statement."""
    if _RUNTIME_EXPANSION.search(command):
        return None
    out: List[str] = []
    cur: List[str] = []
    quote = ""
    for i, ch in enumerate(command):
        if quote:
            cur.append(ch)
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in ";|\n\r" or (ch == "&" and not (i and command[i - 1] == ">")):
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _verb(statement: str) -> str:
    words = statement.split()
    verb = words[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1] if words else ""
    return verb if _VERB_OK.fullmatch(verb) else ""


def _glob_escape(text: str) -> str:
    return re.sub(r"[*?\[]", lambda m: f"[{m.group(0)}]", text)


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


def fingerprint(tool_name: str, arguments: Dict[str, Any]) -> str:
    """What one approval covers: this tool with these arguments. A shell command is its own text; any other tool is its name and a digest of
    its arguments, so approving `run_code` for one snippet lets that snippet run again and no other."""
    command = arguments.get("command") if tool_name == "shell_command" else None
    if isinstance(command, str):
        return f"shell_command:{command.strip()}"
    try:
        body = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        body = repr(sorted(arguments.items(), key=lambda kv: str(kv[0])))
    return f"{tool_name}#{hashlib.sha256(body.encode('utf-8', 'replace')).hexdigest()[:24]}"


def suggest_rules(tool_name: str, arguments: Dict[str, Any], always_ask: FrozenSet[str] = DEFAULT_ALWAYS_ASK) -> List[str]:
    """The allow rules that would let this kind of call run without asking again: a command by the verb of each of its statements
    (`pytest *`; a statement with no plain verb by its own text), any other tool by its name. A tool that always asks, or whose question is
    its own (payments, the writes inside .git), gets none."""
    if tool_name in always_ask or tool_name == "request_payment" or ":" in tool_name:
        return []
    if tool_name != "shell_command":
        return [tool_name]
    command = arguments.get("command")
    parts = statements(command) if isinstance(command, str) else None
    if not parts:
        return []
    rules = [f"shell_command:{_verb(x)} *" if _verb(x) else f"shell_command:{_glob_escape(x)}" for x in parts]
    return list(dict.fromkeys(rules))


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
        self.two = RuleOfTwo()                     # the three facts of the Rule of Two for this conversation (harness/rule_of_two.py)
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

    @property
    def untrusted_input(self) -> bool:
        """The model has read content from outside the user's control."""
        return bool(self.two.untrusted)

    def note_untrusted_input(self, source: str = "") -> None:
        self.two.note_untrusted(source)

    def note_private_data(self, source: str = "") -> None:
        self.two.note_private(source)

    @property
    def rule_of_two_broken(self) -> bool:
        """Untrusted input, sensitive data and outward effects must never all be present unattended. Whether the session can
        reach sensitive data is looked at now, not when the agent was built: the person may add to the vault meanwhile."""
        return self.two.level != "off" and self.untrusted_input and (self.sensitive_data or bool(self.two.private))

    def _allowed_by(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """The allow rules that cover this call, or ''. A rule that is the tool's name covers every call of it; a command is covered when each
        of its statements is covered by a rule, so `cd x && make` needs one for `cd` and one for `make`."""
        allows = [r for r in self.rules if r.action == "allow"]
        if any(r.pattern.lower() == tool_name.lower() for r in allows):
            return tool_name
        command = arguments.get("command")
        if tool_name != "shell_command" or not isinstance(command, str):
            return ""
        parts = statements(command)
        if not parts:
            return ""
        used: List[str] = []
        for part in parts:
            hit = next((r for r in allows if _matches(r, [f"shell_command:{part}"], {"command": part})), None)
            if hit is None:
                return ""
            used.append(hit.pattern)
        return ", ".join(dict.fromkeys(used))

    def _ask(self, tool_name: str, arguments: Dict[str, Any], key: Optional[str]) -> bool:
        if key is not None and key in self._remembered:
            return True
        approved = bool(self.confirm_callback and self.confirm_callback(tool_name, arguments))
        if approved and key is not None:
            self._remembered.add(key)
        return approved

    def ask_leak(self, tool_name: str, arguments: Dict[str, Any], shown: Dict[str, Any], question: str) -> Tuple[bool, str]:
        """A known secret is inside the arguments of an outward call: the person decides, seeing `shown` (the arguments with the secret hidden).
        With the Rule of Two off, or at the Auto level, nothing is asked. The same call, once allowed, is not asked about again."""
        if self.two.level == "off" or self.level == PermissionLevel.AUTO:
            return True, ""
        if self._ask(tool_name, {**shown, "rule_of_two": question}, "leak:" + fingerprint(tool_name, arguments)):
            return True, "User confirmed a secret in an outward call"
        self.denied += 1
        self.denied_by_tool[tool_name] = self.denied_by_tool.get(tool_name, 0) + 1
        return False, self._refusal(question)

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

    def evaluate(self, tool_name: str, effect: EffectClass, arguments: Dict[str, Any], outward: bool = False) -> Tuple[bool, str]:
        """Returns (allowed, reason). Every refusal is counted. `outward`: the tool sends what the model wrote out of this PC."""
        allowed, reason = self._decide(tool_name, effect, arguments, outward)
        if not allowed:
            self.denied += 1
            self.denied_by_tool[tool_name] = self.denied_by_tool.get(tool_name, 0) + 1
        return allowed, reason

    def _decide(self, tool_name: str, effect: EffectClass, arguments: Dict[str, Any], outward: bool = False) -> Tuple[bool, str]:
        if self.plan_mode and effect != EffectClass.PURE:
            return False, f"Plan mode active: execution of {tool_name} deferred until user approves plan"

        targets, _ = _targets(tool_name, arguments)

        for rule in self.rules:
            if rule.action == "deny" and _matches(rule, targets, arguments):
                return False, f"Execution denied by rule: '{rule.pattern}'"

        if self.level == PermissionLevel.AUTO:
            return True, "Auto level permitted"                 # the person chose that nothing is asked; their own deny rules above still hold

        if tool_name in self.always_ask:
            if self._ask(tool_name, arguments, None):
                return True, f"User confirmed {tool_name}"
            return False, self._refusal(f"{tool_name} needs the user's explicit confirmation every time")

        why = self.two.question(tool_name) if outward else ""      # the Rule of Two: an outward action after untrusted content and private data
        if why:
            if self._ask(tool_name, {**arguments, "rule_of_two": why}, None):
                return True, "User confirmed after the Rule of Two check"      # the same call, put plainly: the level's own question would add nothing
            return False, self._refusal(why)

        covered = self._allowed_by(tool_name, arguments)
        if covered:
            return True, f"Permitted by allow rule: '{covered}'"

        if effect == EffectClass.PURE:
            return True, "Pure read-only operation permitted"

        key = fingerprint(tool_name, arguments)
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

        return False, "Permission denied by default"
