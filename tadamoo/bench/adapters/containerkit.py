"""What the adapters that work inside a container share.

`ContainerTools` are the agent's file and shell tools with their work done in a container through one function, `exec_fn(command, timeout) -> ExecOut`:
Harbor's `environment.exec` for Terminal-Bench, `docker exec` for SWE-bench. `run_harness_agent` runs our own agent (tadamoo/stack.py build_agent) with those
tools in place of the ones that act on this PC; `run_bash_baseline` is a bare loop with one bash tool on the same model, for the harness-effect comparison.
Both write the trajectory in the event format tadamoo/bench/analysis.py reads, one JSON line at a time, so a trial that is cut off still leaves its trace."""
from __future__ import annotations

import base64
import json
import math
import posixpath
import re
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from tadamoo import editing

TIMED_OUT_CODE = 124
DEFAULT_COMMAND_S = 180.0
MAX_COMMAND_S = 3600.0
OUTPUT_CHARS = 20_000               # what one command answer may hold; a longer one keeps its start and its end
READ_CHARS = 40_000
READ_LIMIT_BYTES = 4_000_000
WRITE_CHUNK = 6_000                 # base64 characters sent in one command: a command line is short on some hosts (a Windows one holds under 8,000 through cmd.exe)
LISTED = 500
HITS = 100
KEPT_TOOLS = ("shell_command", "read_file", "write_file", "edit_file", "list_directory", "search_code", "todo_write", "self_status")
# AgentConfig fields a benchmark's settings may not set: they reach outside the container or the benchmark's sandbox
FORBIDDEN_FIELDS = frozenset({
    "workspace", "data_dir", "endpoint", "api_key", "confirm", "ask_code", "browser", "browser_headless", "browser_cdp_url", "browser_private", "browser_profile",
    "browser_consent", "browser_permissions", "vault_dir", "payments_dir", "phone", "phone_on_behalf_of", "wiki", "wiki_url", "wiki_project", "mcp_servers",
    "council", "capabilities", "capabilities_dir", "capabilities_home", "lsp", "personal_memory_dir", "personal_memory_keys", "personal_skills_dir",
    "past_index", "attachments_dir", "outputs_dir", "skill_paths", "hooks_file", "workspace_hooks", "exec_level_file", "persistence_file", "report",
    "run_id", "secret_names", "secret_hosts", "lessons_dir", "snapshot_dir", "permission_mode", "unattended", "use_memory", "use_checkpoints",
})


@dataclass
class ExecOut:
    rc: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


ExecFn = Callable[[str, Optional[float]], ExecOut]
_LINES = re.compile(r"[^\n]*\n|[^\n]+")


# -- the shell, in a container ------------------------------------------------------------------------------------------

def wrap_command(command: str, timeout: Optional[float]) -> str:
    """The command run under `timeout` (which also ends what it started) where the container has it, else as it is, in bash or sh."""
    quoted = shlex.quote(command)
    secs = max(1, int(math.ceil(timeout))) if timeout else 0
    run = f'timeout -k 5 {secs} "$__s" -c "$__c"' if secs else '"$__s" -c "$__c"'
    guard = f'if command -v timeout >/dev/null 2>&1; then {run}; else "$__s" -c "$__c"; fi' if secs else run
    return f'__c={quoted}; if command -v bash >/dev/null 2>&1; then __s=bash; else __s=sh; fi; {guard}'


def clip(text: str, limit: int = OUTPUT_CHARS) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return text[:half] + f"\n[... {len(text) - limit} characters cut ...]\n" + text[-half:], True


def check_timeout(value: Any) -> float:
    if value is None:
        return DEFAULT_COMMAND_S
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"The timeout must be a finite number of seconds greater than 0; got {value!r}. The command was not run.")
    return float(min(value, MAX_COMMAND_S))


