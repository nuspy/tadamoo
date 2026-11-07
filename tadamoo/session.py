"""One conversation in one long-lived process: `python -m tadamoo session`.

The server starts one of these for each conversation of the Chat and talks to it in lines of JSON: commands in on
stdin, events out on stdout. The process builds the agent once (tools, skills, connections), so a message does not pay
for the start, and saves the history after every model turn, so a process that dies can be started again from it.

Commands: {"cmd": "send", "id", "text"}, {"cmd": "answer", "id", "value"}, {"cmd": "cancel"}, {"cmd": "resume"}, {"cmd": "close"},
{"cmd": "promote", "id"}, {"cmd": "drop", "id"}, {"cmd": "ping"}, {"cmd": "mode", "mode"} (the permission mode of the conversation),
{"cmd": "role", "role"} (what the agent is for: tadamoo/roles.py), {"cmd": "checks", "safe", "checks"} (the commands that run without a question and the accepted checks of the verify gate),
{"cmd": "effort", "level"} (how long the model thinks), {"cmd": "intake", "mode"} (off, rules or full: how a request is looked at), {"cmd": "optimize", "on"} (whether a message is rewritten by the technical session first; a "send" may carry "optimize": false), {"cmd": "settings", "max_turns", "end_turn_bias", "temperature", "max_tokens"} (the steps one answer may take,
-1 for no limit, the end-of-turn bias, the sampling temperature, null for the model file's own, and the most a reply may write, null for the server's own; any of them), {"cmd": "command", "id", "name", "args"} (a slash command: tadamoo/commands.py),
{"cmd": "skill", "id", "name", "display", "script", "values", "timeout", "cache_dir"} (runs a personal skill in the conversation).
Events: ready, accepted, queued, routed, started, suspended, resumed, agent (an assistant text or a tool start or end), delta
(text as it is written), ask (a confirmation the task waits for, or a card of questions that ends the turn), intake (what the look at a request assumed
or recorded), optimizing and optimized (the technical session's rewrite of the message is being made; then the rewrite, or why it was not used), suggestions (the next-message chips after an answer),
retrying, model_error (only with the error classifier on), done (with `more` when other tasks are waiting), blocked, cleared, dropped, mode, role, checks, effort, intake_mode, optimize_prompt, settings, command, skill, skill_say, heartbeat, error.

A message that comes while a task is under way is looked at before the task's next model call (tadamoo/router.py): it goes into
that task (steer), waits its turn (queue), or, when it cannot wait and has nothing to do with the task, the task is put aside where
it stands, the urgent one is done, and the first goes on afterwards. When the model answers several requests at once, a request that
would wait is done beside the task instead.

A failed model call is retried here, with growing waits that never end by themselves, unless the
error classifier (on by default, `--no-error-classifier` turns it off) finds that waiting cannot cure it, and then the turn ends:
the `retrying` event says what is waited for, and a server that is up with no model loaded is asked to load one (the Loader's refusal is in the event); nothing
about the model ends the turn as `blocked`, and the person can always stop it. If the process dies while a tool that cannot be undone
is running, the next start puts a note in the conversation that the call may already have run and must be checked before it is
repeated; the work itself goes on.

A session flagged `unattended` (--unattended) never waits for a question: one that is not a named exception (another person, the
user, a resource that is not the PC's own, a booking) is answered at once as approved, or as "not available" when only the person
could give the data; a named exception is parked in a visible list (`pending` events, answered later with the same `answer` command)
while the model is told the answer is pending; the `ask_user` tool does not end the turn: each question is answered at once with the
option the model recommended (or its default text, or its recommended side), what involves other people, a booking or a resource is
parked, and the rest is left to the model's own judgement, with every choice the harness made reported as an assumption. A conversation that is not flagged always waits for the person: at the Auto level the harness itself asks
nothing, and what is still asked (the Rule of Two the person set) is theirs to answer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

from tadamoo import agent_loop as al
from tadamoo import error_classifier
from tadamoo import intake as it
from tadamoo import nextsteps as ns
from tadamoo import optimize as opt
from tadamoo import progress as prog
from tadamoo import provenance as prov
from tadamoo import side_uses
from serve import longflow as lfw
from serve import longform as lf
from tadamoo import questions as qs
from tadamoo import suggestions as sg
from tadamoo.client import StreamCancelled
from tadamoo.pipeline import Pipeline
from tadamoo.roles import valid as valid_role
from tadamoo.router import QUEUE, STEER, STRONG_URGENT, URGENT, Router
from tadamoo.stack import PERMISSION_MODES, AgentConfig, BuiltAgent, add_agent_arguments, agent_settings, build_agent, data_dir_for, split_secret_specs
from tadamoo.tools.registry import clock_paused
from tadamoo.vault import valid_code

HEARTBEAT_S = 5.0
EFFORT_LEVELS = ("none", "low", "medium", "high")
DELTA_CHARS, DELTA_S = 400, 0.25
EVENT_CHARS = 20_000
BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0, 120.0, 180.0, 300.0)      # the last wait is repeated for as long as it takes
HISTORY_FILE, INFLIGHT_FILE, ARCHIVE_FILE = "history.json", "inflight.json", "archive.jsonl"
RESUME_NOTE = "[harness] You were set aside for an urgent request, which is now done. Go on with what you were doing."
INTERRUPTED_NOTE = ("[harness] The previous call may have run: the process stopped while it was running {tool} with {arguments}. "
                    "Check what really happened before repeating it.")
PENDING_NOTE = "The answer is pending: the user will see the question later. Go on with other work and do not repeat the request."
UNAVAILABLE_NOTE = "That information is not available in this run and nobody can give it now. Continue with what you have."
EXCEPTION_TOOLS = {"phone_call": "people"}                          # tools that reach another person


PLAN_NEWS_SECONDS = 600            # a plan made by someone else is asked about only while it is new


def _recent(stamp: Any) -> bool:
    """Whether a plan's `updated_at` (UTC, ISO) is within the last PLAN_NEWS_SECONDS."""
    try:
        import calendar
        return time.time() - calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")) <= PLAN_NEWS_SECONDS
    except ValueError:
        return False


