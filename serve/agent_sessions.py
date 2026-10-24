"""The Chat's conversations with the agent: one long-lived process each, watched and started again when needed.

Each conversation is `python -m harness session`, spoken to in lines of JSON (see harness/session.py). This keeps the
list of conversations, starts and stops the processes, buffers their events for the page, and supervises them:

* a process that dies or stops giving signs of life is started again from its saved history, and the turn it was in goes
  on; a message it never confirmed is sent again; at most `restart_cap` restarts in `restart_window_s`, then the
  conversation is blocked and waits for the person;
* a process whose turn makes no progress for `hang_s` is ended and started again;
* a conversation idle for `idle_s` has its process closed (it is dormant: the history stays, the next message wakes it);
* a confirmation the process asks (a change to a plan, a risky action) is kept with its buttons until the person answers; the
  conversation is `asking` meanwhile and a turn waiting for the person is never taken for a stuck one;
* when the process says it is blocked (a refusal, retries that ran out, an action that cannot be undone and was running
  when it died), the conversation waits for the person: nothing is decided for them and nothing is run again.
"""
from __future__ import annotations

import collections
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from harness import context_patterns as context_patterns_mod
from serve import ROOT
from serve.agent_runs import MAX_ALLOW, MODES, SAFE_ENV_KEYS, RunBusy, RunError
from serve.approvals import ApprovalBoard
from serve.tasks import _write_json
from serve.util.winjob import contain

TEXT_CHARS = 20_000
EVENTS_KEPT = 3000
SESSIONS_FILE = "agent_sessions.json"
TITLE_CHARS = 80
OPTIONS = ("resume", "close")
ASK_FIELDS = ("id", "mode", "kind", "question", "options", "facts", "tool", "arguments")


class Live:
    """What exists only while a process runs."""

    def __init__(self, proc: "subprocess.Popen[str]") -> None:
        self.proc = proc
        self.wlock = threading.Lock()
        self.ready = threading.Event()
        self.last_seen = time.monotonic()           # any line from the process
        self.idle_s = 0.0                           # how long its turn has gone without news, from its heartbeat
        self.ending = False                         # the manager is ending it on purpose
        self.after_ready: List[Dict[str, Any]] = []   # commands to give it as soon as it says ready