class ContainerTools:
    """The tools of one trial. Every method raises on a fault, as the harness's own tools do, and the agent loop reports it to the model."""

    def __init__(self, exec_fn: ExecFn, default_timeout: float = DEFAULT_COMMAND_S) -> None:
        self.exec, self.default_timeout = exec_fn, default_timeout
        self.commands = 0

    # -- shell ---------------------------------------------------------------------------------------------------------

    def run(self, command: str, timeout: Optional[float] = None) -> ExecOut:
        self.commands += 1
        limit = check_timeout(timeout if timeout is not None else self.default_timeout)
        out = self.exec(wrap_command(command, limit), limit + 20)
        if out.rc == TIMED_OUT_CODE:
            out.timed_out = True
        return out

    def shell_command(self, command: str, timeout: Optional[float] = None, **_: Any) -> Dict[str, Any]:
        out = self.run(command, timeout)
        stdout, cut_out = clip(out.stdout)
        stderr, cut_err = clip(out.stderr)
        return {"exit_code": out.rc, "stdout": stdout, "stderr": stderr, "success": out.rc == 0, "blocked": False, "timed_out": out.timed_out,
                "truncated": cut_out or cut_err}

    # -- files ---------------------------------------------------------------------------------------------------------

    def _cat(self, path: str) -> str:
        missing = shlex.quote(f"File not found: {path}")
        out = self.run(f"test -f {shlex.quote(path)} || {{ echo {missing} >&2; exit 3; }}; head -c {READ_LIMIT_BYTES} -- {shlex.quote(path)}", 60)
        if out.rc != 0:
            raise FileNotFoundError((out.stderr or out.stdout).strip() or f"File not found: {path}")
        if "\x00" in out.stdout[:8000]:
            raise ValueError(f"'{path}' is a binary file")
        return out.stdout

    def read_file(self, path: str, offset: int = 1, limit: int = 0, numbered: bool = False, **_: Any) -> str:
        try:
            offset, limit = max(1, int(offset)), max(0, int(limit))
        except (TypeError, ValueError):
            raise ValueError("offset and limit must be whole numbers: offset is the first line to read (from 1), limit how many lines (0 for all)")
        lines = _LINES.findall(self._cat(path))
        total = len(lines)
        end = total if not limit else min(total, offset - 1 + limit)
        taken: List[str] = []
        size = 0
        for i in range(offset - 1, end):
            piece = (f"{i + 1:>6}\t" if numbered else "") + lines[i]
            if size + len(piece) > READ_CHARS and taken:
                break
            taken.append(piece[:READ_CHARS])
            size += len(piece)
        if not taken:
            return "" if total == 0 and offset == 1 else f"[offset {offset} is past the end: the file has {total} lines]"
        last = offset + len(taken) - 1
        text = "".join(taken)
        if offset == 1 and last >= total:
            return text
        note = f"lines {offset}-{last} of {total} shown" + (f"; read_file with offset={last + 1} shows the next lines" if last < total else "")
        return text + ("" if text.endswith("\n") else "\n") + f"[{note}]"

    def _put(self, path: str, data: bytes) -> None:
        q, parent = shlex.quote(path), shlex.quote(posixpath.dirname(path) or ".")
        encoded = base64.b64encode(data).decode("ascii")
        parts = [encoded[i:i + WRITE_CHUNK] for i in range(0, len(encoded), WRITE_CHUNK)] or [""]
        for n, part in enumerate(parts):
            redirect = ">" if n == 0 else ">>"
            command = f'mkdir -p -- {parent} && printf %s {part} | base64 -d {redirect} {q}' if part else f'mkdir -p -- {parent} && : {redirect} {q}'
            out = self.run(command, 120)
            if out.rc != 0:
                raise OSError(f"Could not write {path}: {(out.stderr or out.stdout).strip()[:300]}")
        check = self.run(f"wc -c < {q}", 30)
        if check.rc != 0 or check.stdout.strip() != str(len(data)):
            raise OSError(f"{path} was written with {check.stdout.strip() or '?'} bytes instead of {len(data)}")

    def write_file(self, path: str, content: str, overwrite: bool = False, **_: Any) -> Dict[str, Any]:
        self._put(path, content.encode("utf-8"))
        return {"status": "success", "file": path, "bytes": len(content.encode("utf-8"))}

    def edit_file(self, path: str, target: Optional[str] = None, replacement: Optional[str] = None, edits: Optional[List[Dict[str, Any]]] = None,
                  replace_all: bool = False, expected_count: Optional[int] = None) -> Dict[str, Any]:
        old = self._cat(path)
        items = _edit_items(target, replacement, edits, replace_all, expected_count)
        old_lf = editing.to_lf(old)
        outcome = editing.apply_edits(old_lf, items, path)
        new = editing.restore_endings(old, old_lf, outcome.text) if outcome.changed else old
        if new == old:
            return {"status": "success", "file": path, "changed": False, "note": " ".join(outcome.notes) or "The file already has this text; nothing was changed."}
        self._put(path, new.encode("utf-8"))
        result: Dict[str, Any] = {"status": "success", "file": path, "changed": True}
        if len(items) > 1:
            result["edits"] = len(items)
        if outcome.notes:
            result["notes"] = outcome.notes
        return result

    def list_directory(self, path: str = ".", **_: Any) -> List[Dict[str, str]]:
        out = self.run(f"ls -1Ap -- {shlex.quote(path or '.')}", 60)
        if out.rc != 0:
            raise FileNotFoundError((out.stderr or out.stdout).strip() or f"Directory not found: {path}")
        names = [n for n in out.stdout.splitlines() if n]
        return [{"name": n.rstrip("/"), "type": "directory" if n.endswith("/") else "file"} for n in names[:LISTED]]

    def search_code(self, pattern: str, path: str = ".", glob: str = "", context: int = 0, max_results: int = HITS, **_: Any) -> str:
        try:
            ctx = min(10, max(0, int(context)))
            cap = min(HITS, max(1, int(max_results)))
        except (TypeError, ValueError):
            raise ValueError("context and max_results must be whole numbers")
        includes = " ".join(f"--include={shlex.quote(g.strip())}" for g in glob.split(",") if g.strip())
        command = (f"grep -rnI -E {'-C ' + str(ctx) if ctx else ''} --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=__pycache__ {includes} "
                   f"-e {shlex.quote(pattern)} -- {shlex.quote(path or '.')} | head -n {cap}")
        out = self.run(command, 120)
        text = out.stdout.strip()
        if not text:
            if out.stderr.strip() and "No such file" in out.stderr:
                raise FileNotFoundError(out.stderr.strip()[:300])
            return f"No match for {pattern!r}."
        return clip(text, OUTPUT_CHARS)[0] + f"\n[at most {cap} lines shown]"

    # -- registration --------------------------------------------------------------------------------------------------

    def install(self, registry: Any, cwd_label: str = "the working folder") -> None:
        """Puts these tools in the registry in place of the ones of the same name, and drops every other tool that acts on this PC."""
        from tadamoo.tools.registry import STABLE, EffectClass
        for name in registry.names():
            if name not in KEPT_TOOLS:
                registry.unregister(name)
        path = {"type": "string", "description": f"Path in the container; a relative path is relative to {cwd_label}"}
        tools = [
            ("shell_command", "Runs a bash command in the task's container and returns exit_code, stdout and stderr. A command of several lines is run as a script. "
             "Each call is a new shell: put `cd` and exports in the same command. Start a server in the background with nohup and &.",
             {"command": {"type": "string", "description": "The bash command; it may have several lines"},
              "timeout": {"type": "number", "description": f"Seconds the command may run, greater than 0 (default {self.default_timeout:g})"}},
             ["command"], EffectClass.IRREVERSIBLE, self.shell_command),
            ("read_file", "Reads a file in the container, whole or a range of lines. A long file is read in parts: the answer says which lines it holds and the offset to go on with.",
             {"path": path, "offset": {"type": "integer", "description": "First line to read, from 1 (default 1)"},
              "limit": {"type": "integer", "description": "How many lines to read (default: as many as one answer holds)"},
              "numbered": {"type": "boolean", "description": "Put the line number before each line (default false)"}},
             ["path"], EffectClass.PURE, self.read_file),
            ("write_file", "Writes the whole content of a file in the container, creating folders as needed. Give the complete text.",
             {"path": path, "content": {"type": "string", "description": "Full file content to write"}}, ["path", "content"], EffectClass.REVERSIBLE, self.write_file),
            ("edit_file", "Replaces text in a file in the container. Give target and replacement, or edits: a list of {target, replacement} applied in order, all or none. "
             "The target is copied from the file; when it cannot be applied the answer shows the closest text with line numbers.",
             {"path": path, "target": {"type": "string", "description": "Text to replace, copied from the file"},
              "replacement": {"type": "string", "description": "Replacement text (empty to delete the target)"},
              "edits": {"type": "array", "description": "Several edits for this file instead of target and replacement",
                        "items": {"type": "object", "properties": {"target": {"type": "string"}, "replacement": {"type": "string"},
                                                                   "replace_all": {"type": "boolean"}, "expected_count": {"type": "integer"}},
                                  "required": ["target", "replacement"]}},
              "replace_all": {"type": "boolean", "description": "Replace every exact occurrence of the target (default: it must appear once)"},
              "expected_count": {"type": "integer", "description": "Replace every exact occurrence, which must be this many"}},
             ["path"], EffectClass.REVERSIBLE, self.edit_file),
            ("list_directory", "Lists the entries of a folder in the container.", {"path": {**path, "default": "."}}, [], EffectClass.PURE, self.list_directory),
            ("search_code", "Searches the text files of a folder in the container for an extended regular expression (grep -E). Each hit is path:line:text.",
             {"pattern": {"type": "string", "description": "Regular expression matched against each line"},
              "path": {"type": "string", "description": "Folder or file to search (default: the working folder)"},
              "glob": {"type": "string", "description": "Only files whose name matches, e.g. *.py; several separated by commas"},
              "context": {"type": "integer", "description": "Lines to show around each hit (0 to 10)"},
              "max_results": {"type": "integer", "description": f"Most lines to return (1 to {HITS})"}},
             ["pattern"], EffectClass.PURE, self.search_code),
        ]
        for name, description, properties, required, effect, handler in tools:
            registry.register(name=name, description=description, parameters={"type": "object", "properties": properties, "required": required},
                              effect=effect, handler=handler, tier=STABLE)


