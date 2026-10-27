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


# programs that run whatever they are given (`python -c`, `powershell -EncodedCommand`), that fetch and send (`curl`, `scp`), and that start
# other programs (`env`, `npx`): a rule for the verb alone would allow anything, so only the exact command is offered
BROAD_VERBS = frozenset((
    "python python2 python3 py pythonw pypy node nodejs deno bun ruby perl php lua tclsh java dotnet go cargo rustc julia r rscript "
    "powershell pwsh cmd bash sh zsh fish dash ksh csh wsl wscript cscript mshta rundll32 regsvr32 msiexec forfiles "
    "curl wget iwr irm invoke-webrequest invoke-restmethod certutil bitsadmin scp sftp ssh ftp telnet nc ncat netcat socat "
    "env sudo su runas nohup command start call time xargs npx uvx pipx pnpx bunx eval exec invoke-expression iex").split())
# tools that run code or hand the task to another agent: "always allow this tool" is not offered, a rule is written by hand
BROAD_TOOLS = frozenset({"run_code", "delegate_to_acp_agent", "ask_a2a_agent"})


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


def _glob_escape(text: str) -> str:
    return re.sub(r"[*?\[]", lambda m: f"[{m.group(0)}]", text)


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


def suggest_rule(tool_name: str, arguments: Dict[str, Any], always_ask: FrozenSet[str] = DEFAULT_ALWAYS_ASK) -> Optional[str]:
    """The allow rule that would let this kind of call run without asking next time, or None when no rule could cover it: a command is
    allowed by its verb (`pytest *`), any other tool by its name; a program that runs what it is given, fetches or starts other programs
    (`python`, `powershell`, `curl`, `env`) is allowed only as this exact command, and a tool that runs code only by a rule written by hand.
    A command that chains others is covered by no rule, and a tool that always asks, or whose question is its own (payments, the writes
    inside .git), is never offered."""
    if tool_name in always_ask or tool_name == "request_payment" or ":" in tool_name:
        return None
    if tool_name != "shell_command":
        return None if tool_name in BROAD_TOOLS else tool_name
    command = arguments.get("command")
    if not isinstance(command, str) or _CHAINING.search(command):
        return None
    words = command.strip().split()
    verb = words[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1] if words else ""
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", verb):
        return None
    if verb.lower().removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat").removesuffix(".ps1") in BROAD_VERBS:
        return f"shell_command:{_glob_escape(' '.join(words))}"
    return f"shell_command:{verb} *"


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

        targets, plain = _targets(tool_name, arguments)

        for rule in self.rules:
            if rule.action == "deny" and _matches(rule, targets, arguments):
                return False, f"Execution denied by rule: '{rule.pattern}'"

        if tool_name in self.always_ask:
            if self._ask(tool_name, arguments, None):
                return True, f"User confirmed {tool_name}"
            return False, self._refusal(f"{tool_name} needs the user's explicit confirmation every time")

        why = self.two.question(tool_name) if outward else ""      # the Rule of Two: an outward action after untrusted content and private data
        if why:
            if self._ask(tool_name, {**arguments, "rule_of_two": why}, None):
                return True, "User confirmed after the Rule of Two check"      # the same call, put plainly: the level's own question would add nothing
            return False, self._refusal(why)

        for rule in self.rules:
            if rule.action == "allow" and plain and _matches(rule, targets, arguments):
                return True, f"Permitted by allow rule: '{rule.pattern}'"

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

        if self.level == PermissionLevel.AUTO:
            if effect == EffectClass.IRREVERSIBLE and self.rule_of_two_broken:
                if self._ask(tool_name, arguments, key):
                    return True, "User confirmed after the Rule of Two check"      # the same call, put plainly: the level's own question would add nothing
                return False, (f"{tool_name} needs confirmation: this session read untrusted content and can reach "
                               "sensitive data")
            return True, "Auto level permitted"

        return False, "Permission denied by default"