class SessionStore:
    """The history of one conversation and the record of an action in flight, as files in one folder."""

    def __init__(self, directory: str | Path, redact: Optional[Callable[[str], str]] = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redact = redact
        self._lock = threading.Lock()                # tasks that run side by side save through one writer

    def _write(self, name: str, data: Dict[str, Any]) -> None:
        text = json.dumps(data, ensure_ascii=False)
        if self.redact:
            text = self.redact(text)                 # defence in depth: a secret never reaches the disk
        with self._lock:
            tmp = self.dir / (name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            for attempt in range(5):
                try:
                    os.replace(tmp, self.dir / name)
                    return
                except PermissionError:              # a reader or a scanner holds the file for a moment
                    if attempt == 4:
                        raise
                    time.sleep(0.02)

    def save(self, messages: List[Dict[str, Any]], meta: Dict[str, Any]) -> None:
        self._write(HISTORY_FILE, {**meta, "saved": time.time(), "messages": messages})

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads((self.dir / HISTORY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("messages"), list) else None

    def archive(self, messages: List[Dict[str, Any]]) -> None:
        """What left the model's view (pruned or summarized) stays in the person's record of the conversation."""
        text = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
        if self.redact:
            text = self.redact(text)
        with self._lock:
            with (self.dir / ARCHIVE_FILE).open("a", encoding="utf-8") as f:
                f.write(text)

    def archived(self) -> List[Dict[str, Any]]:
        try:
            return [m for m in (json.loads(ln) for ln in (self.dir / ARCHIVE_FILE).read_text(encoding="utf-8").splitlines() if ln.strip())
                    if isinstance(m, dict)]
        except (OSError, ValueError):
            return []

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
    """What the server says about itself: {"reachable", "loaded", "loading", "auto_load", "parallel"} (how many requests the model
    answers at once)."""
    req = urllib.request.Request(endpoint.rstrip("/") + "/v1/status", headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return {"reachable": False, "loaded": False, "loading": False, "auto_load": False, "parallel": 1}
    return {"reachable": True, "loaded": bool(data.get("loaded")), "loading": bool(data.get("loading")),
            "auto_load": bool(data.get("auto_load")), "parallel": int((data.get("concurrency") or {}).get("serving") or 1)}


@dataclass
class Task:
    """One request being worked on. The person's own conversation is the main agent's; an urgent or parallel request, which has
    nothing to do with it, gets a fork with a conversation of its own."""
    turn: str
    text: str
    agent: Any
    side: bool = False
    skill: Optional[Dict[str, Any]] = None        # a personal skill this task runs, instead of a model working on the request
    interrupt: bool = False                       # asked to stop its model call now, to be put aside
    state_running: bool = False                   # a model call or a tool is in progress
    intake: Optional[Dict[str, Any]] = None       # the facts of a request of the person that intake has not looked at yet
    registered: Optional[Tuple[str, List[str]]] = None   # the parent and the parts this request has in the task registry
    progress: Optional[Any] = None                # tadamoo/progress.py: the newest steps, for the line that says how far a task got
    since: int = 0                                # how many turns the agent had when this request began


class _Current:
    """The agent of the task the calling thread is working on, for the tools that look at the person's latest words."""

    def __init__(self, session: "Session") -> None:
        self._s = session

    @property
    def _agent(self) -> Any:
        task = getattr(self._s._tl, "task", None)
        return task.agent if task is not None else self._s.agent

    last_user_text = property(lambda self: self._agent.last_user_text)
    untrusted_since_user = property(lambda self: self._agent.untrusted_since_user)

    def request_end_turn(self) -> None:
        self._agent.request_end_turn()


def question_source(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The facts about whom a question is for. `named` is one of the exceptions that only a person can decide (another person, a booking,
    money or a resource that is not the PC's own); `needs_data` is a question for information only the person or a service can give."""
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    kind = str(payload.get("kind") or facts.get("kind") or "")
    involves = [str(x) for x in facts.get("involves") or []] if isinstance(facts.get("involves"), list) else []
    named = ""
    if payload.get("hook"):
        named = "hook"                                  # the person's own hook asked for their decision: it waits for them where nobody watches
    elif payload.get("instructions"):
        named = "instructions"                          # orders found in outside text are the person's to authorize, also where nobody watches
    elif kind == "payment":
        named = "resource"
    elif kind == "permission" and str(payload.get("tool")) in EXCEPTION_TOOLS:
        named = EXCEPTION_TOOLS[str(payload.get("tool"))]
    elif involves:
        named = ("people" if any(x.startswith("people") or "someone else" in x or "phone call" in x for x in involves)
                 else "resource" if any(x.startswith("resource") for x in involves) else "booking")
    return {"kind": kind, "named": named or None, "needs_data": kind in ("code", "mcp_elicitation", "capability_choice"),
            **({"involves": involves} if involves else {})}


def approving_value(payload: Dict[str, Any]) -> str:
    """The button that approves: "confirm", else an option that says yes, else the first."""
    values = [str(o.get("value")) for o in payload.get("options") or [] if isinstance(o, dict)]
    if "confirm" in values:
        return "confirm"
    yes = [v for v in values if v.strip().lower() in ("yes", "y", "ok", "approve", "proceed", "allow")]
    return yes[0] if yes else (values[0] if values else "")


def unattended_confirm(board: Callable[[str, Dict[str, Any]], bool]) -> Callable[[str, Dict[str, Any]], bool]:
    """With the approvals on the server's board and nobody watching: a call is approved at once unless it is a named exception,
    which goes to the board and waits there for the person."""
    def confirm(tool: str, arguments: Dict[str, Any]) -> bool:
        if question_source({"kind": "permission", "tool": tool})["named"]:
            return board(tool, arguments)
        return True
    return confirm


class Session:
    def __init__(self, built: BuiltAgent, store: SessionStore, emit: Callable[[Dict[str, Any]], None], session_id: str = "",
                 waits: tuple = BACKOFF_S, sleep: Callable[[float], None] = time.sleep,
                 health: Optional[Callable[[], Dict[str, Any]]] = None, router: Optional[Router] = None, unattended: bool = False,
                 classify_errors: bool = True) -> None:
        self.built, self.store, self.emit = built, store, emit
        self.unattended = unattended                            # nobody is there to answer: a question never blocks (see the module's text)
        self.classify_errors = classify_errors                  # a model error that waiting cannot cure ends the turn (tadamoo/error_classifier.py)
        self._parked: Dict[str, Dict[str, Any]] = {}            # questions left for the person, by id
        self._granted: List[str] = []                           # permission questions the person approved after the call was refused
        self.agent = built.agent
        self.session_id = session_id
        self.waits, self.sleep = tuple(waits), sleep
        self.health = health or (lambda: {"reachable": True, "loaded": True, "loading": False, "auto_load": True, "parallel": 1})
        self.router = router or Router()
        self.turn_id = ""
        self.busy = False
        self.blocked: Optional[Dict[str, Any]] = None
        self.turns_done = 0
        self.last_progress = time.monotonic()
        self._workers = 0                                       # threads working on requests
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._tl = threading.local()
        self.inbox: List[Dict[str, str]] = []                   # messages that came while busy, not yet looked at
        self.queue: List[Dict[str, str]] = []                   # requests that wait for their turn on the main conversation
        self.urgent: List[Dict[str, str]] = []                  # requests that cannot wait, to be done next
        self.skill_queue: List[Tuple[str, Dict[str, Any]]] = []  # skill runs asked while a task was under way, started when it is free
        self._poll_due = False                                  # a look at the plans was due while busy: done when the conversation is free
        self.stack: List[Task] = []                             # tasks put aside, to go on afterwards
        self.tasks: Dict[str, Task] = {}                        # the tasks that exist, by turn
        self._pending_note: Optional[str] = None
        self._asks: Dict[str, Dict[str, Any]] = {}              # confirmations a task is waiting for, by id
        self._ask_lock = threading.Lock()
        if getattr(built, "permissions", None) is not None:
            built.permissions.refusal_note = self._refusal_note
        self._waiting_n = 0                                     # a task waiting for the person is not stuck
        self.planner: Any = None
        self.reporter: Any = None                               # sends the file edits to the server's Changes panel, when asked to
        self._summary = ""                                      # the plans summary the conversation last got
        self.attachment_store: Any = None                       # AttachmentStore of this conversation's files, when it has one
        self.intake: Optional[it.Intake] = None                 # what runs between the person's text and the model's first step (tadamoo/intake.py)
        self.optimizer: Optional[opt.Optimizer] = None          # the person's message rewritten by the technical session before the model reads it (tadamoo/optimize.py)
        self.task_sink: Optional[it.TaskSink] = None            # the server's task registry, where a large request's parts are recorded
        self.suggester: Optional[sg.Suggester] = None           # the next-prompt chips (tadamoo/suggestions.py)
        self.ask_only = False                                   # a conversation without tools
        self._suggest_cancel = threading.Event()                # set when the person sends something: a suggestion call in flight stops
        self._card_at: Optional[float] = None                   # when the card now on the screen was shown
        self._long_asked: Optional[List[str]] = None            # the labels that refuse the parts, while the card that offered them is open
        self._steps_meta: Optional[Dict[str, Any]] = None       # how to read the answer to the card of next steps, while it is open (tadamoo/nextsteps.py)
        self.workspace = ""                                     # the folder this conversation works in
        self.checks_offer = False                               # the project's own checks are still to be offered once
        self._long_request: Optional[Dict[str, Any]] = None     # the request that needs a long text: its words, its text and the conversation before it
        self.endpoint, self.api_key = "", None
        self._window: Optional[Tuple[int, float]] = None
        self._announced: set = set()                            # what the planner already woke the conversation about
        self._delta_lock = threading.Lock()
        self._delta_kind, self._delta_turn, self._delta_text, self._delta_at = "", "", [], time.monotonic()
        self.current = _Current(self)
        self.main = Task("main", "", self.agent)
        self._adopt(self.main)
        if built.plan_gate is not None:
            built.plan_gate.ask = self.ask_plan                  # a plan in plan mode is put to the person in the conversation

    @property
    def waiting(self) -> bool:
        return self._waiting_n > 0

    @property
    def permission_denials(self) -> Dict[str, Any]:
        """How many calls the permission layer refused in this conversation, in all and by tool."""
        perms = self.built.permissions
        return {"total": perms.denied, "by_tool": dict(perms.denied_by_tool)}

    def _adopt(self, task: Task) -> None:
        a = task.agent
        a.on_event = lambda e, t=task: self._on_event(e, t)
        a.on_turn_end = lambda turn, t=task: self._on_turn_end(turn, t)
        a.on_delta = lambda k, x, t=task: self._on_delta(k, x, t)
        a.before_inference = lambda t=task: self._safe_point(t)
        a.batch_interrupt = lambda t=task: self._batch_stop(t)

    # -- the history -----------------------------------------------------------------------------------

    def _pending(self) -> List[Dict[str, str]]:
        return [*self.urgent, *self.queue, *self.inbox]

    def _save(self) -> None:
        self.store.save(self.agent.messages[1:], {"session": self.session_id, "turns": self.turns_done, "plans_summary": self._summary, "announced": sorted(self._announced),
                                                  "pending": self._pending(), "parked": list(self._parked.values())})

    def restore(self) -> int:
        """Takes the saved history back. An action that cannot be undone and was running when the process died is told to the model
        as a note (it may have run: check before repeating); the requests that were waiting go on with the next `resume`."""
        data = self.store.load()
        count = 0
        if data:
            self.agent.messages = [self.agent.messages[0], *data["messages"]]
            self.turns_done = int(data.get("turns") or 0)
            self._summary = str(data.get("plans_summary") or "")
            self._announced = {str(k) for k in data.get("announced", [])}
            self.queue = [m for m in data.get("pending", []) if isinstance(m, dict) and m.get("text")]
            self._parked = {str(i["id"]): i for i in data.get("parked", []) if isinstance(i, dict) and i.get("id")}
            count = len(data["messages"])
        lost = self.store.inflight()
        if lost:
            self._pending_note = INTERRUPTED_NOTE.format(tool=lost.get("tool"), arguments=json.dumps(lost.get("arguments"), ensure_ascii=False)[:300])
            self.store.clear_inflight()
        return count

    def _on_turn_end(self, turn: al.AgentTurn, task: Task) -> None:
        self.turns_done += 1
        self.last_progress = time.monotonic()
        self._save()

    def _on_delta(self, kind: str, text: str, task: Task) -> None:
        """The model's text as it is written, in pieces of a few hundred characters or a quarter of a second."""
        self.last_progress = time.monotonic()
        if task.agent.cancel.is_set() or task.interrupt:
            raise StreamCancelled()                      # the connection closes and the model stops writing
        with self._delta_lock:
            if self._delta_text and (self._delta_kind != kind or self._delta_turn != task.turn):
                self._flush_delta()
            self._delta_kind, self._delta_turn = kind, task.turn
            self._delta_text.append(text)
            if sum(map(len, self._delta_text)) >= DELTA_CHARS or time.monotonic() - self._delta_at >= DELTA_S:
                self._flush_delta()

    def _flush_delta(self) -> None:
        if self._delta_text:
            self.emit({"event": "delta", "turn": self._delta_turn, "kind": self._delta_kind, "text": "".join(self._delta_text)})
        self._delta_text, self._delta_at = [], time.monotonic()

    def _on_event(self, event: Dict[str, Any], task: Task) -> None:
        with self._delta_lock:
            self._flush_delta()                         # the pieces come before the whole message they belong to
        self.last_progress = time.monotonic()
        kind = event.get("type")
        if kind == "tool_start" and event.get("effect") == "irreversible":
            self.store.mark_inflight({"turn": task.turn, "tool": event.get("name"), "arguments": event.get("arguments")})
        elif kind == "tool_end":
            self.store.clear_inflight()
        shown = dict(event)
        for key in ("output", "content", "reasoning"):
            if isinstance(shown.get(key), str) and len(shown[key]) > EVENT_CHARS:
                shown[key] = shown[key][:EVENT_CHARS] + f"\n[... {len(event[key]) - EVENT_CHARS} more characters]"
        self.emit({"event": "agent", "turn": task.turn, "data": shown})
        if kind == "tool_end" and (self.unattended or task.registered) and side_uses.enabled("progress"):
            self._progress_line(task, event)
        if self.reporter is not None:
            self.reporter.on_event(event)

    def _progress_line(self, task: Task, event: Dict[str, Any]) -> None:
        """A task nobody watches: every few tool steps the technical session writes one line on how far it got, shown in the task list."""
        if task.progress is None:
            task.progress = prog.Progress()
        task.progress.add(event)
        if not task.progress.due() or getattr(task.progress, "busy", False):
            return
        task.progress.busy = True

        def write() -> None:
            try:
                line = task.progress.line(self.built.technical or task.agent.client, task.agent.last_user_text)
                if line:
                    self.emit({"event": "progress", "turn": task.turn, "text": line})
                    if task.registered is not None and self.task_sink is not None:
                        self.task_sink.record(task.registered[0], "running", line)
            finally:
                task.progress.busy = False
        threading.Thread(target=write, name="progress-line", daemon=True).start()

    # -- questions to the person -------------------------------------------------------------------------

    def _cur_turn(self) -> str:
        task = getattr(self._tl, "task", None)
        return task.turn if task is not None else self.turn_id

    def _cur_cancel(self) -> threading.Event:
        task = getattr(self._tl, "task", None)
        return (task.agent if task is not None else self.agent).cancel

    def _policy(self, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """How a question is handled: "wait" (the person is there), "approve", "unavailable" or "park", with the facts about its source."""
        source = question_source(payload)
        if not self.unattended:
            return "wait", source
        if source["named"]:
            return "park", source
        return ("unavailable" if source["needs_data"] else "approve"), source

    def _auto_answer(self, payload: Dict[str, Any], source: Dict[str, Any], value: str, available: bool) -> str:
        self.emit({"event": "auto_answered", "id": uuid.uuid4().hex[:8], "turn": self._cur_turn(), "kind": source["kind"], "value": value,
                   "available": available, "source": source, "question": str(payload.get("question") or "")[:300]})
        return value

    def _park(self, payload: Dict[str, Any], source: Dict[str, Any]) -> str:
        """A question only the person can answer, in a run nobody watches: it is kept in a visible list and the model goes on with other work."""
        aid = uuid.uuid4().hex[:8]
        item = {"id": aid, "turn": self._cur_turn(), "at": time.time(), "source": source, "payload": payload}
        with self._ask_lock:
            self._parked[aid] = item
        self._tl.parked = item
        self._save()
        self.emit({"event": "pending", "id": aid, "turn": item["turn"], "source": source, **payload})
        return ""

    def _refusal_note(self) -> str:
        item = getattr(self._tl, "parked", None)
        self._tl.parked = None
        return f"{PENDING_NOTE} (question {item['id']})" if item else ""

    def _ask(self, payload: Dict[str, Any]) -> str:
        """A confirmation: shown with its buttons, and the task waits for the answer ("confirm", "cancel" or "" if it was
        cancelled), however long it takes. A task waiting for the person is not stuck. Where nobody can answer (see `_policy`) the
        question is answered at once or parked, and "" comes back."""
        how, source = self._policy(payload)
        if how == "approve":
            return self._auto_answer(payload, source, approving_value(payload), True)
        if how == "unavailable":
            return self._auto_answer(payload, source, "", False)
        if how == "park":
            return self._park(payload, source)
        aid = uuid.uuid4().hex[:8]
        said = al.current_explanation() if payload.get("mode") == "confirm" else ""
        if said and "explain" not in payload:
            payload = {**payload, "explain": said}                    # the model's own sentence for the call that asks, shown as such
        slot = {"event": threading.Event(), "value": "", "values": {o["value"] for o in payload.get("options", [])}, "free": bool(payload.get("free")),
                "free_prefix": str(payload.get("free_prefix") or "")}
        cancel = self._cur_cancel()
        with self._ask_lock:
            self._asks[aid] = slot
        self.emit({"event": "ask", "id": aid, "turn": self._cur_turn(), **payload})
        self._waiting_n += 1
        try:
            with clock_paused():                     # the person's time is not the tool's
                while not slot["event"].wait(0.25):
                    self.last_progress = time.monotonic()
                    if cancel.is_set():
                        return ""
            return slot["value"]
        finally:
            self._waiting_n -= 1
            with self._ask_lock:
                self._asks.pop(aid, None)

    def answer(self, aid: Any, value: Any) -> bool:
        with self._ask_lock:
            slot = self._asks.get(str(aid))
            parked = self._parked.get(str(aid))
        if parked is not None:
            return self._answer_parked(parked, value)
        sentence = bool(slot and slot["free_prefix"] and isinstance(value, str) and value.startswith(slot["free_prefix"]) and len(value) <= len(slot["free_prefix"]) + 500)
        if slot is None or not (value in slot["values"] or (slot["free"] and valid_code(value)) or sentence):
            self.emit({"event": "error", "code": "bad_answer", "message": "no such question, or not one of its answers"})
            return False
        slot["value"] = value
        slot["event"].set()
        return True

    def _answer_parked(self, item: Dict[str, Any], value: Any) -> bool:
        """The person's answer to a parked question, applied now: a permission they approve is granted for the call that was refused,
        and the model is told the answer in a note it reads at its next turn (a turn is started for it when nothing is under way)."""
        payload = item["payload"]
        options = {str(o.get("value")): o for o in payload.get("options") or [] if isinstance(o, dict)}
        prefix = str(payload.get("free_prefix") or "")
        free = bool(payload.get("free")) and isinstance(value, str) and valid_code(value)
        sentence = bool(prefix) and isinstance(value, str) and value.startswith(prefix) and len(value) <= len(prefix) + 500
        if not (value in options or free or sentence):
            self.emit({"event": "error", "code": "bad_answer", "message": "no such question, or not one of its answers"})
            return False
        with self._ask_lock:
            self._parked.pop(item["id"], None)
        shown = "" if free else str(value)
        if payload.get("kind") == "parked_question":
            shown = str(options[value].get("label")) if value in options else str(value)[len(prefix):]
        if payload.get("kind") == "permission" and value in ("confirm", "chat", "always"):
            self._granted.append(self._call_key(str(payload.get("tool")), payload.get("arguments") or {}))
            self._remember_rules(str(value), options.get(str(value), {}).get("rules") or [])
        what = str(payload.get("question") or "")[:200]
        note = f"[harness] The user answered a question that was pending ({what}): {shown or 'answered'}."
        if payload.get("kind") == "permission":
            note += " If it is allowed now, run the call again." if value in ("confirm", "chat", "always") else " Do not run it."
        self._pending_note = f"{self._pending_note}\n{note}" if self._pending_note else note
        self._save()
        self.emit({"event": "pending_done", "id": item["id"], "value": shown})
        if self.idle():
            self.resume()
        return True

    @staticmethod
    def _call_key(tool: str, arguments: Dict[str, Any]) -> str:
        return tool + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)

    def parked_questions(self) -> List[Dict[str, Any]]:
        with self._ask_lock:
            return list(self._parked.values())

    def _question(self, payload: Dict[str, Any]) -> Optional[str]:
        """The `ask_user` card. Where the person is, it is shown and the turn ends (None comes back): their answer is the next message. Where
        nobody watches (`unattended`) it never waits: every question is answered at once from what the model proposed (recommended option,
        default text, recommended side), one that involves another person, a booking or a resource is parked for the person, and the text
        the model reads comes back with a tag on each choice the harness made."""
        if not self.unattended:
            self.emit({"event": "ask", "id": uuid.uuid4().hex[:8], "turn": self._cur_turn(), **payload})
            self._card_at = time.time()
            self._audit("question_shown", {"source": "model", "questions": [{"id": q["id"], "kind": q["kind"], "options": len(q["options"]),
                                                                              "recommended": [o["id"] for o in q["options"] if o.get("recommended")]} for q in payload["questions"]]})
            return None
        checked, tags = qs.auto_answers(payload)
        self._audit("question_auto", {"source": "model", "chosen": tags})
        for q in payload["questions"]:
            named = question_source({"kind": "question", "facts": {"involves": q.get("involves", [])}})
            if tags[q["id"]] == "parked":
                self._park(qs.parked_payload(q), named)
                self._tl.parked = None
            answer = checked["answers"][q["id"]]
            self.emit({"event": "auto_answered", "id": uuid.uuid4().hex[:8], "turn": self._cur_turn(), "kind": "question", "value": answer["text"] or ",".join(answer["picked"]), "answer": qs.answer_label(q, answer),
                       "available": tags[q["id"]] in ("recommended", "default"), "chosen": tags[q["id"]], "source": named, "question": q["text"][:300]})
        return qs.format_answer(payload, checked, tags)

    def ask_code(self, who: str) -> str:
        """A second-factor code typed by the person. It goes to the tool that needs it and nowhere else: not to the model, not to the history."""
        return self._ask({"mode": "confirm", "kind": "code", "free": True, "facts": {"who": who},
                          "question": f"Type the code for {who}.", "options": []})

    def payment_notice(self, result: Dict[str, Any]) -> None:
        """Every authorized payment is said in the conversation, what the waiver let through as much as what the person confirmed."""
        self.emit({"event": "payment", "turn": self._cur_turn(), "amount": result.get("amount"), "recipient": result.get("recipient"),
                   "system": result.get("system"), "site": result.get("site"), "mode": result.get("mode"), "remaining": result.get("remaining")})

    def ask_payment(self, request: Dict[str, Any]) -> bool:
        """A payment the broker will not decide alone: no waiver covers it, or it is over the caps. The person decides."""
        question = f"Pay {request['amount']} to {request['recipient']} ({request['reason']}) with {request['system']}?"
        rule = f"request_payment:{request['system']}"                  # the person's allow for this payment system: this chat or every chat
        if self.built.permissions.allowed_by(rule, {}):
            return True
        options = [{"label": "Allow", "value": "confirm"},
                   {"label": f"Allow in this conversation ({rule})", "value": "chat", "rule": rule, "rules": [rule]},
                   {"label": f"Always allow ({rule})", "value": "always", "rule": rule, "rules": [rule]},
                   {"label": "Deny", "value": "cancel"}]
        answer = self._ask({"mode": "confirm", "kind": "payment", "facts": request, "question": question, "options": options})
        self._remember_rules(answer, [rule])
        return answer in ("confirm", "chat", "always")

    def ask_plan(self, plan: str, modes: List[str], outside: bool) -> str:
        """The plan the agent presents in plan mode. Yes goes on in the mode the person chose for it (the first of `modes`); no keeps plan mode."""
        note = "\n\nThe agent read content from outside this conversation before it wrote this plan." if outside else ""
        answer = self._ask({"mode": "confirm", "kind": "plan_approval", "facts": {"plan": plan, "mode": modes[0], "outside_content": outside},
                            "question": f"Approve this plan and go on in '{modes[0]}' mode?\n\n{plan}{note}",
                            "options": [{"label": "Approve", "value": "confirm"}, {"label": "Not yet", "value": "cancel"}]})
        return modes[0] if answer == "confirm" else ""

    def permission_confirm(self, tool: str, arguments: Dict[str, Any]) -> bool:
        """The permission manager's question, put to the person in the conversation. The answer reaches as far as they choose: this call, this
        conversation, or every conversation from now on; the last two are an allow rule, which also answers the Rule of Two and a tool that asks every time."""
        from tadamoo.permissions import suggest_rules
        two = arguments.get("rule_of_two")                            # set when the Rule of Two is what asks: the question says which facts made it ask
        hooked = arguments.get("hook_asks")                           # set when a hook of the person's asks before the call
        heard = arguments.get("instructions_from")                    # set when text from outside gave orders to the AI: the person authorizes them or not
        arguments = {k: v for k, v in arguments.items() if k not in ("rule_of_two", "instructions_from", "hook_asks")}
        shown = json.dumps(arguments, ensure_ascii=False)[:300]
        if hooked:
            answer = self._ask({"mode": "confirm", "kind": "permission", "tool": tool, "arguments": arguments, "hook": hooked,
                                "question": f"A hook of yours asks before this call ({hooked}). Allow {tool} {shown}?",
                                "options": [{"label": "Allow this call", "value": "confirm"}, {"label": "Deny", "value": "cancel"}]})
            return answer == "confirm"
        if heard:
            if self._call_key(tool, arguments) in self._granted:           # authorized after it was left pending
                self._granted.remove(self._call_key(tool, arguments))
                return True
            answer = self._ask({"mode": "confirm", "kind": "permission", "tool": tool, "arguments": arguments, "instructions": heard,
                                "question": f"Text read from {heard} gave instructions to the AI. Authorize acting on them? The call that asks: {tool} {shown}",
                                "options": [{"label": "Authorize these instructions", "value": "confirm"}, {"label": "Deny", "value": "cancel"}]})
            return answer == "confirm"
        rules = suggest_rules(tool, arguments)
        label = ", ".join(rules)
        options = [{"label": "Allow this call", "value": "confirm"}]
        if rules:
            options += [{"label": f"Allow in this conversation ({label})", "value": "chat", "rule": label, "rules": rules},
                        {"label": f"Always allow ({label})", "value": "always", "rule": label, "rules": rules}]
        options.append({"label": "Deny", "value": "cancel"})
        key = self._call_key(tool, arguments)
        if key in self._granted:                                      # the person approved this very call after it was left pending
            self._granted.remove(key)
            return True
        answer = self._ask({"mode": "confirm", "kind": "permission", "tool": tool, "arguments": arguments, **({"rule_of_two": True} if two else {}),
                            "question": f"{two}" + "\n\n" + f"Allow {tool} {shown}?" if two else f"Allow {tool} {shown}?", "options": options})
        self._remember_rules(answer, rules)
        return answer in ("confirm", "chat", "always")

    def _remember_rules(self, answer: str, rules: List[str]) -> None:
        if answer not in ("chat", "always"):
            return
        scope = "global" if answer == "always" else "chat"
        for rule in rules:
            self.built.permissions.add_rule(rule, "allow")                   # the server keeps it for this conversation's next starts; "always" for every other one too
            self.emit({"event": "rule_added", "rule": rule, "scope": scope})

    def enable_orchestration(self) -> None:
        """Gives the conversation the tools to message and start other conversations (tadamoo/orchestration_tools.py)."""
        if getattr(self.agent.tools, "_orchestration_on", False):
            return
        from tadamoo.integrations.orchestration_client import OrchestrationClient
        from tadamoo.orchestration_tools import register_orchestration_tools
        register_orchestration_tools(self.agent.tools, OrchestrationClient(self.endpoint, self.api_key))
        self.agent.tools._orchestration_on = True

    def enable_planner(self, client: Any) -> None:
        """Gives the conversation the planner's tools and the question tool; every change to a plan asks."""
        from tadamoo.planner_tools import Confirmer, register_ask_tool, register_planner_tools
        self.planner = client
        hooks = getattr(self.agent, "hooks", None)
        if hooks is not None:                                    # a hook of the person's that tells the planner reaches it from here
            hooks.plan_event_sink = lambda text, source: client.ingest(text, source)
        register_planner_tools(self.agent.tools, client, Confirmer(self.current, ask=self._ask, auto=lambda: self.built.permissions.level.value == "auto"))
        register_ask_tool(self.agent.tools, self.current, self._question)
        self.agent.system_prompt += "\n\n" + qs.PROMPT_RULES
        self.agent.messages[0] = self.agent._system_message()

    def _plans_note(self) -> Optional[str]:
        """What the conversation should be told about the plans: only when they changed since it last was, and before the
        person's message, so what follows a message from outside is never a claim about plans."""
        if self.planner is None:
            return None
        from tadamoo.planner_tools import plans_summary
        summary = plans_summary(self.planner)
        if summary == self._summary:
            return None
        self._summary = summary
        return "[harness] The user's plans now:\n" + summary if summary else "[harness] The user has no active plans now."

    # -- commands --------------------------------------------------------------------------------------

    def _begin_main(self, turn: str, text: str, origin: str = "user", attachments: Optional[List[str]] = None, optimize: bool = True) -> Task:
        """Starts a turn of the person's own conversation: what the agent should know first, then their message."""
        agent = self.agent
        agent.cancel.clear()
        if self._pending_note:
            agent.append({"role": "user", "content": self._pending_note})
            self._pending_note = None
        note = self._plans_note()
        if note:
            agent.append({"role": "user", "content": note})
        self._add_message(agent, text, origin, attachments)
        retrieved = self.built.retrieved_message(text) if origin == "user" else None
        if retrieved:
            agent.taint("memories", private=bool(retrieved.get("private")))
            agent.append(retrieved)
        self._save()                                   # the message is kept before the model sees it
        task = Task(turn, text, agent, intake={"origin": origin, "attachments": list(attachments or []), "optimize": optimize} if origin == "user" else None,
                    since=len(agent.turns))
        self._adopt(task)
        self.tasks[turn] = task
        return task

    def _begin_side(self, turn: str, text: str, attachments: Optional[List[str]] = None) -> Task:
        """A request with nothing to do with the main conversation: a fork with a conversation of its own."""
        agent = self.agent.fork()
        self._add_message(agent, text, "user", attachments)
        task = Task(turn, text, agent, side=True)
        self._adopt(task)
        self.tasks[turn] = task
        return task

    def _launch(self, task: Task) -> None:
        with self._lock:
            self._workers += 1
            self.busy = True
        threading.Thread(target=self._work, args=(task,), name=f"session-task-{task.turn}", daemon=True).start()

    def send(self, turn_id: str, text: str, origin: str = "user", attachments: Optional[List[str]] = None, optimize: bool = True) -> bool:
        if origin == "user":
            self._suggest_cancel.set()                 # a suggestion call in flight stops: the person's message is next
            if self.suggester is not None:
                self.suggester.sent(text)
        with self._lock:
            busy = self._workers > 0
            if busy and origin != "user":
                return False                           # a note from the planner never interrupts a task: it waits for the next quiet moment
            if not busy:
                self.blocked = None
                self._workers += 1
                self.busy, self.turn_id = True, turn_id
        if busy:                                       # a task is under way: the message is looked at before its next model call
            msg: Dict[str, Any] = {"turn": turn_id, "text": text, **({"attachments": attachments} if attachments else {}), **({} if optimize else {"optimize": False})}
            self.emit({"event": "queued", "turn": turn_id, "text": text})
            if self.parallel_slots() > self._workers:  # the model can take another request now: no need to wait for the task's safe point
                threading.Thread(target=self._route_now, args=(msg,), name="session-route", daemon=True).start()
                return True
            with self._lock:
                self.inbox.append(msg)
            if STRONG_URGENT.search(text):             # no need to wait for the end of a long answer to put a task aside
                self._interrupt_for_urgent()
            self._save()
            return True
        task = self._begin_main(turn_id, text, origin, attachments, optimize)
        self.emit({"event": "accepted", "turn": turn_id} if origin == "user" else {"event": "woke", "turn": turn_id, "text": text[:300]})
        threading.Thread(target=self._work, args=(task,), name="session-task", daemon=True).start()
        return True

    def run_skill(self, turn_id: str, skill: Dict[str, Any]) -> bool:
        """Runs a personal skill in this conversation, as a task of its own: the person's request is said in it, the skill's tool calls
        go through the agent's gate and are kept in it, and its result is its answer. One thing at a time: while a task is under way
        the run is queued and starts when the conversation is free."""
        with self._lock:
            if self._workers > 0 or self.waiting:
                self.skill_queue.append((turn_id, skill))
                self.emit({"event": "queued", "turn": turn_id, "kind": "skill", "position": len(self.skill_queue)})
                return True
            self._workers += 1
            self.busy, self.turn_id, self.blocked = True, turn_id, None
        task = self._begin_skill(turn_id, skill)
        threading.Thread(target=self._work, args=(task,), name="session-skill", daemon=True).start()
        return True

    def _begin_skill(self, turn_id: str, skill: Dict[str, Any]) -> Task:
        agent = self.agent
        agent.cancel.clear()
        display = str(skill.get("display") or f"Run the skill {skill.get('name')}")
        agent.add_user_message(display)
        task = Task(turn_id, display, agent, skill=skill)
        self._adopt(task)
        self.tasks[turn_id] = task
        self._save()
        self.emit({"event": "accepted", "turn": turn_id})
        return task

    def _run_skill_task(self, task: Task) -> str:
        """The body of a task that runs a personal skill: the script runs, and what it did is told as the answer."""
        from tadamoo.agent_loop import AgentTurn
        from tadamoo.personal_skills.hosting import SkillRun, result_text
        agent, skill = task.agent, task.skill or {}
        result = SkillRun(self, task, skill).execute()
        text = result_text(str(skill.get("name") or "skill"), result)
        agent.stop_reason = al.CANCELLED if result.error == "stopped" else al.FINAL
        agent.error = ""
        agent.append({"role": "assistant", "content": text})
        agent._emit({"type": "assistant", "reasoning": "", "content": text})
        agent.turns.append(AgentTurn(len(agent.turns) + 1, task.text, "", text))
        return "done"

    def _add_message(self, agent: Any, text: str, origin: str, attachments: Optional[List[str]], steer: bool = False) -> None:
        """Puts the person's message in the conversation: with files attached, they come first as data in blocks of their own and the
        person's words after; what the person can confirm with is only their words. The phrases they marked with # (a skill's variable parts)
        are given to the model as plain text; what they wrote is kept beside it, for a skill made from it."""
        raw = text
        if origin == "user" and self.intake is not None and self.intake.mode != "off":
            text = it.clean(text)                                    # the model reads the cleaned text; the person's own stays as `raw`
        if origin == "user" and "#" in text:
            from tadamoo.personal_skills import markers
            text = markers.clean(text)
        self._add_user(agent, text, origin, attachments, steer)
        if raw != text and agent.messages and agent.messages[-1].get("role") == "user":
            agent.messages[-1]["raw"] = raw

    def _add_user(self, agent: Any, text: str, origin: str, attachments: Optional[List[str]], steer: bool) -> None:
        if origin == "user" and attachments and self.attachment_store is not None:
            from tadamoo.attachments import compose_message
            full, inlined = compose_message(self.attachment_store, self.session_id, attachments, text, self.window_tokens())
            agent.add_user_message(full, steer=steer, shown=text)
            agent.taint("attachment", private=True)         # a file can say anything, and it is the person's own: nothing confirms a change to a plan after it
            return
        agent.add_user_message(text, origin, steer=steer)

    def window_tokens(self) -> int:
        """The loaded model's context window, as the server says it (a default when it does not)."""
        now = time.monotonic()
        if self._window and now - self._window[1] < 120:
            return self._window[0]
        n = 32768
        try:
            req = urllib.request.Request(self.endpoint.rstrip("/") + "/v1/models", headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for m in data.get("data", []):
                if (m.get("status") or {}).get("value") == "loaded" and int(m.get("context_length") or 0) > 0:
                    n = int(m["context_length"])
        except (urllib.error.URLError, OSError, ValueError):
            pass
        self._window = (n, now)
        return n

    def set_effort(self, level: str) -> None:
        """How long the model thinks, from the next model call on; empty is the model's own default. Side tasks share the setting."""
        if level:
            self.agent.sampling["reasoning_effort"] = level
        else:
            self.agent.sampling.pop("reasoning_effort", None)

    def set_max_turns(self, value: int) -> None:
        """The most model steps one answer may take, from the next step on; below 1 is no limit."""
        self.agent.max_turns = value

    def set_end_turn_bias(self, value: float) -> None:
        """How much less ready the model is to end its turn, from the next model call on; 0 is off. Side tasks that start later share it."""
        if value:
            self.agent.sampling["end_turn_bias"] = value
        else:
            self.agent.sampling.pop("end_turn_bias", None)

    def set_temperature(self, value: Optional[float]) -> None:
        """The sampling temperature of every model call from the next on; None leaves it to the model file's own sampling."""
        if value is None:
            self.agent.sampling.pop("temperature", None)
        else:
            self.agent.sampling["temperature"] = value

    def set_max_tokens(self, value: Optional[int]) -> None:
        """The most one reply may write, from the next request on; None leaves it to the server."""
        self.agent.max_tokens = value

    def run_command(self, cid: str, name: str, args: str) -> None:
        """A slash command that asks this process something; the answer is an event, so the person's page shows it where it was asked."""
        from tadamoo.commands import run
        self.emit({"event": "command", "id": cid, "name": name, **run(name, args, self)})

    def idle(self) -> bool:
        """Nothing under way, nothing waiting, nobody asked: a quiet moment."""
        with self._lock:
            return (self._workers == 0 and not self._waiting_n and not self.blocked and not (self.queue or self.inbox or self.urgent or self.stack or self.skill_queue))

    def poll_plans(self) -> Optional[str]:
        """Looks at the plans in a quiet moment and, for what is new and needs the person (a change proposed by the planner, a
        step it cannot do, a step that failed), starts a turn that tells them (an unattended run is told to decide for itself instead).
        Each thing is announced once. A look asked for while the conversation is busy is kept and made when it is free."""
        if self.planner is None:
            return None
        if not self.idle():
            self._poll_due = True
            return None
        self._poll_due = False
        try:
            plans = self.planner.list()
        except Exception:
            return None
        for plan in sorted(plans, key=lambda x: str(x.get("updated_at") or ""), reverse=True):
            pid = str(plan.get("plan_id"))
            try:
                snap = self.planner.snapshot(pid)
            except Exception:
                continue
            title = str((snap.get("plan") or {}).get("title") or pid)
            news: List[Tuple[str, str]] = []
            for pr in snap.get("proposals", []):
                if pr.get("source") != "chat":
                    news.append((f"p:{pid}:{pr.get('proposal_id')}", f'the planner proposes a change: "{pr.get("title")}" ({str(pr.get("rationale") or "")[:200]}).'))
            for of in snap.get("offers", []):
                news.append((f"o:{pid}:{of.get('offer_id')}", f'a step needs the person: {str(of.get("reason") or "")[:200]}.'))
            for node in ((snap.get("plan") or {}).get("nodes") or {}).values() if isinstance((snap.get("plan") or {}).get("nodes"), dict) else (snap.get("plan") or {}).get("nodes") or []:
                if isinstance(node, dict) and node.get("status") == "failed":
                    news.append((f"f:{pid}:{node.get('id')}", f'the step "{node.get("title")}" failed.'))
                stuck = (node.get("state_variables") or {}).get("no_route") if isinstance(node, dict) else None
                if stuck:
                    news.append((f"n:{pid}:{node.get('id')}:{stuck.get('at')}", f'the decision "{node.get("title")}" has no exit that holds and waits for the person (values: {json.dumps(stuck.get("values", {}), ensure_ascii=False)[:200]}).'))
            gaps = snap.get("card_missing") or []
            if gaps and not any(h.get("actor") == "chat" for h in snap.get("history") or []) and _recent(plan.get("updated_at")):
                news.append((f"c:{pid}:{','.join(gaps)}", "to judge incoming events the planner still lacks " + ", ".join(gaps) + ": ask the person only these, in one short question, "
                                                         "and write what they say with plan_set_card."))
            fresh = [(k, t) for k, t in news if k not in self._announced]
            if fresh:
                key, text = fresh[0]
                self._announced.add(key)
                self._save()
                if self.unattended:
                    note = (f'[planner] Plan {pid} "{title}": {text} Nobody is there to answer: decide with what you know and go on (retry the step, '
                            f'work around it or leave it, and go on with the rest of the plan). This note is from the system, not from the person.')
                else:
                    note = (f'[planner] Plan {pid} "{title}": {text} Tell the person briefly and ask what to do; change nothing in the plan '
                            f'without their say. This note is from the system, not from the person.')
                if self.send("w-" + uuid.uuid4().hex[:6], note, origin="planner"):
                    return key
                self._announced.discard(key)
        return None

    def _route_now(self, msg: Dict[str, str]) -> None:
        """When the model can answer more requests at once, a request that is not about the task in hand starts beside it at once;
        one that is about the task waits for the task's next safe point, where it can be put into it."""
        with self._lock:
            running = next((t for t in self.tasks.values() if not t.side and t.state_running), None) or self.main
        how, why = self.router.route(msg["text"], self._context_of(running))
        if how == STEER:
            with self._lock:
                self.inbox.append(msg)
            self._save()
        else:
            self._route(running, msg, how, why)

    def _interrupt_for_urgent(self) -> None:
        """A strong urgency word: stop the model call that is being written (nothing it wrote is lost that matters: it was not
        yet an action), so the message is looked at now. A tool that is running is never interrupted."""
        with self._lock:
            if self.parallel_slots() > 1:
                return
            for t in list(self.tasks.values()):
                if not t.side and t.state_running:
                    t.interrupt = True

    def parallel_slots(self) -> int:
        try:
            return max(1, int(self.health().get("parallel") or 1))
        except Exception:
            return 1

    def resume(self) -> bool:
        """Goes on after a block, from where the conversation stands: the turn that was under way, then what was waiting."""
        with self._lock:
            if self._workers > 0:
                self.emit({"event": "error", "code": "busy", "message": "a task is still running"})
                return False
            self._workers += 1
            self.busy, self.blocked = True, None
        self.agent.cancel.clear()
        if self._pending_note:
            self.agent.append({"role": "user", "content": self._pending_note})
            self._pending_note = None
            self._save()
        if self._main_unfinished():
            task = Task(self.turn_id or "resumed", "", self.agent)
            self._adopt(task)
            self.tasks[task.turn] = task
        else:
            task = self._next_after(None)
            if task is None:
                with self._lock:
                    self._workers -= 1
                    self.busy = self._workers > 0
                return True
        threading.Thread(target=self._work, args=(task,), name="session-task", daemon=True).start()
        return True

    def _main_unfinished(self) -> bool:
        """Whether the main conversation stops in the middle of a turn: after the person's message or a tool's result."""
        last = self.agent.messages[-1] if len(self.agent.messages) > 1 else None
        return bool(last) and (last["role"] in ("user", "tool") or bool(last.get("tool_calls")))

    def cancel(self) -> None:
        """Stops everything: the task in hand, what was put aside and what was waiting. (Stop means stop.)"""
        with self._lock:
            cleared = len(self.inbox) + len(self.queue) + len(self.urgent) + len(self.stack)
            self.inbox, self.queue, self.urgent, self.stack = [], [], [], []
            for t in list(self.tasks.values()):
                t.agent.cancel.set()
            self.agent.cancel.set()
        if cleared:
            self.emit({"event": "cleared", "count": cleared})

    def promote(self, turn: str) -> bool:
        """A request that waits in the queue becomes urgent: the task in hand is put aside at its next safe point."""
        with self._lock:
            msg = next((m for m in self.queue if m["turn"] == turn), None)
            if msg is None:
                return False
            self.queue.remove(msg)
            self.urgent.append(msg)
        self.emit({"event": "routed", "turn": turn, "how": URGENT, "why": "person"})
        self._interrupt_for_urgent()
        return True

    def drop(self, turn: str) -> bool:
        with self._lock:
            before = len(self.queue) + len(self.urgent)
            self.queue = [m for m in self.queue if m["turn"] != turn]
            self.urgent = [m for m in self.urgent if m["turn"] != turn]
            gone = before != len(self.queue) + len(self.urgent)
        if gone:
            self.emit({"event": "dropped", "turn": turn})
            self._save()
        return gone

    def idle_seconds(self) -> float:
        """How long the running task has gone without news; a task waiting for the person is not stuck, so 0."""
        if not self.busy or self.waiting:
            return 0.0
        return round(time.monotonic() - self.last_progress, 1)

    # -- the looking at what the person wrote meanwhile ---------------------------------------------------

    def _context_of(self, task: Task) -> Dict[str, Any]:
        msgs = task.agent.messages
        last_assistant = next((m for m in reversed(msgs) if m["role"] == "assistant"), None)
        progress = ""
        if last_assistant:
            progress = (last_assistant.get("content") or "") or ", ".join(c["function"]["name"] for c in last_assistant.get("tool_calls", []))
        return {"task": task.text or task.agent.last_user_text, "progress": progress}

    def _batch_stop(self, task: Task) -> str:
        """Between two calls of one message: a reason to leave the rest of the batch unrun when the person has written something that changes
        or cancels the work (it is read at the next safe point), else ''. A message that would wait or is a different request does not stop it."""
        with self._lock:
            msg = self.inbox[0] if self.inbox else None
        if msg is None:
            return ""
        if "peek" not in msg:
            msg["peek"], msg["peek_why"] = self.router.route(msg["text"], self._context_of(task))            # asked once; the same answer is used when the message is routed
        if msg["peek"] != STEER:
            return ""
        return "Not run: the person wrote a message that changes or cancels this work, and it is read next. Do not repeat this call unless the new message asks for it."

    def _safe_point(self, task: Task) -> Optional[str]:
        """Between two model calls nothing is half done: the messages that came meanwhile are routed here, and the task is put
        aside if something urgent is waiting."""
        verdict = None
        while True:
            with self._lock:
                msg = self.inbox.pop(0) if self.inbox else None
            if msg is None:
                break
            how, why = (msg["peek"], msg.get("peek_why", "judge")) if msg.get("peek") else self.router.route(msg["text"], self._context_of(task))
            msg.pop("peek", None)
            msg.pop("peek_why", None)
            self._route(task, msg, how, why)
        with self._lock:
            if self.urgent and not task.side:
                verdict = al.SUSPENDED
        return verdict

    def _route(self, task: Task, msg: Dict[str, str], how: str, why: str) -> None:
        turn, text = msg["turn"], msg["text"]
        if how == STEER:
            self._add_message(task.agent, text, "user", msg.get("attachments"), steer=True)
            if not task.side:
                self._save()
            self.emit({"event": "routed", "turn": turn, "how": STEER, "why": why})
            return
        if self.parallel_slots() > self._workers:        # the model answers several requests at once: no need to wait or to stop
            side = self._begin_side(turn, text, msg.get("attachments"))
            self.emit({"event": "routed", "turn": turn, "how": "parallel", "why": why})
            self._launch(side)
            return
        with self._lock:
            (self.urgent if how == URGENT else self.queue).append(msg)
            position = len(self.queue)
        self.emit({"event": "routed", "turn": turn, "how": how, "why": why, "position": position if how == QUEUE else 0})
        self._save()

    # -- the tasks, one after the other --------------------------------------------------------------------

    def _wait(self, seconds: float, cancel: threading.Event) -> bool:
        """Waits in short slices; False when the person cancelled meanwhile."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cancel.is_set():
                return False
            self.sleep(min(0.25, max(0.0, end - time.monotonic())))
        return not cancel.is_set()

    def load_model(self, wait_s: float) -> str:
        """Asks the Loader to load the model and waits up to `wait_s` for it; returns what was done, raises when the Loader refuses."""
        from tadamoo.llm.qwen_loader import QwenLoader
        return QwenLoader(self.endpoint, self.api_key, wait_s=wait_s, sleep=self.sleep).ensure()

    def _model_wait(self, health: Dict[str, Any], wait: float) -> Tuple[str, str]:
        """What a retry waits for, as a sentence for the person, and the Loader's refusal if there was one. A server that is up with no
        model loaded and does not load by itself is asked to load the default one."""
        if not health["reachable"]:
            return "the server to answer", ""
        if health["loading"]:
            return "the model to finish loading", ""
        if health["loaded"]:
            return "the model to answer", ""
        if health["auto_load"] or not self.endpoint:
            return "the model to load on this request", ""
        try:
            self.load_model(max(1.0, min(wait, 30.0)))
            return "the model to finish loading (load started by this conversation)", ""
        except Exception as exc:                                       # the Loader's own words: another model holds the memory, no model chosen
            return "the Loader to accept the load of the model", str(exc)

    # -- intake: what is looked at before the model's first step -----------------------------------------------

    def _audit(self, action: str, details: Dict[str, Any]) -> None:
        audit = getattr(self.agent, "audit", None)
        if audit is not None:
            try:
                audit.record("harness", action, details)
            except Exception:
                pass                                         # the record is a courtesy: it never changes what is asked

    def _intake_context(self, task: Task, facts: Dict[str, Any]) -> it.Context:
        agent = task.agent
        last = agent.turns[-1] if agent.turns else None
        return it.Context(origin=str(facts.get("origin") or "user"), attachments=[str(a) for a in facts.get("attachments") or []],
                          attended=not self.unattended, ask_only=self.ask_only, auto=self.built.permissions.level.value == "auto",
                          started=any(m.get("tool_calls") for m in agent.messages), answers_card=agent.last_user_text.startswith(qs.ANSWER_HEADER),
                          history_tokens=last.prompt_tokens if last else 0, warm=bool(last and last.prompt_tokens and last.cached_tokens * 2 >= last.prompt_tokens),
                          now=agent.clock() if agent.clock else None, turn=task.turn)

    def _intake_step(self, task: Task) -> bool:
        """Intake for a request of the person, once, before the model's first step: the note is put after their message, a large request is
        recorded in the task registry, and a card is shown when the facts it lacks are worth a question (True: the turn ends on the card).
        Never raises and never waits for a model that is down."""
        facts, task.intake = task.intake, None
        agent = task.agent
        if agent.last_user_text.startswith(qs.ANSWER_HEADER) and self._card_at is not None:
            self._audit("question_answered", {"seconds": round(time.time() - self._card_at, 1), "chars": len(agent.last_user_text)})
            self._card_at = None
        if facts and self._steps_closed(task):
            return True
        if facts and self._long_step(task):
            return True
        if facts:
            self._optimize_step(task, facts)
        if self.intake is None or self.intake.mode == "off" or not facts or not agent.last_user_text.strip():
            return False
        try:
            outcome = self.intake.run(agent.last_user_text, self._intake_context(task, facts))
        except Exception as exc:
            it.log.warning("intake: failed: %s", type(exc).__name__)
            return False
        shown = {"assumptions": outcome.assumptions[:6], "subtasks": [s["title"] for s in outcome.subtasks], "parked": [m["what"] for m in outcome.parked]}
        if outcome.note or outcome.card or outcome.parked:
            self.emit({"event": "intake", "turn": task.turn, "triaged": outcome.triaged, "asked": outcome.card is not None, **shown})
        if outcome.note:
            agent.append({"role": "user", "content": outcome.note, "note": True})
        if outcome.subtasks:
            self._register_subtasks(task, outcome.subtasks)
        for m in outcome.parked:                             # another person, a booking or a resource: left for the person, the rest goes on
            card = it.card_from([m])["questions"][0]
            self._park(qs.parked_payload(card), question_source({"kind": "question", "facts": {"involves": card.get("involves", [])}}))
            self._tl.parked = None
        if outcome.card is None:
            return False
        said = self._question({"mode": "question", "kind": "questions", "source": "intake", **outcome.card, "question": qs.summary(outcome.card), "options": []})
        if said is not None:                                 # nobody to ask: what was proposed is taken, and the model is told
            agent.append({"role": "user", "content": said, "note": True})
            return False
        self._card_at = time.time()
        self._audit("question_shown", {"source": "intake", "questions": [{"id": q["id"], "kind": q["kind"], "options": len(q["options"])} for q in outcome.card["questions"]]})
        text = qs.history_text(outcome.card)
        agent.append({"role": "assistant", "content": text})
        agent.turns.append(al.AgentTurn(len(agent.turns) + 1, task.text, "", text))
        agent.stop_reason, agent.error = al.ASKED, ""
        self._save()
        return True

    def _optimize_step(self, task: Task, facts: Dict[str, Any]) -> None:
        """Optimize prompt (tadamoo/optimize.py): the model reads the technical session's rewrite of the person's message in place of the
        message; their own words stay beside it as `raw`, and `opt` says what happened so the page can show it again after a reload. What
        confirmations quote and what the answer is checked against stay the person's words. Never raises."""
        optimizer, agent = self.optimizer, task.agent
        if optimizer is None or not optimizer.enabled or facts.get("optimize") is False or self.unattended:
            return
        try:
            text = agent.last_user_text
            at = next((i for i in range(len(agent.messages) - 1, -1, -1) if self._is_request(agent.messages[i], text)), None)
            if at is None:
                return
            origin = str(facts.get("origin") or "user")
            if optimizer.ready(text, origin=origin, attended=not self.unattended):
                return
            result = self._optimize_call(task, optimizer, text, opt.previous_words(agent.messages, at), origin)
            if result.state == "skipped":
                return
            message = agent.messages[at]
            if result.state == "sent":
                message["content"] = message["content"].replace(text, result.text, 1)
                message["raw"], message["opt"] = task.text, {"state": "sent"}
            else:
                message["opt"] = {"state": "rejected", "reason": result.reason, "detail": result.detail}
            self._audit("prompt_optimized", {"state": result.state, "reason": result.reason, "ms": result.ms, "chars": [len(text), len(result.text)]})
            self.emit({"event": "optimized", "turn": task.turn, "state": result.state, "reason": result.reason, "detail": result.detail,
                       **({"text": result.text, "original": task.text} if result.state == "sent" else {})})
            self._save()
        except Exception as exc:
            it.log.warning("optimize: failed: %s", type(exc).__name__)

    def _optimize_call(self, task: Task, optimizer: opt.Optimizer, text: str, previous: str, origin: str) -> opt.Result:
        """The call, while the page shows that the prompt is being rewritten. Stop gives it up at once: the original goes on, and the task itself is not stopped."""
        self.emit({"event": "optimizing", "turn": task.turn})
        box: Dict[str, opt.Result] = {}
        finished = threading.Event()

        def work() -> None:
            box["result"] = optimizer.run(text, previous=previous, origin=origin, attended=not self.unattended)
            finished.set()
        threading.Thread(target=work, name="session-optimize", daemon=True).start()
        while not finished.wait(0.1):
            if task.agent.cancel.is_set():
                task.agent.cancel.clear()
                result = opt.Result(reason="stopped")
                break
        else:
            result = box["result"]
        if result.state == "skipped":
            self.emit({"event": "optimized", "turn": task.turn, "state": "skipped", "reason": result.reason})
        return result

    @staticmethod
    def _is_request(message: Dict[str, Any], text: str) -> bool:
        content = message.get("content")
        return (message.get("role") == "user" and not message.get("note") and isinstance(content, str) and text in content
                and not content.startswith(("[harness]", "<untrusted_data")))

    LONG_TEXT = {
        "en": {"q": "This asks for about {words} words ({tokens} tokens), more than one reply writes well. Write it in {parts} parts?",
               "yes": "Yes, in {parts} parts", "yes_d": "The length is estimated and the text planned, each part is written and checked on its own, then the whole is "
                                                         "checked and corrected; you see the text as it comes.",
               "no": "No, in one go", "no_d": "One reply; it will probably come out shorter than asked."},
        "it": {"q": "La richiesta è di circa {words} parole ({tokens} token), più di quante una risposta ne scriva bene. La scrivo in {parts} parti?",
               "yes": "Sì, in {parts} parti", "yes_d": "Si stima la lunghezza e si pianifica il testo, ogni parte è scritta e controllata da sola, poi si controlla "
                                                       "e si corregge il tutto; vedi il testo man mano.",
               "no": "No, in un colpo solo", "no_d": "Una sola risposta; probabilmente sarà più corta del richiesto."},
    }
    LONG_PRIOR_CHARS = 30_000       # the earlier conversation that rides ahead of the parts, newest kept

    def _long_prior(self, agent: Any) -> List[Dict[str, str]]:
        """The text turns of the conversation before the request that needs a long text, for the parts to follow on from: no notes, no tool
        steps, no cards; the newest within a size limit."""
        last = max((i for i, m in enumerate(agent.messages) if m.get("role") == "user" and not m.get("note")), default=len(agent.messages))
        turns = [{"role": m["role"], "content": m["content"]} for m in agent.messages[1:last]
                 if m.get("role") in ("user", "assistant") and not m.get("note") and not m.get("tool_calls") and isinstance(m.get("content"), str)
                 and m["content"].strip() and not m["content"].startswith(("[harness]", qs.ANSWER_HEADER))]
        kept: List[Dict[str, str]] = []
        size = 0
        for turn in reversed(turns):
            size += len(turn["content"])
            if size > self.LONG_PRIOR_CHARS:
                break
            kept.insert(0, turn)
        while kept and kept[0]["role"] != "user":
            kept.pop(0)
        return kept

    def _long_step(self, task: Task) -> bool:
        """A request for a text too long for one reply (serve/longform.py): where the person is, a card offers to write it in parts and the
        turn ends on it (True); where nobody watches the offer is taken at once. Once taken, the harness writes the text itself
        (`_long_run`). Never raises."""
        agent = task.agent
        try:
            text = agent.last_user_text
            if text.startswith(qs.ANSWER_HEADER) and self._long_asked is not None:
                refused, self._long_asked = self._long_asked, None
                if any(label in text for label in refused):
                    self._long_request = None
                    self._audit("long_answer", {"choice": "one_go"})
                    return False
                return bool(self._long_request) and self._long_run(task)
            if self.ask_only or text.startswith(qs.ANSWER_HEADER) or not callable(getattr(agent.client, "chat_completion", None)):
                return False
            asked = lf.parse_hint(text)
            if asked is not None:                             # the person turned the control on: no card
                self._long_request = {**asked, "prior": self._long_prior(agent)}
                self._audit("long_answer", {"asked": asked["parts"], "words": asked["words"], "mode": asked["mode"]})
                return self._long_run(task)
            found = lf.detect_long_request(text)
            if found is None:
                return False
            self._long_request = {**found, "text": text, "prior": self._long_prior(agent)}
            words = (found["words"] // 100) * 100 or found["words"]
            strings = self.LONG_TEXT["it" if lf.request_language(text) == "it" else "en"]
            point = "." if strings is self.LONG_TEXT["it"] else ","
            fill = {"words": f"{words:,}".replace(",", point), "tokens": f"{(found['tokens'] // 100) * 100 or found['tokens']:,}".replace(",", point), "parts": found["parts"]}
            payload = qs.normalise({"questions": [{"id": "long_parts", "text": strings["q"].format(**fill), "kind": "single", "allow_other": False,
                "options": [{"label": strings["yes"].format(**fill), "description": strings["yes_d"], "recommended": True},
                            {"label": strings["no"], "description": strings["no_d"]}]}]})
            self._long_asked = [strings["no"]]
            self._audit("long_answer", {"proposed": found["parts"], "words": found["words"], "reason": found["reason"]})
            said = self._question(payload)
            if said is not None:                              # nobody to ask: the parts are taken, and it is noted
                self._long_asked = None
                agent.append({"role": "user", "content": said, "note": True})
                return self._long_run(task)
            self._card_at = time.time()
            shown = qs.history_text(payload)
            agent.append({"role": "assistant", "content": shown})
            agent.turns.append(al.AgentTurn(len(agent.turns) + 1, task.text, "", shown))
            agent.stop_reason, agent.error = al.ASKED, ""
            self._save()
            return True
        except Exception as exc:
            self._long_asked = None
            it.log.warning("long answer: failed: %s", type(exc).__name__)
            return False

    def _long_spec(self, agent: Any, request: Dict[str, Any]) -> Optional[lf.Spec]:
        """The job for a request that needs a long text, or None when the harness cannot do it alone (a translation with no language named,
        a rewrite with no instruction): the model then answers as usual."""
        fields, text = request.get("fields") or {}, str(request.get("text") or "")
        typed = request.get("reason") == "asked"
        language, style = lf.language_code(fields.get("language", "")) or fields.get("language", ""), fields.get("style", "")
        base = dict(style=style, retries=1, headings=None)
        if request["mode"] == "write":
            return lf.Spec(mode="write", task=fields.get("task") or text, target_words=max(lf.TARGET_WORDS_RANGE[0], min(lf.TARGET_WORDS_RANGE[1], int(request["words"]))),
                           language=language or lf.request_language(text), **base)
        instruction, source = ("", text) if typed else lf.split_request(text)
        if request["mode"] == "translate":
            language = language or lf.target_language(text)
            return lf.Spec(mode="translate", task=instruction, source=source, language=language, **base) if language else None
        instruction = fields.get("task") or instruction
        return lf.Spec(mode="rewrite", task=instruction, source=source, language=language, **base) if instruction else None

    def _long_run(self, task: Task) -> bool:
        """Writes the long text of the request in parts (serve/longflow.py): a new text is estimated, planned, written and checked by the
        harness; a translation or rewrite goes part by part from its source. The text is shown as it passes its checks, progress goes out as
        `long_answer` events, and the text is the answer of the turn (True). False: the text is short, or the job cannot be done alone, and
        the model answers as usual."""
        agent, request = task.agent, self._long_request or {}
        self._long_request = None
        spec = self._long_spec(agent, request)
        if spec is None:
            self._audit("long_answer", {"skipped": "no language or instruction"})
            return False
        client = agent.client
        try:
            spec.context_tokens = int(client.context_window() or 0)
        except Exception:                                            # a client that cannot say: the parts carry the whole text
            spec.context_tokens = 0
        bias = agent.sampling.get("end_turn_bias")
        spec.end_turn_bias = float(bias) if isinstance(bias, (int, float)) and not isinstance(bias, bool) else None
        kw: Dict[str, Any] = {"prior": request.get("prior") or [], "cancel": agent.cancel,
                              "sampling": {k: agent.sampling[k] for k in lf.SAMPLING_FIELDS if agent.sampling.get(k) is not None}}
        if spec.mode == "write":
            run: lf.LongAnswer = lfw.LongFlow(client, spec, hint_words=int(request.get("words") or 0), stated=bool((request.get("fields") or {}).get("words")), **kw)
        else:
            run = lf.LongAnswer(client, spec, **kw)
        done: Dict[str, Any] = {}
        failure = ""
        try:
            for ev in run.run():
                if ev is None:
                    continue
                if ev["event"] == "delta":
                    self._on_delta("content", ev["text"], task)
                else:
                    done = ev if ev["event"] == "done" else done
                    self._on_event({"type": "long_answer", **ev}, task)
        except lfw.NotLong as short:                                 # the estimate says it fits one reply
            self._audit("long_answer", {"estimate": short.words, "result": "one_reply"})
            return False
        except lf.LongformError as exc:
            it.log.warning("long answer: not accepted: %s", exc)
            return False
        except StreamCancelled:
            failure = "cancelled"
        except Exception as exc:                                     # the model failed after some parts: what was written is kept
            failure = f"{type(exc).__name__}: {exc}"
        with self._delta_lock:
            self._flush_delta()
        answer = run.text()
        cancelled = failure == "cancelled" or done.get("stopped") == "cancelled"
        if not answer.strip():
            agent.stop_reason, agent.error = (al.CANCELLED, "") if cancelled else (al.ERROR, failure)
            return True
        usage = done.get("usage") or {}
        agent.append({"role": "assistant", "content": answer})
        agent.turns.append(al.AgentTurn(len(agent.turns) + 1, task.text, "", answer, prompt_tokens=int(usage.get("prompt_tokens") or 0),
                                        completion_tokens=int(usage.get("completion_tokens") or 0), cached_tokens=int(usage.get("cached_tokens") or 0),
                                        finish_reason=str(done.get("finish") or "length")))
        agent.total_tokens += int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        agent.stop_reason = al.CANCELLED if cancelled else al.ERROR if failure else al.FINAL
        agent.error = "" if cancelled else failure
        self._audit("long_answer", {"words": done.get("words"), "parts": done.get("segments"), "calls": usage.get("calls"), "applied": done.get("applied"),
                                    "unapplied": len(done.get("unapplied") or []), "stopped": done.get("stopped") or ""})
        self._save()
        return True

    def _register_subtasks(self, task: Task, subtasks: List[Dict[str, Any]]) -> None:
        """A large request as a parent and its parts in the server's task registry (`req-<turn>`, `req-<turn>.1`, ...)."""
        if self.task_sink is None:
            return
        parent, title = it.TASK_PREFIX + task.turn, " ".join(task.agent.last_user_text.split())
        ids = [f"{parent}.{i}" for i in range(1, len(subtasks) + 1)]
        self.task_sink.record(parent, "running", title[:200], children=ids, metadata={"kind": "intake", "title": title[:80]})
        for tid, st in zip(ids, subtasks):
            self.task_sink.record(tid, "pending", st["title"], metadata={"kind": "intake", "title": st["title"], "span": st["span"], "acceptance": st["acceptance"],
                                                                         "depends_on": [ids[d] for d in st["depends_on"]]})
        task.registered = (parent, ids)

    def _close_registry(self, task: Task) -> None:
        """When the request ends: the parent is done if the model finished, else closed; the parts are closed, never marked done on the model's word."""
        if task.registered is None or self.task_sink is None:
            return
        parent, ids = task.registered
        task.registered = None
        self.task_sink.record(parent, "done" if task.agent.stop_reason == al.FINAL else "closed", al.answer_text(task.agent.turns)[:200] or task.agent.stop_reason)
        for tid in ids:
            self.task_sink.record(tid, "closed", "")

    # -- the card after a task: next steps, and the project's own checks offered once ---------------------------

    def _next_steps(self, task: Task) -> bool:
        """After a final answer, where the person is: a card built by code from the facts of the work (tadamoo/nextsteps.py). True when it was
        shown, and then the chips are not. Never raises."""
        agent = task.agent
        try:
            if (self.unattended or self.ask_only or task.side or task.skill is not None or getattr(agent, "subagent", False) or agent.cut or agent.error
                    or agent.stop_reason not in (al.FINAL, al.VERIFY_FAILED) or agent.last_user_text.startswith(qs.ANSWER_HEADER)
                    or getattr(self.built.permissions, "plan_mode", False)):
                return False
            checks = []
            if self.checks_offer and ns.written_paths(agent.turns[task.since:]):
                from tadamoo import project_profile
                checks = project_profile.detect(self.workspace)
            facts = ns.facts_from(agent, task.since, checks, getattr(agent, "lessons_for", None))
            payload = ns.build(facts, lf.request_language(agent.last_user_text) or "en", show_next=ns.enabled())
            if payload is None:
                return False
            meta = payload.pop("_meta")
            if self._question(payload) is not None:
                return False
            self.checks_offer = self.checks_offer and "offer" not in payload              # offered once; the server remembers it for the next conversations
            self._steps_meta = meta
            self._audit("question_shown", {"source": "next_steps", "questions": [{"id": q["id"], "kind": q["kind"], "options": len(q["options"])} for q in payload["questions"]]})
            return True
        except Exception as exc:
            it.log.warning("next steps: failed: %s", type(exc).__name__)
            return False

    def _steps_closed(self, task: Task) -> bool:
        """The answer to the card of next steps, when it asks for nothing to be done: the turn ends here with a word of thanks and no model call."""
        meta, self._steps_meta = self._steps_meta, None
        if meta is None:
            return False
        agent = task.agent
        closing = ns.settle(meta, agent.last_user_text)
        if closing is None:
            return False
        agent.append({"role": "assistant", "content": closing})
        agent.turns.append(al.AgentTurn(len(agent.turns) + 1, task.text, "", closing))
        agent.stop_reason, agent.error = al.FINAL, ""
        self._save()
        return True

    # -- suggestions: the chips after an answer --------------------------------------------------------------

    def _maybe_suggest(self, task: Task) -> None:
        """Starts the suggestion call for the answer that just ended, if every condition holds (see tadamoo/suggestions.py)."""
        s, agent = self.suggester, task.agent
        if s is None or task.side or task.skill is not None:
            return
        last = agent.turns[-1] if agent.turns else None
        reason = s.skip_reason(answer=al.answer_text(agent.turns), stop_reason=agent.stop_reason, cut=agent.cut, error=agent.error, attended=not self.unattended,
                               plan_mode=bool(getattr(self.built.permissions, "plan_mode", False)),
                               idle=not self._more() and not self._waiting_n and not self.blocked, prompt_tokens=last.prompt_tokens if last else 0,
                               cached_tokens=last.cached_tokens if last else 0)
        if reason:
            it.log.info("suggestions: skipped (%s)", reason)
            return
        self._suggest_cancel.clear()
        from tadamoo.context import wire_messages
        threading.Thread(target=self._suggest, args=(task.turn, wire_messages(agent.messages), agent.tools.schemas()), name="session-suggest", daemon=True).start()

    def _suggest(self, turn: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> None:
        deadline = time.monotonic() + 3.0
        while self._workers > 0 and time.monotonic() < deadline and not self._suggest_cancel.is_set():
            time.sleep(0.02)                                 # the answer's own task is still closing
        if self._suggest_cancel.is_set() or not self.idle():
            return
        got = self.suggester.generate(messages, tools, self._suggest_cancel.is_set)
        if not got["items"] or self._suggest_cancel.is_set():
            return
        self.suggester.offered = [i["prompt"] for i in got["items"]]
        self._audit("suggestions", {"count": len(got["items"]), "ms": got["ms"], "tokens": got["tokens"]})
        self.emit({"event": "suggestions", "turn": turn, "items": got["items"], "ms": got["ms"], "tokens": got["tokens"]})

    def _run_task(self, task: Task) -> str:
        """Runs one task to where it stops, with new tries while the model is busy, not loaded or not answering. Returns "done", "suspended" or "blocked"."""
        agent = task.agent
        attempt = 0
        self._tl.task = task
        self.turn_id = task.turn
        task.interrupt = False
        agent.cancel.clear()
        self.emit({"event": "started", "turn": task.turn, "side": task.side})
        if task.skill is not None:
            task.state_running = True
            try:
                return self._run_skill_task(task)
            finally:
                task.state_running = False
        refresh = getattr(self.built, "refresh_budget", None)       # the model may have been loaded or changed since the last request
        if refresh is not None:
            refresh()
        if task.intake is not None and self._intake_step(task):
            return "done"
        while True:
            self.last_progress = time.monotonic()
            task.state_running = True
            try:
                agent.run(None)
            finally:
                task.state_running = False
            if agent.stop_reason == al.SUSPENDED:
                return "suspended"
            if agent.stop_reason == al.CANCELLED and task.interrupt:      # the model call was cut to look at an urgent message
                task.interrupt = False
                agent.cancel.clear()
                if self._safe_point(task) == al.SUSPENDED:
                    return "suspended"
                continue
            if agent.stop_reason != al.ERROR:
                return "done"
            status = agent.error_status                                    # a model error never blocks: the model is waited for, and loaded when it is not
            if self.classify_errors and error_classifier.classify(status, agent.error) == error_classifier.DETERMINISTIC:
                self.emit({"event": "model_error", "turn": task.turn, "status": status, "error": agent.error, "retry": False})
                return "done"                                              # the turn ends on the error; the `done` event carries it
            health = self.health()
            wait = self.waits[min(attempt, len(self.waits) - 1)]          # the last wait is repeated: only the person stops the retries
            attempt += 1
            waiting_for, load_error = self._model_wait(health, wait)
            self.emit({"event": "retrying", "turn": task.turn, "attempt": attempt, "wait_s": wait,
                       "status": status, "error": agent.error, "reachable": health["reachable"], "loading": health["loading"],
                       "loaded": health["loaded"], "auto_load": health["auto_load"], "waiting_for": waiting_for,
                       **({"load_error": load_error} if load_error else {})})
            if not self._wait(wait, agent.cancel):
                agent.stop_reason = al.CANCELLED
                return "done"

    def _more(self) -> bool:
        return bool(self.stack or self.queue or self.urgent or self.inbox or self.skill_queue)

    def _work(self, task: Optional[Task]) -> None:
        try:
            while task is not None:
                outcome = self._run_task(task)
                if outcome == "blocked":
                    return
                if outcome == "suspended":
                    task.state_running = task.interrupt = False
                    with self._lock:
                        self.stack.append(task)
                        nxt = self.urgent.pop(0) if self.urgent else None
                    self.emit({"event": "suspended", "turn": task.turn, "by": nxt["turn"] if nxt else None})
                    task = self._begin_side(nxt["turn"], nxt["text"], nxt.get("attachments")) if nxt else self._next_after(task)
                    continue
                agent = task.agent
                final = al.answer_text(agent.turns)
                self._save()
                self.emit({"event": "done", "turn": task.turn, "stop_reason": agent.stop_reason, "error": agent.error, "final": final,
                           "tokens": agent.total_tokens, "max_turns": agent.max_turns, "cut": agent.cut, "more": self._more() and not agent.cancel.is_set(),
                           "denied": self.permission_denials["total"]})
                self.tasks.pop(task.turn, None)
                self._close_registry(task)
                if not self._more() and not agent.cancel.is_set() and not self._next_steps(task):
                    self._maybe_suggest(task)
                task = self._next_after(task)
        except BaseException as exc:                       # the process stays up: the task ends with an error
            self._audit("crash", {"traceback": traceback.format_exc()[-4000:]})     # the short message is what the person sees; the file and line are only in the audit log
            self._block(task, "crash", f"{type(exc).__name__}: {exc}")
        finally:
            self._tl.task = None
            with self._lock:
                self._workers -= 1
                self.busy = self._workers > 0
            if self._poll_due and self.idle():
                try:
                    self.poll_plans()
                except Exception:
                    pass                                         # a failed look is tried again at the next one

    def _next_after(self, finished: Optional[Task]) -> Optional[Task]:
        """What to do when a task ends: an urgent request first, then the task that was put aside, then what waited its turn."""
        if finished is not None and finished.agent.cancel.is_set():
            with self._lock:
                self.stack, self.queue, self.urgent, self.inbox, self.skill_queue = [], [], [], [], []      # Stop means stop
            return None
        with self._lock:
            leftovers, self.inbox = self.inbox, []
        for msg in leftovers:                              # what came as the task ended: it has no task left to be added to
            how, why = self.router.route(msg["text"], self._context_of(finished or self.main))
            hurry = how == URGENT and bool(self.stack or (finished is not None and finished.side))
            with self._lock:
                (self.urgent if hurry else self.queue).append(msg)
                position = len(self.queue)
            self.emit({"event": "routed", "turn": msg["turn"], "how": URGENT if hurry else QUEUE, "why": why, "position": 0 if hurry else position})
        with self._lock:
            if self.urgent:
                msg = self.urgent.pop(0)
                side = True
            elif self.stack:
                back = self.stack.pop()
                back.agent.append({"role": "user", "content": RESUME_NOTE})
                back.agent.cancel.clear()
                self.emit({"event": "resumed", "turn": back.turn})
                return back
            elif self.queue:
                msg = self.queue.pop(0)
                side = False
            elif self.skill_queue:
                turn_id, skill = self.skill_queue.pop(0)
                msg = None
            else:
                return None
        if msg is None:
            self.turn_id = turn_id
            return self._begin_skill(turn_id, skill)
        if side:
            return self._begin_side(msg["turn"], msg["text"], msg.get("attachments"))
        self.emit({"event": "accepted", "turn": msg["turn"]})
        return self._begin_main(msg["turn"], msg["text"], "user", msg.get("attachments"), msg.get("optimize") is not False)

    def _block(self, task: Optional[Task], reason: str, detail: str) -> None:
        """The task cannot go on and waiting will not change it: the person decides what happens next."""
        self.blocked = {"reason": reason, "detail": detail, "options": ["resume", "close"]}
        self._save()
        self.emit({"event": "blocked", "turn": task.turn if task else self.turn_id, **self.blocked})

    def close(self, timeout: float = 15.0) -> None:
        self.cancel()
        deadline = time.monotonic() + timeout
        while self._workers > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self._save()


# -- the process ---------------------------------------------------------------------------------------------

def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_settings(cmd: Dict[str, Any]) -> bool:
    """A "settings" command: any of the steps one answer may take (-1 for no limit, or 1 and more), the end-of-turn bias (-20 to 0), the
    temperature (0 to 5, or null) and the most a reply may write (1 and more, or null)."""
    steps, bias, temp, room = cmd.get("max_turns"), cmd.get("end_turn_bias"), cmd.get("temperature"), cmd.get("max_tokens")
    if not any(k in cmd for k in ("max_turns", "end_turn_bias", "temperature", "max_tokens")):
        return False
    if "max_turns" in cmd and (not isinstance(steps, int) or isinstance(steps, bool) or not (steps == -1 or steps >= 1)):
        return False
    if "temperature" in cmd and temp is not None and not (_number(temp) and 0.0 <= temp <= 5.0):
        return False
    if "max_tokens" in cmd and room is not None and (not isinstance(room, int) or isinstance(room, bool) or room < 1):
        return False
    return "end_turn_bias" not in cmd or (_number(bias) and -20.0 <= bias <= 0.0)


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tadamoo session", description="One Chat conversation in a long-lived process")
    parser.add_argument("--session-id", default="", help="the conversation's id; its history is kept under it")
    parser.add_argument("--history-dir", default=None, help="where the history is kept (default: in the workspace's data folder)")
    add_agent_arguments(parser)
    parser.add_argument("--unattended", action="store_true",
                        help="nobody answers questions here: an approval is given at once, what only the person can give is reported as not available, "
                             "and a question about another person, money or a booking waits in the pending list")
    parser.add_argument("--cross-commands", action="append", default=[], metavar="NAME",
                        help="a conversation (a name, an id or a pattern with * and ?) whose messages may give orders here; what any other conversation sends is data")
    parser.add_argument("--ask-server", action="store_true", help="ask for approvals on the server's pages instead of in the conversation")
    parser.add_argument("--ask-only", action="store_true", help="answer without tools: a greeting does not become a loop")
    parser.add_argument("--report-changes", action="store_true", help="send each file edit to the server's Changes panel, to keep or undo by block")
    parser.add_argument("--no-planner", action="store_true", help="do not offer the planner's tools")
    parser.add_argument("--effort", default="", choices=["", *EFFORT_LEVELS], help="how long the model thinks (empty: the model's own default)")
    parser.add_argument("--router", choices=["model", "rules"], default="model",
                        help="how a message that comes during a task is judged when its words do not decide: by the model, or not at all")
    parser.add_argument("--pipeline", choices=["off", "light", "full"], default="full",
                        help="the check of an answer against what the request asked: nothing, measured only, or measured and judged")
    parser.add_argument("--intake", choices=it.MODES, default=it.DEFAULT_MODE,
                        help="what is looked at before the model's first step: nothing, cleaning and a note from rules (no model call), or also one triage call when the message calls for it")
    parser.add_argument("--side-call-tokens", type=int, default=it.DEFAULT_SIDE_CALL_TOKENS, metavar="TOKENS",
                        help="the largest conversation in which the extra model calls (triage, suggestions) are made")
    parser.add_argument("--suggestions", choices=("on", "off"), default="off", help="offer next-prompt chips after an answer (an unattended run never does)")
    parser.add_argument("--offer-checks", action="store_true", help="after the first work that changes files, offer the checks this folder seems to have, once (an unattended run never does)")
    parser.add_argument("--optimize-prompt", choices=("on", "off"), default="off",
                        help="rewrite each message of the person in the technical session before the model reads it (needs --side-slot on the server)")
    parser.add_argument("--heartbeat", type=float, default=HEARTBEAT_S)
    parser.add_argument("--watch-plans", type=float, default=15.0, metavar="SECONDS", help="how often the plans are looked at in a quiet moment (0: never)")
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
    sid = args.session_id or uuid.uuid4().hex[:12]
    confirm = None
    if args.ask_server:
        from tadamoo import outward_calls
        from tadamoo.approval_client import ServerApprover
        outward_calls.report_to(args.endpoint, args.api_key)
        confirm = ServerApprover(args.endpoint, args.api_key, sid)
        if args.unattended:
            confirm = unattended_confirm(confirm)
    history = Path(args.history_dir) if args.history_dir else data_dir_for(Path(args.workspace).resolve()) / "sessions" / sid
    try:
        settings = agent_settings(args)
    except FileNotFoundError as exc:                              # the reference folder is gone
        emit({"event": "error", "code": "start", "message": str(exc)})
        return 1
    settings["council"] = not args.ask_only                       # the council is offered whenever the person set one up
    settings["unattended"] = args.unattended
    if args.ask_only:                                             # answers without tools: nothing that reaches outside is built
        settings.update(capabilities=False, lsp=False, vault_dir="", payments_dir="", browser=False, wiki=False, phone=False)
    cfg = AgentConfig(**settings, confirm=confirm, run_id=sid, snapshot_dir=str(history / "snapshots"))
    from tadamoo.client import TadamooClient
    from tadamoo.llm import client_for, judge_for
    from tadamoo.llm.judge import router_judge
    here = TadamooClient(base_url=cfg.endpoint, api_key=cfg.api_key)          # the model loaded in this TaDaMoo
    try:
        built = build_agent(cfg, client=client_for("executor", here), summarizer=client_for("compaction", here), technical=client_for("technical", here))
    except Exception as exc:
        emit({"event": "error", "code": "start", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    if args.ask_only:
        from tadamoo.tools.registry import ToolRegistry
        built.agent.tools = ToolRegistry()
    store = SessionStore(history, redact=built.agent.secrets.mask_secrets if built.agent.secrets else None)
    router = Router(router_judge(judge_for("router", here)) if args.router == "model" else None)
    if hasattr(built.agent.context, "on_removed"):
        built.agent.context.on_removed = store.archive
    built.agent.pipeline = Pipeline(args.pipeline, (lambda q, o, c, j=judge_for("pipeline", here): j.choose(q, o, c)) if args.pipeline == "full" else None)
    session = Session(built, store, emit, session_id=sid, health=lambda: server_health(cfg.endpoint, cfg.api_key), router=router,
                      unattended=args.unattended, classify_errors=cfg.error_classifier)
    if args.report_changes and not args.ask_only:
        from tadamoo.reporting import PanelReporter
        session.reporter = PanelReporter(cfg.endpoint, cfg.api_key, built.sandbox, "", session=sid, tasks=False)
    if args.attachments_dir:
        from tadamoo.attachments import AttachmentStore
        session.attachment_store = AttachmentStore(Path(args.attachments_dir))
    session.endpoint, session.api_key = cfg.endpoint, cfg.api_key
    built.agent.cross_commands = tuple(args.cross_commands)
    seen = prov.SeenDeliveries(history)
    session.ask_only = args.ask_only
    session.workspace, session.checks_offer = cfg.workspace, args.offer_checks and not args.ask_only and not args.unattended
    session.intake = it.Intake(args.intake, built.technical, args.side_call_tokens)
    session.optimizer = opt.Optimizer(built.technical, args.optimize_prompt == "on" and not args.unattended)
    session.task_sink = it.TaskSink(cfg.endpoint, cfg.api_key) if args.report_changes else None
    session.suggester = sg.Suggester(built.technical, args.suggestions == "on" and not args.unattended, args.side_call_tokens)
    session.set_effort(args.effort)
    if built.agent.secrets is not None:
        built.agent.secrets.ask_code = session.ask_code
    if getattr(built.agent.tools, "capability_ask", None) is not None and not args.unattended:
        built.agent.tools.capability_ask.ask = session._ask                      # find_online_capability puts its choice to the person here; unattended, it queues the request
    if getattr(built.agent.tools, "mcp_asker", None) is not None:
        built.agent.tools.mcp_asker.ask = session._ask                           # so does an MCP server that stops to ask for a few values
    if built.payment_confirm is not None:
        built.payment_confirm.ask = session.ask_payment
        built.payment_confirm.notice = session.payment_notice
    if not args.ask_server:
        built.permissions.confirm_callback = session.permission_confirm      # the questions are put in the conversation

        def auto_approved(tool: str, arguments: Dict[str, Any]) -> None:
            shown = json.dumps(arguments, ensure_ascii=False, default=str)[:300]
            session._audit("auto_approved", {"tool": tool, "arguments": shown})                  # the log keeps what the technical session let through
            session.emit({"event": "auto_approved", "turn": session._cur_turn(), "tool": tool, "arguments": shown})
        built.permissions.on_auto_approved = auto_approved
        jobs = getattr(built.agent.tools, "jobs", None)
        if jobs is not None:                                                                         # a background job's output reaches the chat as it is written
            jobs.sink = lambda event: session.emit({**event, "event": "job_output" if "text" in event else "job", "turn": session._cur_turn()})
    if not args.ask_only and not args.no_planner:
        from tadamoo.integrations.planner_client import PlannerClient
        session.enable_planner(PlannerClient(cfg.endpoint, cfg.api_key))
    if not args.ask_only and args.orchestration:
        session.enable_orchestration()
    if not args.ask_only and built.capabilities is not None:
        def activate(cap_id: str) -> Optional[str]:
            if cap_id != "builtin:orchestration":
                return None
            session.enable_orchestration()
            return "The tools to message and start other conversations are available for the rest of this conversation."
        built.capabilities.activate = activate
    restored = session.restore()
    emit({"event": "ready", "session": sid, "restored": restored, "pid": os.getpid(), "blocked": session.blocked,
          "parked": session.parked_questions()})

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(args.heartbeat):
            emit({"event": "heartbeat", "busy": session.busy, "turn": session.turn_id, "asking": session.waiting,
                  "waiting_requests": len(session.queue) + len(session.inbox) + len(session.urgent), "put_aside": len(session.stack),
                  "idle_s": session.idle_seconds(), "denied": session.permission_denials["total"]})
    threading.Thread(target=beat, name="session-heartbeat", daemon=True).start()

    def watch() -> None:
        while not stop.wait(args.watch_plans):
            try:
                session.poll_plans()
            except Exception:
                pass                                     # a failed look is tried again at the next one
    if args.watch_plans > 0 and session.planner is not None:
        threading.Thread(target=watch, name="session-plans", daemon=True).start()

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
                ids = [i for i in cmd.get("attachments", []) if isinstance(i, str)] if isinstance(cmd.get("attachments"), list) else []
                if cmd.get("place") is not None and built.take_place:
                    built.take_place(cmd["place"])                  # before the message, so the tool is there from this turn on
                delivery = cmd["delivery"] if isinstance(cmd.get("delivery"), str) else ""
                if delivery and delivery in seen:
                    emit({"event": "delivery", "id": delivery, "state": "accepted"})        # written twice: it was taken the first time
                    continue
                took = session.send(str(cmd.get("id") or uuid.uuid4().hex[:8]), cmd["text"], origin=prov.clean(cmd.get("provenance")) if delivery else "user",
                                    attachments=ids or None, optimize=cmd.get("optimize") is not False)
                if delivery:
                    if took:
                        seen.add(delivery)
                    emit({"event": "delivery", "id": delivery, "state": "accepted" if took else "busy"})
            elif name == "cross_commands" and isinstance(cmd.get("patterns"), list):
                built.agent.cross_commands = tuple(str(p) for p in cmd["patterns"])
                emit({"event": "cross_commands", "patterns": list(built.agent.cross_commands)})
            elif name == "answer":
                session.answer(cmd.get("id"), cmd.get("value"))
            elif name == "promote":
                session.promote(str(cmd.get("id")))
            elif name == "drop":
                session.drop(str(cmd.get("id")))
            elif name == "cancel":
                session.cancel()
            elif name == "mode" and cmd.get("mode") in PERMISSION_MODES and not args.ask_only:
                built.set_mode(cmd["mode"])
                emit({"event": "mode", "mode": cmd["mode"]})
            elif name == "role" and valid_role(cmd.get("role")) and not args.ask_only:
                built.set_role(cmd["role"])
                emit({"event": "role", "role": cmd["role"]})
            elif name == "checks" and isinstance(cmd.get("safe"), list) and isinstance(cmd.get("checks"), list) and not args.ask_only:
                built.set_checks([str(x) for x in cmd["safe"]], [str(x) for x in cmd["checks"]])
                emit({"event": "checks", "safe": len(built.permissions.safe_commands), "checks": len(cmd["checks"])})
            elif name == "effort" and cmd.get("level", "") in ("", *EFFORT_LEVELS):
                session.set_effort(cmd.get("level", ""))
                emit({"event": "effort", "level": cmd.get("level", "")})
            elif name == "intake" and cmd.get("mode") in it.MODES and session.intake is not None:
                session.intake.mode = cmd["mode"]
                emit({"event": "intake_mode", "mode": cmd["mode"]})
            elif name == "optimize" and isinstance(cmd.get("on"), bool) and session.optimizer is not None:
                session.optimizer.enabled = cmd["on"] and not args.unattended
                emit({"event": "optimize_prompt", "on": session.optimizer.enabled})
            elif name == "settings" and _valid_settings(cmd):
                changed = {}
                if "max_turns" in cmd:
                    session.set_max_turns(cmd["max_turns"])
                    changed["max_turns"] = cmd["max_turns"]
                if "end_turn_bias" in cmd:
                    session.set_end_turn_bias(float(cmd["end_turn_bias"]))
                    changed["end_turn_bias"] = float(cmd["end_turn_bias"])
                if "temperature" in cmd:
                    session.set_temperature(None if cmd["temperature"] is None else float(cmd["temperature"]))
                    changed["temperature"] = cmd["temperature"]
                if "max_tokens" in cmd:
                    session.set_max_tokens(cmd["max_tokens"])
                    changed["max_tokens"] = cmd["max_tokens"]
                emit({"event": "settings", **changed})
            elif name == "command" and isinstance(cmd.get("name"), str):
                threading.Thread(target=session.run_command, args=(str(cmd.get("id") or ""), cmd["name"], str(cmd.get("args") or "")[:2000]),
                                 name="session-command", daemon=True).start()           # a command may take a model call; the others go on meanwhile
            elif name == "skill" and isinstance(cmd.get("script"), str) and isinstance(cmd.get("values"), dict):
                session.run_skill(str(cmd.get("id") or uuid.uuid4().hex[:8]),
                                  {k: cmd.get(k) for k in ("name", "display", "script", "values", "timeout", "cache_dir")})
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
    emit({"event": "closed", "session": sid, "permission_denials": session.permission_denials})
    return 0


if __name__ == "__main__":
    sys.exit(main())