def _edit_items(target: Optional[str], replacement: Optional[str], edits: Any, replace_all: bool, expected_count: Optional[int]) -> List[Dict[str, Any]]:
    if edits is not None:
        if target is not None or replacement is not None:
            raise editing.EditError("Send either 'target' and 'replacement' or an 'edits' list, not both.")
        if not isinstance(edits, list) or not edits:
            raise editing.EditError("'edits' must be a non-empty list of {target, replacement} objects.")
        defaults = {k: v for k, v in (("replace_all", replace_all), ("expected_count", expected_count)) if v}
        return [{**defaults, **e} if isinstance(e, dict) else e for e in edits]
    if target is None or replacement is None:
        raise editing.EditError("Give 'target' and 'replacement', or an 'edits' list of {target, replacement} objects.")
    return [{"target": target, "replacement": replacement, "replace_all": replace_all, "expected_count": expected_count}]


# -- the trajectory -----------------------------------------------------------------------------------------------------

DECISIONS = {"executed": "executed", "denied": "refused", "blocked": "blocked", "invalid": "invalid", "loop": "failed", "skipped": "failed"}
_EXIT = re.compile(r'"exit_code"\s*:\s*(-?\d+)')
ARG_CHARS = 600


def _shrink(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= ARG_CHARS else value[:ARG_CHARS] + "..."
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v) for v in value[:20]]
    return value


