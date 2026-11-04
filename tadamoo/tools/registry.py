"""Tool definition and registry with effect classes.

Effect classes:
- PURE: read-only, deterministic, zero side effects
- REVERSIBLE: modifies files or local state, can be rolled back with checkpoints
- IRREVERSIBLE: executes external commands, state-changing network requests, or hardware calls

A tool marked `untrusted` returns content from outside the user's control (web pages, mail,
third-party servers); its results are fenced as data and taint the session's permissions. A tool marked
`outward` sends what the model wrote (an address, a query, a form) out of this PC: the Rule of Two looks at it
(tadamoo/rule_of_two.py).

Every tool has a time limit: the registry's default (the person's `tool_timeout_s` in limits.json, never
unlimited), or its own (`timeout=` when it is registered, or `set_timeout`; None means no limit). The tool runs in the calling thread, and a watchdog raises
`ToolTimeout` in that thread when the limit is spent, so no thread is left running behind. The clock does
not run while the person is being asked something (`clock_paused`). A tool stuck inside one blocking
system call is stopped when that call returns.
"""
from __future__ import annotations

import contextlib
import ctypes
import enum
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

from tadamoo.tools.repair import coerce_arguments, resolve_tool_name

DEFAULT_TOOL_TIMEOUT = 3600.0         # the limit when the person has not set one (serve/limits.py, tool_timeout_s); a tool call always has one
_TICK = 0.1


def configured_tool_timeout() -> float:
    """The person's limit for one tool call, in seconds: limits.json or the default; never unlimited."""
    try:
        from serve import limits
        return limits.get("tool_timeout_s", DEFAULT_TOOL_TIMEOUT) or DEFAULT_TOOL_TIMEOUT
    except Exception:
        return DEFAULT_TOOL_TIMEOUT
_local = threading.local()            # the clock of the tool call this thread is running

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


class ToolTimeout(TimeoutError):
    """The tool ran past its time limit and was stopped."""


class _Interrupt(BaseException):
    """Raised inside a tool that ran out of time; not an Exception, so a tool's own `except Exception` cannot swallow it."""


class _Clock:
    def __init__(self, limit: float, thread_id: int) -> None:
        self.limit, self.thread_id = limit, thread_id
        self.spent = 0.0
        self.paused = 0
        self.fired = False
        self.finished = threading.Event()
        self.lock = threading.Lock()

    def _inject(self, exc: Any) -> None:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self.thread_id), ctypes.py_object(exc) if exc else None)

    def watch(self) -> None:
        last = time.monotonic()
        while not self.finished.wait(_TICK):
            now = time.monotonic()
            if not self.paused:
                self.spent += now - last
            last = now
            if self.spent >= self.limit:
                with self.lock:
                    if self.finished.is_set():
                        return
                    self.fired = True
                    self._inject(_Interrupt)
                return

    def finish(self) -> None:
        with self.lock:
            self.finished.set()
            if self.fired:
                self._inject(None)           # an interrupt not yet raised must not outlive the call


@contextlib.contextmanager
def clock_paused() -> Iterator[None]:
    """For code that waits for the person inside a tool: the tool's time limit does not run meanwhile."""
    clock = getattr(_local, "clock", None)
    if clock is not None:
        clock.paused += 1
    try:
        yield
    finally:
        if clock is not None:
            clock.paused -= 1


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    effect: EffectClass
    handler: Callable[..., Any]
    untrusted: bool = False
    outward: bool = False        # sends something the model wrote (an address, a query, a form) out of this PC
    tier: int = 1                # where its definition goes in the tool list: STABLE, SESSION or EXTERNAL (see ToolRegistry.schemas)


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


_UNSET: Any = object()

# The tool list is part of the prompt prefix the server caches, so tools that rarely change come first.
STABLE, SESSION, EXTERNAL = 0, 1, 2

# The optional argument every tool takes beside its own: one sentence for the person. The first name a tool does not already use is the one offered.
DESCRIPTION_KEYS = ("description", "explain")
DESCRIPTION_CHARS = 300
TOOL_CALL_NOTE = f"The person sees your `{DESCRIPTION_KEYS[0]}` of each tool call beside it."
_DESCRIPTION_HELP = "One short sentence for the person, in the language they write in, saying what this call does and why."


def description_key(parameters: Dict[str, Any]) -> Optional[str]:
    """The name under which a tool with these parameters takes the person's sentence; None if the tool uses every name."""
    props = parameters.get("properties") or {}
    return next((k for k in DESCRIPTION_KEYS if k not in props), None)


