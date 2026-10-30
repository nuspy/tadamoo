"""What the case studies share for running the agent: a client that counts, an in-process conversation for the Target Loop, a session for
the experiments that need the conversation process, and the measures taken from a transcript."""
from __future__ import annotations

import difflib
import json
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness import agent_loop as al
from harness import target as tg
from harness.session import Session, SessionStore
from harness.stack import AgentConfig, BuiltAgent, build_agent

FIXED_NOW = datetime(2026, 10, 30, 10, 0)                  # a Friday: the clock of the experiments that read dates
SKIP_DIRS = {".basalt", "__pycache__", ".git", ".pytest_cache"}


class CountingClient:
    """Wraps a model client and adds up the usage of every call, whoever makes it (the agent, the Target Loop's planner and judge, the triage)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.prompt_tokens = self.completion_tokens = self.cached_tokens = self.calls = 0
        self._lock = threading.Lock()

    def chat_completion(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        reply = self.inner.chat_completion(*args, **kwargs)
        usage = reply.get("usage") or {} if isinstance(reply, dict) else {}
        with self._lock:
            self.calls += 1
            self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            self.completion_tokens += int(usage.get("completion_tokens") or 0)
            self.cached_tokens += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        return reply

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def totals(self) -> Dict[str, float]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens, "cached_tokens": self.cached_tokens, "model_calls": self.calls}


def wasted_turns(turns: Sequence[al.AgentTurn]) -> int:
    """Turns that moved nothing: every call in them was invalid, refused, stopped as a loop or failed, or repeated an earlier call with the same
    arguments. A turn with no call (an answer) is never wasted."""
    seen = set()
    wasted = 0
    for turn in turns:
        if not turn.tool_calls:
            continue
        useless = True
        for c in turn.tool_calls:
            key = (c.tool_name, json.dumps(c.arguments, sort_keys=True, default=str))
            fresh = c.decision == "executed" and not c.is_error and key not in seen
            seen.add(key)
            if fresh:
                useless = False
        wasted += useless
    return wasted


def transcript_measures(turns: Sequence[al.AgentTurn]) -> Dict[str, float]:
    return {"turns": len(turns), "tool_calls": sum(len(t.tool_calls) for t in turns), "wasted_turns": wasted_turns(turns),
            "prompt_tokens": sum(t.prompt_tokens for t in turns), "completion_tokens": sum(t.completion_tokens for t in turns),
            "cached_tokens": sum(t.cached_tokens for t in turns)}


def write_files(root: Path, files: Dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return root


def apply_solution(root: Path, solution: Dict[str, Optional[str]]) -> None:
    """A reference result put into a workspace: whole files written, a file with None deleted."""
    for rel, text in solution.items():
        target = root / rel
        if text is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))


def clear_bytecode(root: Path) -> None:
    """Python's compiled files go before a final check: an edit made in the same second as the run before it, to a file of the same size, would otherwise be read stale."""
    for folder in list(root.rglob("__pycache__")):
        shutil.rmtree(folder, ignore_errors=True)


def snapshot(root: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not (set(path.relative_to(root).parts) & SKIP_DIRS):
            out[path.relative_to(root).as_posix()] = path.read_bytes().decode("utf-8", errors="replace")
    return out


def diff_entries(before: Dict[str, str], after: Dict[str, str], when: float) -> List[Dict[str, Any]]:
    """The file changes between two snapshots, in the shape of the server's change journal."""
    out = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if a == b:
            continue
        lines = list(difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(), lineterm="", n=1))
        out.append({"kind": "added" if a is None else "deleted" if b is None else "modified", "file": name, "time": when,
                    "added": sum(1 for x in lines if x.startswith("+") and not x.startswith("+++")),
                    "removed": sum(1 for x in lines if x.startswith("-") and not x.startswith("---")), "diff": "\n".join(lines[:200])})
    return out


def open_agent(workspace: Path, client: Any, data_dir: Path, **overrides: Any) -> BuiltAgent:
    """An agent on a throwaway workspace, built as the evaluation runner builds it; `overrides` are fields of AgentConfig (the arms' settings)."""
    settings: Dict[str, Any] = {"permission_mode": "default", "allow": ["shell_command:python *"], "use_memory": False, "use_checkpoints": False,
                                "budget_grace": False, "max_seconds": 1800.0}
    settings.update(overrides)
    return build_agent(AgentConfig(workspace=str(workspace), data_dir=str(data_dir), **settings), client=client)