def _command_error(output: str) -> str:
    """The error text of a shell answer that ended with a code other than 0 ('' when it worked or is not a shell answer)."""
    found = _EXIT.search(output)
    if not found or int(found.group(1)) == 0:
        return ""
    try:
        data = json.loads(output)
        return str(data.get("stderr") or data.get("stdout") or "")[-1500:] or f"exit code {found.group(1)}"
    except (ValueError, AttributeError):
        return output[-1500:]


class TrajectoryRecorder:
    """Writes the events of an agent run as JSON lines, one flushed line at a time (the format is documented in tadamoo/bench/analysis.py)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._open: Dict[str, Tuple[str, Any]] = {}
        self.plans = 0
        self.calls = 0

    def write(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            if self._file.closed:
                return
            self._file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            self._file.flush()

    def on_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "tool_start":
            self._open[str(event.get("call_id"))] = (str(event.get("name")), event.get("arguments") or {})
        elif kind == "tool_end":
            name, args = self._open.pop(str(event.get("call_id")), (str(event.get("name")), {}))
            output = str(event.get("output") or "")
            decision = DECISIONS.get(str(event.get("decision")), "executed")
            failed = event.get("status") != "completed"
            if failed and decision == "executed":
                decision = "failed"
            self.calls += 1
            self.write({"type": "tool_call", "name": name, "args": _shrink(args), "decision": decision})
            error = output[-1500:] if failed else (_command_error(output) if name == "shell_command" else "")
            self.write({"type": "tool_result", "name": name, **({"error": error} if error else {})})
        elif kind == "todo":
            self.plans += 1
            self.write({"type": "plan", "revision": self.plans})
        elif kind == "compaction":
            self.write({"type": "compaction"})
        elif kind == "assistant":
            text = str(event.get("content") or "")
            if text:
                self.write({"type": "assistant", "text": text[:2000]})

    def stop(self, reason: str, error: str = "") -> None:
        if error:
            self.write({"type": "error", "text": error[:2000]})
        self.write({"type": "stop", "reason": reason})

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()


# -- running an agent ---------------------------------------------------------------------------------------------------

@dataclass
class AgentOutcome:
    stop_reason: str = ""
    error: str = ""
    final: str = ""
    turns: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {**self.__dict__, "tokens": self.tokens}


def stop_name(stop_reason: str, error: str, timed_out: bool = False) -> str:
    """The reason written into the trajectory, in the words analysis.py looks for."""
    text = f"{stop_reason} {error}".lower()
    if timed_out or (stop_reason == "budget" and "second" in text):
        return "timeout"
    if re.search(r"context (?:length|window|overflow|limit)|maximum context|too many tokens|prompt is too long", text):
        return "context_overflow"
    return stop_reason or "final"


_SERVER_FAULT = re.compile(r"cannot reach the tadamoo server|the answer stopped before it was complete|the engine reported an error|connection (?:refused|reset)|"
                           r"remote end closed|bad gateway|service unavailable|layer \d+ never rang|illegal memory access", re.I)


def is_server_fault(outcome: "AgentOutcome") -> bool:
    """Whether the agent stopped because the model server failed (not because of anything the agent did), so the trial says nothing about the agent."""
    return outcome.stop_reason == "error" and bool(_SERVER_FAULT.search(outcome.error))


def local_endpoint(endpoint: str, record: Optional[Mapping[str, Any]] = None) -> str:
    """The model server address, refused unless it is the local engine's (a loopback address or the address the run recorded)."""
    if not endpoint:
        raise ValueError("the run recorded no model address; benchmarks run only with the served model")
    host = urlparse(endpoint if "//" in endpoint else "//" + endpoint).hostname or ""
    recorded = urlparse(str((record or {}).get("endpoint") or "")).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1") and host != recorded:
        raise ValueError(f"the model address {endpoint!r} is not the local engine; benchmarks run only with the served model")
    return endpoint.rstrip("/")


