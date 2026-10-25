"""Tool definition and registry with effect classes.

Effect classes:
- PURE: read-only, deterministic, zero side effects
- REVERSIBLE: modifies files or local state, can be rolled back with checkpoints
- IRREVERSIBLE: executes external commands, state-changing network requests, or hardware calls

A tool marked `untrusted` returns content from outside the user's control (web pages, mail,
third-party servers); its results are fenced as data and taint the session's permissions.

Every tool has a time limit: the registry's default, or its own (`timeout=` when it is registered, or
`set_timeout`; None means no limit). The tool runs in the calling thread, and a watchdog raises
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

DEFAULT_TOOL_TIMEOUT = 900.0          # more than the longest a shell command may run, so the command's own limit comes first
_TICK = 0.1
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


class ToolRegistry:
    def __init__(self, default_timeout: Optional[float] = DEFAULT_TOOL_TIMEOUT) -> None:
        self._tools: Dict[str, ToolDefinition] = {}
        self.default_timeout = default_timeout               # seconds; None: no limit unless a tool has its own
        self._timeouts: Dict[str, Optional[float]] = {}      # a tool's own limit (None: no limit for that tool)

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
        )

    def unregister(self, name: str) -> bool:
        self._timeouts.pop(name, None)
        return self._tools.pop(name, None) is not None

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