class SessionManager:
    def __init__(self, directory: str | Path, endpoint: str, api_key: str = "", board: Optional[ApprovalBoard] = None,
                 python: Optional[str] = None, root: Path = ROOT, command: Optional[Callable[[str, Dict[str, Any], Path], List[str]]] = None,
                 idle_s: float = 1800.0, hang_s: float = 1200.0, beat_timeout_s: float = 45.0, restart_cap: int = 3,
                 restart_window_s: float = 600.0, tick_s: float = 1.0, start_wait_s: float = 60.0,
                 default_workspace: Optional[Path] = None, router: str = "model", pipeline: str = "full") -> None:
        self.directory = Path(directory) / "sessions"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.endpoint, self.api_key, self.board = endpoint, api_key, board
        self.router = router                                    # how a message that comes during a task is judged: "model" or "rules"
        self.pipeline = pipeline                                # the check of an answer against its request: "off", "light" or "full"
        self.default_workspace = Path(default_workspace) if default_workspace else None     # where a conversation works when none is named
        self.python, self.root = python or sys.executable, Path(root)
        self.command = command or self._command
        self.idle_s, self.hang_s, self.beat_timeout_s = idle_s, hang_s, beat_timeout_s
        self.restart_cap, self.restart_window_s, self.tick_s, self.start_wait_s = restart_cap, restart_window_s, tick_s, start_wait_s
        self._lock = threading.RLock()
        self._live: Dict[str, Live] = {}
        self._events: Dict[str, Deque[Dict[str, Any]]] = {}
        self._seq: Dict[str, int] = {}
        self._restarts: Dict[str, List[float]] = {}
        self._unconfirmed: Dict[str, Dict[str, Any]] = {}      # the message sent and not yet accepted
        self.records: List[Dict[str, Any]] = self._load()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._supervise, name="session-supervisor", daemon=True)
        self._thread.start()

    # -- the list of conversations -----------------------------------------------------------------------

    def _load(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads((self.directory / SESSIONS_FILE).read_text(encoding="utf-8"))
            items = data.get("sessions") if isinstance(data, dict) else None
        except (OSError, ValueError):
            items = None
        out = [r for r in (items or []) if isinstance(r, dict) and r.get("id")]
        for r in out:
            if r.get("state") not in ("closed",):
                r["state"] = "dormant"                       # no process survives the server
            r.pop("ask", None)                                # and a confirmation it was waiting for goes with it
        return out

    def _save(self) -> None:
        for attempt in range(5):
            try:
                _write_json(self.directory / SESSIONS_FILE, {"schema": "1.0", "sessions": self.records})
                return
            except PermissionError:                      # a reader or a scanner holds the file for a moment
                if attempt == 4:
                    raise
                time.sleep(0.02)

    def _record(self, sid: str) -> Dict[str, Any]:
        rec = next((r for r in self.records if r["id"] == sid), None)
        if rec is None:
            raise RunError("That conversation is not in the list any more.", "gone")
        return rec

    def _set(self, rec: Dict[str, Any], **changes: Any) -> None:
        rec.update(changes, updated=time.time())
        self._save()

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return {"sessions": [dict(r) for r in sorted(self.records, key=lambda r: r.get("updated", 0), reverse=True)]}

    def get(self, sid: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._record(sid))

    def history_dir(self, sid: str) -> Path:
        return self.directory / sid

    def history(self, sid: str) -> Dict[str, Any]:
        """The saved messages of a conversation, for the page to show (the system prompt left out)."""
        with self._lock:
            rec = self._record(sid)
        data = self._read_history(sid)
        if data is None:
            return {"messages": [], "turns": 0, "session": dict(rec)}
        shown = []
        for m in self._transcript(sid, data):
            shown.append({k: (v[:TEXT_CHARS] if isinstance(v, str) else v) for k, v in m.items() if k in ("role", "content", "tool_calls", "name", "tool_call_id")})
        return {"messages": shown, "turns": data.get("turns", 0), "session": dict(rec)}

    def _read_history(self, sid: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads((self.history_dir(sid) / "history.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _transcript(self, sid: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """What the person said and was answered, whole: the turns pruned or summarized away for the model come back first,
        a request the model saw reframed shows as the person wrote it, and the harness's own notes are left out."""
        try:
            archived = [m for m in (json.loads(ln) for ln in (self.history_dir(sid) / "archive.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip())
                        if isinstance(m, dict)]
        except (OSError, ValueError):
            archived = []
        out = []
        for m in [*archived, *data.get("messages", [])]:
            if not isinstance(m, dict) or m.get("role") == "system" or m.get("summary") or m.get("note"):
                continue
            text = m.get("content")
            if m.get("role") == "user" and isinstance(text, str) and text.startswith("<request>\n") and "\n</request>" in text:
                m = {**m, "content": text[len("<request>\n"):text.index("\n</request>")]}
            out.append(m)
        return out

    def branch(self, sid: str, turn: Any) -> Dict[str, Any]:
        """A new conversation with the same settings and the history up to the end of the person's `turn`-th request. The first
        one is not touched."""
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            raise RunError("Say which request to branch after: a number from 1.", "bad_turn")
        with self._lock:
            rec = self._record(sid)
        data = self._read_history(sid)
        if data is None:
            raise RunError("That conversation has nothing to copy yet.", "empty")
        kept: List[Dict[str, Any]] = []
        users = 0
        for m in self._transcript(sid, data):
            if m.get("role") == "user":
                users += 1
                if users > turn:
                    break
            kept.append({k: v for k, v in m.items() if k not in ("masked", "stripped")})
        if users < turn:
            raise RunError(f"That conversation has only {users} requests.", "bad_turn")
        new = secrets.token_hex(4)
        child = {**rec, "id": new, "title": ((rec.get("title") or "") + " (branch)").strip()[:TITLE_CHARS], "branched_from": {"session": sid, "turn": turn},
                 "created": time.time(), "updated": time.time(), "state": "dormant", "blocked": None, "turns": turn}
        with self._lock:
            target = self.history_dir(new)
            target.mkdir(parents=True, exist_ok=True)
            _write_json(target / "history.json", {"session": new, "turns": turn, "saved": time.time(), "messages": kept, "pending": []})
            self.records.append(child)
            self._events[new], self._seq[new] = collections.deque(maxlen=EVENTS_KEPT), 0
            self._save()
        return dict(child)

    def snapshots(self, sid: str) -> Dict[str, Any]:
        with self._lock:
            self._record(sid)
        snaps = context_patterns_mod.Snapshots(self.history_dir(sid) / "snapshots")
        return {"snapshots": [{"n": r["n"], "time": r["time"], "hash": r["hash"][:16], "messages": len(r["messages"])} for r in snaps.list()],
                "intact": snaps.verify()}

    def snapshot_diff(self, sid: str, a: Any, b: Any) -> Dict[str, Any]:
        with self._lock:
            self._record(sid)
        try:
            return context_patterns_mod.Snapshots(self.history_dir(sid) / "snapshots").diff(int(a), int(b))
        except (KeyError, TypeError, ValueError):
            raise RunError("There is no such snapshot.", "gone") from None

    # -- creating ----------------------------------------------------------------------------------------

    def create(self, workspace: Any, mode: Any = "default", allow: Any = None, ask_only: bool = False, title: Any = "",
               confirm_auto: bool = False, max_turns: Any = 15, context_patterns: Any = None) -> Dict[str, Any]:
        from serve.agent_runs import AgentRunner
        if (workspace is None or workspace == "") and self.default_workspace is not None:
            self.default_workspace.mkdir(parents=True, exist_ok=True)
            workspace = str(self.default_workspace)
        path = AgentRunner.check_workspace(workspace)
        if mode not in MODES:
            raise RunError("The mode must be one of: " + ", ".join(MODES) + ".", "bad_mode")
        if mode == "auto" and confirm_auto is not True:
            raise RunError("Automatic mode runs commands without asking. Confirm that you want that, in a folder you can lose.", "auto_confirm")
        rules = allow if isinstance(allow, list) else []
        if len(rules) > MAX_ALLOW or any(not isinstance(r, str) or not r.strip() or len(r) > 200 or "\n" in r for r in rules):
            raise RunError(f"Allow rules are lines of text, at most {MAX_ALLOW}.", "bad_rules")
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 50:
            raise RunError("The number of steps must be between 1 and 50.", "bad_steps")
        patterns = context_patterns_mod.check_config(context_patterns) if context_patterns else {}
        sid = secrets.token_hex(4)
        rec = {"id": sid, "context_patterns": patterns, "title": str(title or "").strip()[:TITLE_CHARS], "workspace": str(path), "mode": mode,
               "allow": [r.strip() for r in rules], "ask_only": bool(ask_only), "max_turns": max_turns,
               "created": time.time(), "updated": time.time(), "state": "dormant", "blocked": None, "turns": 0}
        with self._lock:
            self.records.append(rec)
            self._events[sid], self._seq[sid] = collections.deque(maxlen=EVENTS_KEPT), 0
            self._save()
        return dict(rec)

    # -- starting a process ------------------------------------------------------------------------------

    def _command(self, sid: str, rec: Dict[str, Any], history: Path) -> List[str]:
        cmd = [self.python, "-m", "harness", "session", "--session-id", sid, "--history-dir", str(history), "--workspace", rec["workspace"],
               "--endpoint", self.endpoint, "--permission-mode", rec["mode"], "--max-turns", str(rec.get("max_turns", 15)), "--router", self.router, "--pipeline", self.pipeline, "--capabilities"]
        for rule in rec.get("allow", []):
            cmd += ["--allow", rule]
        if rec.get("ask_only"):
            cmd.append("--ask-only")
        if rec.get("context_patterns"):
            cmd += ["--context-patterns", json.dumps(rec["context_patterns"])]
        return cmd

    def _spawn(self, sid: str) -> Live:
        rec = self._record(sid)
        history = self.history_dir(sid)
        history.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_KEYS}
        env.update({"PYTHONUTF8": "1", "PYTHONPATH": str(self.root)})
        if self.api_key:
            env["BASALT_API_KEY"] = self.api_key
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
        log = open(history / "process.log", "ab")
        try:
            proc = subprocess.Popen(self.command(sid, rec, history), cwd=str(self.root), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=log, text=True, encoding="utf-8", bufsize=1, creationflags=flags, start_new_session=(os.name != "nt"))
        except OSError as exc:
            raise RunError(f"The agent could not be started: {exc.strerror or exc}", "start_failed") from None
        finally:
            log.close()
        contain(proc)
        live = Live(proc)
        self._live[sid] = live
        threading.Thread(target=self._read, args=(sid, live), name=f"session-{sid}", daemon=True).start()
        return live

    def _ensure(self, sid: str) -> Live:
        with self._lock:
            live = self._live.get(sid)
            if live is not None and live.proc.poll() is None and not live.ending:
                return live
            self._set(self._record(sid), state="starting")
            return self._spawn(sid)

    # -- what the process says ---------------------------------------------------------------------------

    def _push(self, sid: str, event: Dict[str, Any]) -> None:
        with self._lock:
            self._seq[sid] = self._seq.get(sid, 0) + 1
            event = {**event, "seq": self._seq[sid], "t": time.time()}
            self._events.setdefault(sid, collections.deque(maxlen=EVENTS_KEPT)).append(event)

    def _read(self, sid: str, live: Live) -> None:
        try:
            for line in live.proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self._on_event(sid, live, event)
        except (OSError, ValueError):
            pass
        self._on_exit(sid, live)

    def _on_event(self, sid: str, live: Live, event: Dict[str, Any]) -> None:
        live.last_seen = time.monotonic()
        kind = event.get("event")
        with self._lock:
            rec = next((r for r in self.records if r["id"] == sid), None)
            if rec is None or self._live.get(sid) is not live:
                return
            if kind == "heartbeat":
                live.idle_s = float(event.get("idle_s") or 0.0)
                waiting, aside = int(event.get("waiting_requests") or 0), int(event.get("put_aside") or 0)
                if (rec.get("waiting_requests", 0), rec.get("put_aside", 0)) != (waiting, aside):
                    self._set(rec, waiting_requests=waiting, put_aside=aside)       # what waits behind the task in hand
                return
            if kind == "ready":
                blocked = event.get("blocked")
                pending, live.after_ready = live.after_ready, []
                self._set(rec, state="blocked" if blocked else "busy" if pending else "idle", blocked=blocked)
                live.ready.set()
                if not blocked:                              # a block is the person's to lift, so nothing is given on
                    for cmd in pending:
                        self._write(sid, live, cmd)
            elif kind in ("accepted", "woke"):                        # a message of the person, or a note of the planner
                self._unconfirmed.pop(sid, None)
                self._set(rec, state="busy", blocked=None, ask=None, question=None)
            elif kind == "ask":
                shown = {k: event.get(k) for k in ASK_FIELDS}
                if event.get("mode") == "confirm":
                    self._set(rec, state="asking", ask=shown)
                else:
                    self._set(rec, question=shown)
            elif kind == "queued":
                self._unconfirmed.pop(sid, None)                     # the process has the message; it will look at it before its next model call
            elif kind in ("started", "resumed"):
                if rec["state"] not in ("asking", "blocked"):
                    self._set(rec, state="busy")
            elif kind == "done":
                asked = event.get("stop_reason") == "asked"
                state = "busy" if event.get("more") else "idle"      # other requests are still waiting: the conversation is still at work
                self._set(rec, state=state, blocked=None, ask=None, turns=rec.get("turns", 0) + 1, **({} if asked else {"question": None}),
                          **({} if event.get("more") else {"waiting_requests": 0, "put_aside": 0}))
            elif kind == "cleared":
                self._set(rec, waiting_requests=0, put_aside=0)
            elif kind == "blocked":
                self._set(rec, state="blocked", ask=None, blocked={k: event.get(k) for k in ("reason", "detail", "options", "tool", "arguments")})
            elif kind == "closed":
                live.ending = True
        self._push(sid, event)

    def _on_exit(self, sid: str, live: Live) -> None:
        code = live.proc.wait()
        if self._stop.is_set():                              # the server is ending: the next start finds it dormant
            return
        with self._lock:
            if self._live.get(sid) is live:
                self._live.pop(sid, None)
            rec = next((r for r in self.records if r["id"] == sid), None)
        if self.board:
            self.board.refuse_all(sid)
        if rec is None or live.ending:
            if rec is not None and rec["state"] != "closed":
                with self._lock:
                    self._set(rec, state="dormant")
            return
        self._died(sid, rec, f"the process ended (exit code {code})")

    def _died(self, sid: str, rec: Dict[str, Any], why: str) -> None:
        """A process that was meant to be there is gone: start it again from its history, or ask the person."""
        with self._lock:
            was_busy = rec["state"] in ("busy", "starting", "asking")
            rec.pop("ask", None)
            if rec["state"] == "blocked" or not was_busy:   # waiting for the person, or nothing running: it starts at the next message
                self._set(rec, state="dormant")
                return
            now = time.monotonic()
            recent = [t for t in self._restarts.get(sid, []) if now - t < self.restart_window_s]
            if len(recent) >= self.restart_cap:
                self._restarts[sid] = recent
                blocked = {"reason": "process_unstable", "detail": f"{why}; it was started again {len(recent)} times in "
                           f"{int(self.restart_window_s // 60)} minutes", "options": list(OPTIONS)}
                self._set(rec, state="blocked", blocked=blocked)
                self._push(sid, {"event": "blocked", **blocked})
                return
            self._restarts[sid] = recent + [now]
            self._push(sid, {"event": "restarting", "why": why, "attempt": len(recent) + 1, "of": self.restart_cap})
            unconfirmed = self._unconfirmed.get(sid)
            try:
                live = self._spawn(sid)
            except RunError as exc:
                blocked = {"reason": "start_failed", "detail": str(exc), "options": list(OPTIONS)}
                self._set(rec, state="blocked", blocked=blocked)
                self._push(sid, {"event": "blocked", **blocked})
                return
            self._set(rec, state="starting")
            live.after_ready = [{"cmd": "send", "id": unconfirmed["id"], "text": unconfirmed["text"]}] if unconfirmed else [{"cmd": "resume"}]

    def _write(self, sid: str, live: Live, cmd: Dict[str, Any]) -> bool:
        try:
            with live.wlock:
                live.proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
                live.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    # -- the page's commands -----------------------------------------------------------------------------

    def send(self, sid: str, text: Any) -> Dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise RunError("Write a message.", "text_missing")
        if len(text) > TEXT_CHARS:
            raise RunError(f"The message is too long ({len(text)} characters; at most {TEXT_CHARS}).", "text_long")
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            turn = secrets.token_hex(3)              # a message that comes while the agent works is looked at by the agent itself
            live = self._ensure(sid)
            cmd = {"cmd": "send", "id": turn, "text": text}
            self._unconfirmed[sid] = cmd
            if not rec.get("title"):
                rec["title"] = " ".join(text.split())[:TITLE_CHARS]
            keep = rec["state"] in ("busy", "asking") and live.ready.is_set()       # a task is under way: its state is not changed
            self._set(rec, blocked=None, question=None, **({} if keep else {"state": "starting" if not live.ready.is_set() else "busy"}))
            if not live.ready.is_set():
                live.after_ready.append(cmd)
            elif not self._write(sid, live, cmd):
                raise RunError("The agent process did not take the message; try again.", "write_failed")
        self._push(sid, {"event": "user", "turn": turn, "text": text})
        return {"turn": turn, "session": self.get(sid)}

    def answer(self, sid: str, ask_id: Any, value: Any) -> Dict[str, Any]:
        """The person's answer to a confirmation: it must be one of the buttons of the question that is waiting."""
        with self._lock:
            rec = self._record(sid)
            ask = rec.get("ask")
            live = self._live.get(sid)
            if not ask or live is None or ask.get("id") != ask_id:
                raise RunError("That question is not waiting for an answer any more.", "not_asking")
            if value not in [o.get("value") for o in ask.get("options") or []]:
                raise RunError("That is not one of the answers.", "bad_answer")
            self._set(rec, state="busy", ask=None)
            if not self._write(sid, live, {"cmd": "answer", "id": ask_id, "value": value}):
                raise RunError("The agent process did not take the answer; try again.", "write_failed")
        self._push(sid, {"event": "answered", "ask": ask_id, "value": value})
        return self.get(sid)

    def promote(self, sid: str, turn: Any) -> Dict[str, Any]:
        """A request that waits behind the task in hand becomes urgent: the task is put aside at its next safe point."""
        return self._tell(sid, {"cmd": "promote", "id": str(turn)})

    def drop(self, sid: str, turn: Any) -> Dict[str, Any]:
        """Takes a waiting request out of the queue."""
        return self._tell(sid, {"cmd": "drop", "id": str(turn)})

    def _tell(self, sid: str, cmd: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._record(sid)
            live = self._live.get(sid)
            if live is None or not live.ready.is_set():
                raise RunError("That conversation is not running.", "not_running")
            self._write(sid, live, cmd)
        return self.get(sid)

    def cancel(self, sid: str) -> Dict[str, Any]:
        with self._lock:
            self._record(sid)
            live = self._live.get(sid)
            if live is None:
                raise RunError("That conversation is not running.", "not_running")
            self._write(sid, live, {"cmd": "cancel"})
        return self.get(sid)

    def resume(self, sid: str) -> Dict[str, Any]:
        """The person's answer to a block: go on from where the conversation stands."""
        with self._lock:
            rec = self._record(sid)
            if rec["state"] != "blocked":
                raise RunError("That conversation is not waiting for you.", "not_blocked")
            live = self._ensure(sid)
            self._set(rec, state="starting" if not live.ready.is_set() else "busy", blocked=None)
            if not live.ready.is_set():
                live.after_ready.append({"cmd": "resume"})
            else:
                self._write(sid, live, {"cmd": "resume"})
        return self.get(sid)

    def close(self, sid: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._record(sid)
            live = self._live.get(sid)
            if live is not None:
                live.ending = True
                self._write(sid, live, {"cmd": "close"})
            self._set(rec, state="closed", blocked=None)
        if live is not None:
            self._end(live)
        if self.board:
            self.board.refuse_all(sid)
        return self.get(sid)

    def _end(self, live: Live, wait: float = 5.0) -> None:
        try:
            live.proc.wait(wait)
        except subprocess.TimeoutExpired:
            from serve.agent_runs import _kill_tree
            _kill_tree(live.proc)

    def events(self, sid: str, after: int = 0) -> Dict[str, Any]:
        with self._lock:
            rec = self._record(sid)
            items = [e for e in self._events.get(sid, ()) if e["seq"] > after]
            return {"events": items, "next": self._seq.get(sid, 0), "state": rec["state"], "blocked": rec.get("blocked")}

    # -- the supervisor ----------------------------------------------------------------------------------

    def _supervise(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.check()
            except Exception:
                pass

    def check(self, now: Optional[float] = None) -> None:
        """One round of looking at every process (public so the tests can drive it)."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            live_items = list(self._live.items())
            records = {r["id"]: r for r in self.records}
        from serve.agent_runs import _kill_tree
        for sid, live in live_items:
            rec = records.get(sid)
            if rec is None or live.ending or live.proc.poll() is not None:
                continue
            silent = now - live.last_seen
            hung = rec["state"] == "busy" and live.idle_s >= self.hang_s
            if silent >= self.beat_timeout_s or hung:
                self._push(sid, {"event": "stalled", "why": "no sign of life for %d s" % silent if silent >= self.beat_timeout_s
                                 else f"the turn made no progress for {int(live.idle_s)} s"})
                _kill_tree(live.proc)                            # the reader sees the end and starts it again
            elif rec["state"] == "idle" and time.time() - rec.get("updated", 0) >= self.idle_s:
                with self._lock:
                    live.ending = True
                    self._write(sid, live, {"cmd": "close"})
                    self._set(rec, state="dormant")
                self._end(live)

    def shutdown(self) -> None:
        """The server ends: every process ends with it, and the conversations are found again at the next start."""
        self._stop.set()
        with self._lock:
            lives = list(self._live.values())
            for live in lives:
                live.ending = True
        from serve.agent_runs import _kill_tree
        for live in lives:
            self._write("", live, {"cmd": "close"})
        for live in lives:
            self._end(live, 3.0)
            if live.proc.poll() is None:
                _kill_tree(live.proc)