def watch_stop(should_stop: Optional[Callable[[], bool]], cancel: threading.Event, done: threading.Event, every: float = 1.0) -> Optional[threading.Thread]:
    """A thread that sets `cancel` when `should_stop()` turns true, until `done` is set."""
    if should_stop is None:
        return None

    def loop() -> None:
        while not done.wait(every):
            if should_stop():
                cancel.set()
                return
    t = threading.Thread(target=loop, name="bench-stop-watch", daemon=True)
    t.start()
    return t


def agent_overrides(settings: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The AgentConfig fields a run's settings may change: `agent_config`, plus the failed attempts allowed and the reasoning level the person set
    for the run. Any field that reaches outside the sandbox is refused by name."""
    from dataclasses import fields
    from tadamoo.stack import AgentConfig
    known = {f.name for f in fields(AgentConfig)}
    asked = dict((settings or {}).get("agent_config") or {})
    bad = sorted(k for k in asked if k not in known or k in FORBIDDEN_FIELDS)
    if bad:
        raise ValueError(f"these agent settings are not allowed in a benchmark: {', '.join(bad)}")
    s = settings or {}
    if s.get("max_failed_attempts"):
        asked["max_failed_attempts"] = int(s["max_failed_attempts"])
    if s.get("reasoning"):
        asked["reasoning_effort"] = str(s["reasoning"])
    return asked


# -- guidance: help for the agent that a run's settings switch on (off by default, so two runs can be compared) --------------

GUIDANCE = ("rules", "outputs")
RULES_NOTE = (
    "\nWorking rules:\n"
    "- A value your own script produced that fails a check is put right by fixing the script and running it again, not by editing the value by hand.\n"
    "- When the task leaves a choice open (a boundary, an order, a format), take the most literal reading of the data you were given, and check each "
    "requirement again under the other readings that are as valid.\n"
    "- Parse or sanitise a standard format (HTML, JSON, XML, CSV) with an installed, well-tested library rather than a parser of your own, unless the task forbids it.\n"
    "- Write each file the task asks for as soon as you have a first candidate, write it again whenever you improve it, and before you stop read it back "
    "and compare it with your final answer.")
_ABSOLUTE_PATH = re.compile(r"(?<![\w./~:-])/(?:[\w.+@-]+/)*[\w.+@-]*\w[\w.+@-]*")
MAX_NAMED_PATHS = 8


def guidance_of(settings: Optional[Mapping[str, Any]]) -> List[str]:
    """The guidance a run's settings ask for: a list or a comma-separated text of names in GUIDANCE. A name that is not one is refused."""
    asked = (settings or {}).get("guidance") or []
    names = [n.strip() for n in (asked.split(",") if isinstance(asked, str) else asked) if str(n).strip()]
    bad = sorted({n for n in names if n not in GUIDANCE})
    if bad:
        raise ValueError(f"the guidance is one of {', '.join(GUIDANCE)}; not {', '.join(bad)}")
    return list(dict.fromkeys(names))


def named_paths(instruction: str, limit: int = MAX_NAMED_PATHS) -> List[str]:
    """The absolute paths the instruction names, in order of appearance (a URL is not one)."""
    found: List[str] = []
    for m in _ABSOLUTE_PATH.finditer(instruction):
        path = m.group(0).rstrip(".,:;)")
        if len(path) > 2 and path not in found:
            found.append(path)
    return found[:limit]


class OutputWatch:
    """The completion check of the `outputs` guidance. The paths the instruction names and the container did not have at the start are watched: when
    the agent wants to stop and one is still missing, it is told once, and it may answer that the path is no result to produce."""

    def __init__(self, instruction: str, exec_fn: ExecFn) -> None:
        self.exec_fn, self.told = exec_fn, False
        self.absent = [p for p in named_paths(instruction) if self._exists(p) is False]

    def _exists(self, path: str) -> Optional[bool]:
        """True or False; None when the container could not say."""
        try:
            rc = self.exec_fn(f"test -e {shlex.quote(path)}", 30).rc
        except Exception:
            return None
        return True if rc == 0 else False if rc == 1 else None

    def __call__(self, goal: str, answer: str, work: str) -> str:
        if self.told or not self.absent:
            return ""
        missing = [p for p in self.absent if self._exists(p) is False]
        if not missing:
            return ""
        self.told = True
        return (f"[harness] The task text names {', '.join(missing)}, which does not exist. If it is a result you must produce, create it now; "
                "if it is not, answer again and say so.")


def run_harness_agent(instruction: str, exec_fn: ExecFn, *, endpoint: str, work_dir: Path, trajectory: TrajectoryRecorder, environment_note: str,
                      client: Any = None, cancel: Optional[threading.Event] = None, max_seconds: Optional[float] = None, max_turns: int = -1,
                      overrides: Optional[Mapping[str, Any]] = None, cwd_label: str = "the working folder", guidance: Tuple[str, ...] = ()) -> AgentOutcome:
    """Runs our headless agent on `instruction` with its tools acting in the container. The agent keeps its own loop, context handling, permission
    layer (at Auto: nobody is there to ask) and repeat guards; nothing of it is switched off for the benchmark. Returns when the agent stops."""
    from tadamoo import agent_loop as al
    from tadamoo.client import TadamooClient
    from tadamoo.stack import AgentConfig, build_agent
    work_dir = Path(work_dir)
    workspace = work_dir / "host-workspace"                       # an empty folder: the agent builder needs one, and no tool looks at it
    workspace.mkdir(parents=True, exist_ok=True)
    fixed = dict(workspace=str(workspace), data_dir=str(work_dir / "agent-data"), endpoint=endpoint, permission_mode="auto", use_memory=False,
                 use_checkpoints=False, unattended=True, report=False, lessons_dir=None)
    cfg = AgentConfig(**fixed, **{"max_seconds": max_seconds, "max_turns": max_turns, **dict(overrides or {})})
    client = client or TadamooClient(base_url=endpoint)
    client.call_timeout = None                                  # a model call has no time limit of its own: the loop guards and the number of steps end a task that goes nowhere
    built = build_agent(cfg, client=client)
    finished = threading.Event()
    if cancel is not None:
        threading.Thread(target=_pass_on, args=(cancel, built.cancel, finished), name="bench-cancel-link", daemon=True).start()
    tools = ContainerTools(exec_fn)
    tools.install(built.agent.tools, cwd_label)
    built.builder.pin("environment", environment_note + (RULES_NOTE if "rules" in guidance else ""))
    built.agent.system_prompt = built.builder.assemble_system_message()
    if "outputs" in guidance:
        watch, other = OutputWatch(instruction, exec_fn), built.agent.completion
        built.agent.completion = lambda goal, answer, work: watch(goal, answer, work) or (other(goal, answer, work) if other is not None else "")
    built.agent.reset()                                          # the conversation was started with the prompt that names this PC; it begins again with this one
    built.agent.on_event = trajectory.on_event
    outcome = AgentOutcome()
    started = time.monotonic()
    try:
        turns = built.agent.run(instruction)
        outcome.turns = len(turns)
        outcome.final = al.answer_text(turns)
        outcome.prompt_tokens = sum(t.prompt_tokens for t in turns)
        outcome.completion_tokens = sum(t.completion_tokens for t in turns)
        outcome.cached_tokens = sum(t.cached_tokens for t in turns)
        outcome.tool_calls = sum(len(t.tool_calls) for t in turns)
        outcome.stop_reason, outcome.error = built.agent.stop_reason or al.FINAL, built.agent.error or ""
    except Exception as exc:
        outcome.stop_reason, outcome.error = al.ERROR, f"{type(exc).__name__}: {exc}"
    finished.set()
    outcome.seconds = time.monotonic() - started
    trajectory.stop(stop_name(outcome.stop_reason, outcome.error), outcome.error if outcome.stop_reason != al.FINAL else "")
    return outcome


def _pass_on(source: threading.Event, target: threading.Event, finished: threading.Event) -> None:
    while not finished.is_set():
        if source.wait(0.5):
            target.set()
            return


# -- the baseline: one bash tool, no harness ----------------------------------------------------------------------------

BASH_TOOL = {"type": "function", "function": {
    "name": "bash", "description": "Runs one bash command in the container and returns its output and exit code. Each call is a new shell.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command"}}, "required": ["command"]}}}
