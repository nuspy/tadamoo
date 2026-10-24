"""One conversation in one long-lived process: `python -m harness session`.

The server starts one of these for each conversation of the Chat and talks to it in lines of JSON: commands in on
stdin, events out on stdout. The process builds the agent once (tools, skills, connections), so a message does not pay
for the start, and saves the history after every model turn, so a process that dies can be started again from it.

Commands: {"cmd": "send", "id", "text"}, {"cmd": "cancel"}, {"cmd": "resume"}, {"cmd": "close"}, {"cmd": "ping"}.
Events: ready, accepted, agent (an assistant text or a tool start or end), retrying, done, blocked, heartbeat, error.

A failed model call is retried here, with growing waits, while the model or the server is only slow or busy; a refusal
that waiting cannot change, or retries that ran out, end the turn as `blocked` and the process waits for the person:
nothing is decided for them. A tool that cannot be undone is never run again: if the process dies while one is running, the
next start says so and puts a note in the conversation that the tool may already have run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

from harness import agent_loop as al
from harness.stack import AgentConfig, BuiltAgent, build_agent, data_dir_for

HEARTBEAT_S = 5.0
DELTA_CHARS, DELTA_S = 400, 0.25
EVENT_CHARS = 20_000
TRANSIENT_STATUS = frozenset({0, 408, 425, 429, 500, 502, 503, 504})
BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0)
HISTORY_FILE, INFLIGHT_FILE = "history.json", "inflight.json"
INTERRUPTED_NOTE = ("[harness] The previous turn was interrupted while it was running {tool} with {arguments}. It may have "
                    "run already. Do not run it again without first checking what really happened, and tell the user.")


class SessionStore:
    """The history of one conversation and the record of an action in flight, as files in one folder."""

    def __init__(self, directory: str | Path, redact: Optional[Callable[[str], str]] = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redact = redact

    def _write(self, name: str, data: Dict[str, Any]) -> None:
        text = json.dumps(data, ensure_ascii=False)
        if self.redact:
            text = self.redact(text)                 # defence in depth: a secret never reaches the disk
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.dir / name)

    def save(self, messages: List[Dict[str, Any]], meta: Dict[str, Any]) -> None:
        self._write(HISTORY_FILE, {**meta, "saved": time.time(), "messages": messages})

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads((self.dir / HISTORY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("messages"), list) else None

    def mark_inflight(self, info: Dict[str, Any]) -> None:
        self._write(INFLIGHT_FILE, {**info, "since": time.time()})

    def clear_inflight(self) -> None:
        try:
            (self.dir / INFLIGHT_FILE).unlink()
        except FileNotFoundError:
            pass

    def inflight(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads((self.dir / INFLIGHT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None


def server_health(endpoint: str, api_key: Optional[str], timeout: float = 5.0) -> Dict[str, Any]:
    """What the server says about itself: {"reachable", "loaded", "loading", "auto_load"}."""
    req = urllib.request.Request(endpoint.rstrip("/") + "/v1/status", headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return {"reachable": False, "loaded": False, "loading": False, "auto_load": False}
    return {"reachable": True, "loaded": bool(data.get("loaded")), "loading": bool(data.get("loading")),
            "auto_load": bool(data.get("auto_load"))}


class Session:
    def __init__(self, built: BuiltAgent, store: SessionStore, emit: Callable[[Dict[str, Any]], None], session_id: str = "",
                 waits: tuple = BACKOFF_S, sleep: Callable[[float], None] = time.sleep,
                 health: Optional[Callable[[], Dict[str, Any]]] = None) -> None:
        self.built, self.store, self.emit = built, store, emit
        self.agent = built.agent
        self.session_id = session_id
        self.waits, self.sleep = tuple(waits), sleep
        self.health = health or (lambda: {"reachable": True, "loaded": True, "loading": False, "auto_load": True})
        self.turn_id = ""
        self.busy = False
        self.blocked: Optional[Dict[str, Any]] = None
        self.turns_done = 0
        self.last_progress = time.monotonic()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pending_note: Optional[str] = None
        self._delta_lock = threading.Lock()
        self._delta_kind, self._delta_text, self._delta_at = "", [], time.monotonic()
        self.agent.on_event = self._on_event
        self.agent.on_turn_end = self._on_turn_end
        self.agent.on_delta = self._on_delta

    # -- the history -----------------------------------------------------------------------------------

    def _save(self) -> None:
        self.store.save(self.agent.messages[1:], {"session": self.session_id, "turns": self.turns_done})

    def restore(self) -> int:
        """Takes the saved history back. An action that cannot be undone and was running when the process died is
        reported at once, and nothing goes on until the person says so."""
        data = self.store.load()
        count = 0
        if data:
            self.agent.messages = [self.agent.messages[0], *data["messages"]]
            self.turns_done = int(data.get("turns") or 0)
            count = len(data["messages"])
        lost = self.store.inflight()
        if lost:
            self._pending_note = INTERRUPTED_NOTE.format(tool=lost.get("tool"), arguments=json.dumps(lost.get("arguments"), ensure_ascii=False)[:300])
            self.blocked = {"reason": "interrupted_irreversible", "detail": f"{lost.get('tool')} may have run before the process stopped",
                            "options": ["resume", "close"], "tool": lost.get("tool"), "arguments": lost.get("arguments")}
            self.store.clear_inflight()
        return count

    def _on_turn_end(self, turn: al.AgentTurn) -> None:
        self.turns_done += 1
        self.last_progress = time.monotonic()
        self._save()

    def _on_delta(self, kind: str, text: str) -> None:
        """The model's text as it is written, in pieces of a few hundred characters or a quarter of a second."""
        self.last_progress = time.monotonic()
        with self._delta_lock:
            if self._delta_kind and self._delta_kind != kind:
                self._flush_delta()
            self._delta_kind = kind
            self._delta_text.append(text)
            if sum(map(len, self._delta_text)) >= DELTA_CHARS or time.monotonic() - self._delta_at >= DELTA_S:
                self._flush_delta()

    def _flush_delta(self) -> None:
        if self._delta_text:
            self.emit({"event": "delta", "turn": self.turn_id, "kind": self._delta_kind, "text": "".join(self._delta_text)})
        self._delta_text, self._delta_at = [], time.monotonic()

    def _on_event(self, event: Dict[str, Any]) -> None:
        with self._delta_lock:
            self._flush_delta()                         # the pieces come before the whole message they belong to
        self.last_progress = time.monotonic()
        kind = event.get("type")
        if kind == "tool_start" and event.get("effect") == "irreversible":
            self.store.mark_inflight({"turn": self.turn_id, "tool": event.get("name"), "arguments": event.get("arguments")})
        elif kind == "tool_end":
            self.store.clear_inflight()
        shown = dict(event)
        for key in ("output", "content", "reasoning"):
            if isinstance(shown.get(key), str) and len(shown[key]) > EVENT_CHARS:
                shown[key] = shown[key][:EVENT_CHARS] + f"\n[... {len(event[key]) - EVENT_CHARS} more characters]"
        self.emit({"event": "agent", "turn": self.turn_id, "data": shown})

    # -- commands --------------------------------------------------------------------------------------

    def send(self, turn_id: str, text: str) -> bool:
        with self._lock:
            if self.busy:
                self.emit({"event": "error", "turn": turn_id, "code": "busy", "message": "a turn is still running"})
                return False
            self.busy, self.turn_id, self.blocked = True, turn_id, None
        self.agent.cancel.clear()
        if self._pending_note:
            self.agent.messages.append({"role": "user", "content": self._pending_note})
            self._pending_note = None
        self.agent.messages.append({"role": "user", "content": text})
        retrieved = self.built.retrieved_message(text)
        if retrieved:
            self.agent.messages.append(retrieved)
        self._save()                                   # the message is kept before the model sees it
        self.emit({"event": "accepted", "turn": turn_id})
        self._start(first=True)
        return True

    def resume(self) -> bool:
        """Goes on after a block, from where the conversation stands."""
        with self._lock:
            if self.busy:
                self.emit({"event": "error", "code": "busy", "message": "a turn is still running"})
                return False
            self.busy, self.blocked = True, None
        self.agent.cancel.clear()
        if self._pending_note:
            self.agent.messages.append({"role": "user", "content": self._pending_note})
            self._pending_note = None
            self._save()
        self._start(first=False)
        return True

    def cancel(self) -> None:
        self.agent.cancel.set()

    def _start(self, first: bool) -> None:
        self._worker = threading.Thread(target=self._work, name="session-turn", daemon=True)
        self._worker.start()

    # -- one turn, with its retries --------------------------------------------------------------------

    def _wait(self, seconds: float) -> bool:
        """Waits in short slices; False when the person cancelled meanwhile."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.agent.cancel.is_set():
                return False
            self.sleep(min(0.25, max(0.0, end - time.monotonic())))
        return not self.agent.cancel.is_set()

    def _work(self) -> None:
        agent = self.agent
        attempt = 0
        try:
            while True:
                self.last_progress = time.monotonic()
                turns = agent.run(None)
                if agent.stop_reason != al.ERROR:
                    break
                status = agent.error_status
                if status is None or status not in TRANSIENT_STATUS:
                    self._block("refused" if status else "error", agent.error)
                    return
                if attempt >= len(self.waits):
                    self._block("model_unavailable", f"{agent.error} (after {attempt} new tries)")
                    return
                health = self.health()
                if health["reachable"] and not health["loaded"] and not health["loading"] and not health["auto_load"]:
                    self._block("model_not_loaded", "the model is not loaded and the server will not load it by itself")
                    return
                wait = self.waits[attempt]
                attempt += 1
                self.emit({"event": "retrying", "turn": self.turn_id, "attempt": attempt, "of": len(self.waits), "wait_s": wait,
                           "status": status, "error": agent.error, "reachable": health["reachable"], "loading": health["loading"]})
                if not self._wait(wait):
                    agent.stop_reason = al.CANCELLED
                    break
            final = next((t.assistant_content for t in reversed(agent.turns) if not t.tool_calls and t.assistant_content), "")
            self._save()
            self.emit({"event": "done", "turn": self.turn_id, "stop_reason": agent.stop_reason, "error": agent.error,
                       "final": final, "tokens": agent.total_tokens})
        except BaseException as exc:                       # the process stays up: the turn ends with an error
            self._block("crash", f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self.busy = False

    def _block(self, reason: str, detail: str) -> None:
        """The turn cannot go on and waiting will not change it: the person decides what happens next."""
        self.blocked = {"reason": reason, "detail": detail, "options": ["resume", "close"]}
        self._save()
        self.emit({"event": "blocked", "turn": self.turn_id, **self.blocked})
        with self._lock:
            self.busy = False

    def close(self, timeout: float = 15.0) -> None:
        self.agent.cancel.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout)
        self._save()


# -- the process ---------------------------------------------------------------------------------------------

def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m harness session", description="One Chat conversation in a long-lived process")
    parser.add_argument("--session-id", default="", help="the conversation's id; its history is kept under it")
    parser.add_argument("--history-dir", default=None, help="where the history is kept (default: in the workspace's data folder)")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--endpoint", default="http://127.0.0.1:7232")
    parser.add_argument("--api-key", default=os.environ.get("BASALT_API_KEY"))
    parser.add_argument("--max-turns", type=int, default=15)
    parser.add_argument("--permission-mode", choices=["plan", "read_only", "default", "auto"], default="default")
    parser.add_argument("--allow", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--deny", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--secret-env", action="append", default=[], metavar="NAME")
    parser.add_argument("--skills", action="append", default=[], metavar="DIR")
    parser.add_argument("--ask-server", action="store_true", help="ask for approvals on the server's pages")
    parser.add_argument("--ask-only", action="store_true", help="answer without tools: a greeting does not become a loop")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--no-checkpoints", action="store_true")
    parser.add_argument("--heartbeat", type=float, default=HEARTBEAT_S)
    return parser.parse_args(args)


def make_emitter(out: TextIO) -> Callable[[Dict[str, Any]], None]:
    lock = threading.Lock()

    def emit(obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False)
        with lock:
            out.write(line + "\n")
            out.flush()
    return emit


def main(argv: Optional[List[str]] = None, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    args = parse_args(argv)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for stream in (stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    emit = make_emitter(stdout)
    from harness.headless import split_secret_specs
    secret_names, secret_hosts = split_secret_specs(args.secret_env)
    sid = args.session_id or uuid.uuid4().hex[:12]
    confirm = None
    if args.ask_server:
        from harness.approval_client import ServerApprover
        confirm = ServerApprover(args.endpoint, args.api_key, sid)
    cfg = AgentConfig(workspace=args.workspace, endpoint=args.endpoint, api_key=args.api_key, max_turns=args.max_turns,
                      permission_mode=args.permission_mode, allow=args.allow, deny=args.deny, secret_names=secret_names,
                      secret_hosts=secret_hosts, use_memory=not args.no_memory, use_checkpoints=not args.no_checkpoints,
                      skill_paths=args.skills, confirm=confirm, run_id=sid)
    try:
        built = build_agent(cfg)
    except Exception as exc:
        emit({"event": "error", "code": "start", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    if args.ask_only:
        from harness.tools.registry import ToolRegistry
        built.agent.tools = ToolRegistry()
    history = Path(args.history_dir) if args.history_dir else data_dir_for(Path(args.workspace).resolve()) / "sessions" / sid
    store = SessionStore(history, redact=built.agent.secrets.mask_secrets if built.agent.secrets else None)
    session = Session(built, store, emit, session_id=sid, health=lambda: server_health(cfg.endpoint, cfg.api_key))
    restored = session.restore()
    emit({"event": "ready", "session": sid, "restored": restored, "pid": os.getpid(), "blocked": session.blocked})

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(args.heartbeat):
            emit({"event": "heartbeat", "busy": session.busy, "turn": session.turn_id,
                  "idle_s": round(time.monotonic() - session.last_progress, 1) if session.busy else 0.0})
    threading.Thread(target=beat, name="session-heartbeat", daemon=True).start()

    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError:
                emit({"event": "error", "code": "bad_command", "message": "not JSON"})
                continue
            name = cmd.get("cmd") if isinstance(cmd, dict) else None
            if name == "send" and isinstance(cmd.get("text"), str) and cmd["text"].strip():
                session.send(str(cmd.get("id") or uuid.uuid4().hex[:8]), cmd["text"])
            elif name == "cancel":
                session.cancel()
            elif name == "resume":
                session.resume()
            elif name == "ping":
                emit({"event": "pong", "busy": session.busy})
            elif name == "close":
                break
            else:
                emit({"event": "error", "code": "bad_command", "message": f"unknown command: {name!r}"})
    finally:
        stop.set()
        session.close()
        built.close()
    emit({"event": "closed", "session": sid})
    return 0


if __name__ == "__main__":
    sys.exit(main())