def split_description(parameters: Dict[str, Any], arguments: Any) -> "tuple[Any, str]":
    """The arguments without the person's sentence, and the sentence (trimmed, one line, at most DESCRIPTION_CHARS; empty if none).
    It is display text only: nothing that decides or runs sees it."""
    key = description_key(parameters)
    if key is None or not isinstance(arguments, dict) or key not in arguments:
        return arguments, ""
    rest = {k: v for k, v in arguments.items() if k != key}
    text = arguments[key]
    return rest, " ".join(text.split())[:DESCRIPTION_CHARS] if isinstance(text, str) else ""


class ToolRegistry:
    def __init__(self, default_timeout: Optional[float] = _UNSET) -> None:
        self._tools: Dict[str, ToolDefinition] = {}
        self._default_timeout = default_timeout              # seconds; None: no limit unless a tool has its own; unset: the person's limit
        self._timeouts: Dict[str, Optional[float]] = {}      # a tool's own limit (None: no limit for that tool)

    @property
    def default_timeout(self) -> Optional[float]:
        return configured_tool_timeout() if self._default_timeout is _UNSET else self._default_timeout

    @default_timeout.setter
    def default_timeout(self, seconds: Optional[float]) -> None:
        self._default_timeout = seconds

    def set_timeout(self, name: str, seconds: Optional[float]) -> None:
        """The time limit of one tool; None or 0 for no limit."""
        self._timeouts[name] = seconds if seconds and seconds > 0 else None

    def timeout_for(self, name: str) -> Optional[float]:
        return self._timeouts[name] if name in self._timeouts else self.default_timeout

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        effect: EffectClass,
        handler: Callable[..., Any],
        untrusted: bool = False,
        timeout: Optional[float] = _UNSET,
        outward: bool = False,
        tier: int = SESSION,
    ) -> None:
        if timeout is not _UNSET:
            self.set_timeout(name, timeout)
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            effect=effect,
            handler=handler,
            untrusted=untrusted,
            outward=outward,
            tier=tier,
        )

    def mark_stable(self) -> None:
        """The tools registered so far are the fixed set every conversation has: they lead the tool list."""
        for tool in self._tools.values():
            tool.tier = STABLE

    def unregister(self, name: str) -> bool:
        self._timeouts.pop(name, None)
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def schemas(self) -> List[Dict[str, Any]]:
        """Standard OpenAI/TaDaMoo tool definitions, in a stable order so the prompt prefix stays identical: the fixed tools first, then
        those a conversation's setup adds, then those of outside servers, each group by name. Each tool also offers the optional sentence
        for the person (see DESCRIPTION_KEYS); the tool's own parameters are not changed."""
        out = []
        for t in sorted(self._tools.values(), key=lambda t: (t.tier, t.name)):
            parameters, key = t.parameters, description_key(t.parameters)
            if key is not None:
                parameters = {**parameters, "properties": {**(parameters.get("properties") or {}), key: {"type": "string", "description": _DESCRIPTION_HELP}}}
            out.append({"type": "function", "function": {"name": t.name, "description": t.description, "parameters": parameters}})
        return out

    def split_description(self, name: str, arguments: Any) -> "tuple[Any, str]":
        """`split_description` for a registered tool (an unknown name has nothing to split)."""
        tool = self.get(name)
        return (arguments, "") if tool is None else split_description(tool.parameters, arguments)

    def resolve(self, name: str) -> "tuple[Optional[str], List[str]]":
        """The registered tool a name stands for (see repair.resolve_tool_name) and, when there is none, the tools with a similar name."""
        return resolve_tool_name(name, self.names())

    def coerce(self, name: str, arguments: Any) -> "tuple[Any, bool]":
        """The arguments with each value made the type the tool's schema asks for where that is not in doubt, and whether any changed."""
        tool = self.get(name)
        return (arguments, False) if tool is None else coerce_arguments(tool.parameters, arguments)

    def validate(self, name: str, arguments: Any) -> Dict[str, Any]:
        """The arguments checked against the tool's schema, after the safe coercions; the checked arguments are returned."""
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: '{name}'")
        return validate_arguments(name, tool.parameters, self.coerce(name, arguments)[0])

    def execute(self, name: str, arguments: Dict[str, Any]) -> Any:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: '{name}'")
        arguments = validate_arguments(name, tool.parameters, self.coerce(name, arguments)[0])
        limit = self.timeout_for(name)
        if not limit:
            return tool.handler(**arguments)
        clock = _Clock(limit, threading.get_ident())
        before = getattr(_local, "clock", None)
        _local.clock = clock
        threading.Thread(target=clock.watch, name=f"tool-clock-{name}", daemon=True).start()
        try:
            try:
                return tool.handler(**arguments)
            finally:
                clock.finish()
        except _Interrupt:
            raise ToolTimeout(f"Tool '{name}' was stopped after {limit:g} seconds without finishing. "
                              "Try a smaller request or another way.") from None
        finally:
            _local.clock = before