BASELINE_SYSTEM = ("You are an autonomous software engineer working in a Linux container with one tool, bash. Run commands to look at the code, change it and check the "
                   "result. When the work is done, answer without calling a tool.")
_XML_CALL = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_XML_FUNCTION = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.DOTALL)
_XML_PARAMETER = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
BASELINE_OUTPUT = 10_000


def _calls_of(message: Mapping[str, Any]) -> List[Tuple[str, str, Any]]:
    """(call id, tool name, arguments) of a reply: its native tool calls, else the `<tool_call>` text the model template writes. The same reading as the
    harness's, so the two arms differ in what they do with the calls and not in whether a call is understood."""
    calls = []
    for c in message.get("tool_calls") or []:
        fn = c.get("function") or {}
        raw = fn.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            args = {"_unreadable": raw}
        calls.append((str(c.get("id") or f"call_{len(calls)}"), str(fn.get("name") or ""), args))
    if calls:
        return calls
    content = str(message.get("content") or "")
    for body in _XML_CALL.findall(content):
        fn = _XML_FUNCTION.search(body)
        if fn:
            calls.append((f"call_{len(calls)}", fn.group(1), {k: v for k, v in _XML_PARAMETER.findall(fn.group(2))}))
            continue
        try:
            data = json.loads(body)
            calls.append((f"call_{len(calls)}", str(data.get("name")), data.get("arguments") or {}))
        except ValueError:
            continue
    return calls