class AgentConversation:
    """The Target Loop's view of an agent running in this process: a prompt in, the answer and what the tools did out. The agent's conversation
    goes on from one prompt to the next, as the Chat's does. A stop of the loop takes effect between prompts."""

    def __init__(self, built: BuiltAgent, workspace: Path) -> None:
        self.built, self.root = built, Path(workspace)
        self.turns: List[al.AgentTurn] = []
        self.said: List[Dict[str, Any]] = []
        self.edits: List[Dict[str, Any]] = []
        self._calls: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        built.agent.on_event = self._event

    def _event(self, d: Dict[str, Any]) -> None:
        if d.get("type") == "tool_start":
            self._calls[d.get("call_id")] = {"name": d.get("name"), "arguments": d.get("arguments") or {}, "status": "running", "output": "",
                                             **({"effect": d["effect"]} if d.get("effect") else {}), **({"gate": True} if d.get("gate") else {})}
            self._order.append(d.get("call_id"))
        elif d.get("type") == "tool_end" and d.get("call_id") in self._calls:
            self._calls[d["call_id"]].update(status=d.get("status") or "completed", output=str(d.get("output") or "")[:4000])

    def run(self, prompt: str, cancel: threading.Event) -> tg.TurnResult:
        if cancel.is_set():
            return tg.TurnResult(stop_reason="cancelled")
        self._calls, self._order = {}, []
        before = snapshot(self.root)
        turns = self.built.agent.run(prompt)
        self.turns += turns
        self.edits += diff_entries(before, snapshot(self.root), time.time())
        agent = self.built.agent
        return tg.TurnResult(final=al.answer_text(turns), stop_reason=agent.stop_reason or "final", error=agent.error,
                             tools=[self._calls[c] for c in self._order if c in self._calls])

    def cancel(self) -> None:
        self.built.agent.cancel.set()

    def file_exists(self, path: str) -> bool:
        p = (self.root / path).resolve()
        return p.is_file() and self.root.resolve() in p.parents

    def parked(self) -> List[str]:
        return []

    def changes_since(self, since: float) -> List[Dict[str, Any]]:
        return [e for e in self.edits if e["time"] >= since]

    def say(self, event: Dict[str, Any]) -> None:
        self.said.append(event)


class Events:
    """The events a session emits, kept in order, with a wait for one kind."""

    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []
        self.cond = threading.Condition()

    def __call__(self, event: Dict[str, Any]) -> None:
        with self.cond:
            self.items.append(event)
            self.cond.notify_all()

    def wait(self, kind: str, timeout: float, after: int = 0) -> Optional[Dict[str, Any]]:
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                for e in self.items[after:]:
                    if e.get("event") == kind:
                        return e
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(left)

    def count(self, kind: str) -> int:
        return sum(1 for e in self.items if e.get("event") == kind)


def open_session(workspace: Path, client: Any, data_dir: Path, *, waits: Tuple[float, ...] = (0.05,), classify_errors: bool = False,
                 unattended: bool = False, **overrides: Any) -> Tuple[Session, Events, BuiltAgent]:
    """A conversation process in this thread's process: an agent, its history folder and a session over them. The fixed clock is set."""
    settings: Dict[str, Any] = {"permission_mode": "auto", "max_turns": 12, "use_memory": False, "use_checkpoints": False, "budget_grace": False}
    settings.update(overrides)
    built = build_agent(AgentConfig(workspace=str(workspace), data_dir=str(data_dir), **settings), client=client)
    events = Events()
    store = SessionStore(data_dir / "hist", redact=built.agent.secrets.mask_secrets)
    session = Session(built, store, events, session_id="case", waits=waits, unattended=unattended, classify_errors=classify_errors)
    built.agent.clock = lambda: FIXED_NOW
    return session, events, built


def close_quietly(*things: Any) -> None:
    for thing in things:
        try:
            thing.close()
        except Exception:
            pass