def run_bash_baseline(instruction: str, exec_fn: ExecFn, *, client: Any, trajectory: TrajectoryRecorder, max_steps: int = 150, max_seconds: Optional[float] = None,
                      cancel: Optional[threading.Event] = None, command_timeout: float = 120.0, system: str = BASELINE_SYSTEM, max_tokens: Optional[int] = None) -> AgentOutcome:
    """A bare agent loop with a single bash tool on the same model: no planning, no context management, no repeat guard, no file tools. It ends when the model
    answers without a tool call, when `max_steps` model calls are used, or when the time or the stop request comes."""
    from tadamoo.client import extract_reasoning_and_content
    tools = ContainerTools(exec_fn, command_timeout)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": instruction}]
    outcome = AgentOutcome()
    started = time.monotonic()
    reason = ""
    try:
        while True:
            if cancel is not None and cancel.is_set():
                reason = "cancelled"
                break
            if outcome.turns >= max_steps:
                reason = "max_turns"
                break
            if max_seconds and time.monotonic() - started > max_seconds:
                reason = "timeout"
                break
            reply = client.chat_completion(messages=messages, tools=[BASH_TOOL], max_tokens=max_tokens, timeout=600.0)
            outcome.turns += 1
            usage = reply.get("usage") or {}
            outcome.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            outcome.completion_tokens += int(usage.get("completion_tokens") or 0)
            outcome.cached_tokens += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            message = (reply.get("choices") or [{}])[0].get("message") or {}
            _thinking, content = extract_reasoning_and_content(message)
            calls = _calls_of(message)
            if content:
                trajectory.on_event({"type": "assistant", "content": content})
            wire = {"role": "assistant", "content": message.get("content") or ""}
            if message.get("tool_calls"):
                wire["tool_calls"] = message["tool_calls"]
            messages.append(wire)
            if not calls:
                outcome.final, reason = content, "final"
                break
            for call_id, name, args in calls:
                trajectory.on_event({"type": "tool_start", "call_id": call_id, "name": name or "unknown", "arguments": args if isinstance(args, dict) else {}})
                text, failed = _bash(tools, name, args)
                trajectory.on_event({"type": "tool_end", "call_id": call_id, "name": name or "unknown", "status": "failed" if failed else "completed",
                                     "decision": "invalid" if name != "bash" else "executed", "output": text})
                outcome.tool_calls += 1
                messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": text})
    except Exception as exc:
        reason, outcome.error = "error", f"{type(exc).__name__}: {exc}"
    outcome.stop_reason = reason
    outcome.seconds = time.monotonic() - started
    trajectory.stop(stop_name(reason, outcome.error), outcome.error)
    return outcome


def _bash(tools: ContainerTools, name: str, args: Any) -> Tuple[str, bool]:
    if name != "bash" or not isinstance(args, dict) or not isinstance(args.get("command"), str):
        return "Error: call the bash tool with a string argument `command`.", True
    out = tools.run(args["command"])
    body = (out.stdout + (("\n" + out.stderr) if out.stderr else "")).strip()
    text, _ = clip(body, BASELINE_OUTPUT)
    note = f"\n[the command timed out after {tools.default_timeout:g} seconds]" if out.timed_out else ""
    failed = out.rc != 0
    return json.dumps({"exit_code": out.rc, "stdout": text if out.rc == 0 else "", "stderr": text if out.rc != 0 else ""}) + note, failed
