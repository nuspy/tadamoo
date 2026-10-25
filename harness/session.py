"""One conversation in one long-lived process: `python -m harness session`.

The server starts one of these for each conversation of the Chat and talks to it in lines of JSON: commands in on
stdin, events out on stdout. The process builds the agent once (tools, skills, connections), so a message does not pay
for the start, and saves the history after every model turn, so a process that dies can be started again from it.

Commands: {"cmd": "send", "id", "text"}, {"cmd": "answer", "id", "value"}, {"cmd": "cancel"}, {"cmd": "resume"}, {"cmd": "close"},
{"cmd": "promote", "id"}, {"cmd": "drop", "id"}, {"cmd": "ping"}.
Events: ready, accepted, queued, routed, started, suspended, resumed, agent (an assistant text or a tool start or end), delta
(text as it is written), ask (a question with buttons: a confirmation the task waits for, or a question that ends the turn),
retrying, done (with `more` when other tasks are waiting), blocked, cleared, dropped, heartbeat, error.

A message that comes while a task is under way is looked at before the task's next model call (harness/router.py): it goes into
that task (steer), waits its turn (queue), or, when it cannot wait and has nothing to do with the task, the task is put aside where
it stands, the urgent one is done, and the first goes on afterwards. When the model answers several requests at once, a request that
would wait is done beside the task instead.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

from harness import agent_loop as al
from harness.client import StreamCancelled
from harness.pipeline import Pipeline
from harness.router import QUEUE, STEER, STRONG_URGENT, URGENT, Router
from harness.stack import AgentConfig, BuiltAgent, build_agent, data_dir_for
from harness.vault import valid_code

HEARTBEAT_S = 5.0
DELTA_CHARS, DELTA_S = 400, 0.25
EVENT_CHARS = 20_000
TRANSIENT_STATUS = frozenset({0, 408, 425, 429, 500, 502, 503, 504})
BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0)
HISTORY_FILE, INFLIGHT_FILE, ARCHIVE_FILE = "history.json", "inflight.json", "archive.jsonl"
RESUME_NOTE = "[harness] You were set aside for an urgent request, which is now done. Go on with what you were doing."
INTERRUPTED_NOTE = ("[harness] The previous turn was interrupted while it was running {tool} with {arguments}. It may have "
                    "run already. Do not run it again without first checking what really happened, and tell the user.")


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
    interrupt: bool = False                       # asked to stop its model call now, to be put aside
    state_running: bool = False                   # a model call or a tool is in progress


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


class Session:
    def __init__(self, built: BuiltAgent, store: SessionStore, emit: Callable[[Dict[str, Any]], None], session_id: str = "",
                 waits: tuple = BACKOFF_S, sleep: Callable[[float], None] = time.sleep,
                 health: Optional[Callable[[], Dict[str, Any]]] = None, router: Optional[Router] = None) -> None:
        self.built, self.store, self.emit = built, store, emit
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
        self.stack: List[Task] = []                             # tasks put aside, to go on afterwards
        self.tasks: Dict[str, Task] = {}                        # the tasks that exist, by turn
        self._pending_note: Optional[str] = None
        self._asks: Dict[str, Dict[str, Any]] = {}              # confirmations a task is waiting for, by id
        self._ask_lock = threading.Lock()
        self._waiting_n = 0                                     # a task waiting for the person is not stuck
        self.planner: Any = None
        self._summary = ""                                      # the plans summary the conversation last got
        self.attachment_store: Any = None                       # AttachmentStore of this conversation's files, when it has one
        self.endpoint, self.api_key = "", None
        self._window: Optional[Tuple[int, float]] = None
        self._announced: set = set()                            # what the planner already woke the conversation about
        self._delta_lock = threading.Lock()
        self._delta_kind, self._delta_turn, self._delta_text, self._delta_at = "", "", [], time.monotonic()
        self.current = _Current(self)
        self.main = Task("main", "", self.agent)
        self._adopt(self.main)

    @property
    def waiting(self) -> bool:
        return self._waiting_n > 0

    def _adopt(self, task: Task) -> None:
        a = task.agent
        a.on_event = lambda e, t=task: self._on_event(e, t)
        a.on_turn_end = lambda turn, t=task: self._on_turn_end(turn, t)
        a.on_delta = lambda k, x, t=task: self._on_delta(k, x, t)
        a.before_inference = lambda t=task: self._safe_point(t)

    # -- the history -----------------------------------------------------------------------------------

    def _pending(self) -> List[Dict[str, str]]:
        return [*self.urgent, *self.queue, *self.inbox]

    def _save(self) -> None:
        self.store.save(self.agent.messages[1:], {"session": self.session_id, "turns": self.turns_done, "plans_summary": self._summary, "announced": sorted(self._announced),
                                                  "pending": self._pending()})

    def restore(self) -> int:
        """Takes the saved history back. An action that cannot be undone and was running when the process died is
        reported at once, and requests that were waiting are kept for the person to release: nothing goes on until they say so."""
        data = self.store.load()
        count = 0
        if data:
            self.agent.messages = [self.agent.messages[0], *data["messages"]]
            self.turns_done = int(data.get("turns") or 0)
            self._summary = str(data.get("plans_summary") or "")
            self._announced = {str(k) for k in data.get("announced", [])}
            self.queue = [m for m in data.get("pending", []) if isinstance(m, dict) and m.get("text")]
            count = len(data["messages"])
        lost = self.store.inflight()
        if lost:
            self._pending_note = INTERRUPTED_NOTE.format(tool=lost.get("tool"), arguments=json.dumps(lost.get("arguments"), ensure_ascii=False)[:300])
            self.blocked = {"reason": "interrupted_irreversible", "detail": f"{lost.get('tool')} may have run before the process stopped",
                            "options": ["resume", "close"], "tool": lost.get("tool"), "arguments": lost.get("arguments")}
            self.store.clear_inflight()
        elif self.queue:
            self.blocked = {"reason": "unfinished_requests", "detail": f"{len(self.queue)} request(s) were waiting when the process stopped",
                            "options": ["resume", "close"]}
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

    # -- questions to the person -------------------------------------------------------------------------

    def _cur_turn(self) -> str:
        task = getattr(self._tl, "task", None)
        return task.turn if task is not None else self.turn_id

    def _cur_cancel(self) -> threading.Event:
        task = getattr(self._tl, "task", None)
        return (task.agent if task is not None else self.agent).cancel

    def _ask(self, payload: Dict[str, Any]) -> str:
        """A confirmation: shown with its buttons, and the task waits for the answer ("confirm", "cancel" or "" if it was
        cancelled). A task waiting for the person is not stuck."""
        aid = uuid.uuid4().hex[:8]
        slot = {"event": threading.Event(), "value": "", "values": {o["value"] for o in payload.get("options", [])}, "free": bool(payload.get("free"))}
        cancel = self._cur_cancel()
        with self._ask_lock:
            self._asks[aid] = slot
        self.emit({"event": "ask", "id": aid, "turn": self._cur_turn(), **payload})
        self._waiting_n += 1
        try:
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
        if slot is None or not (value in slot["values"] or (slot["free"] and valid_code(value))):
            self.emit({"event": "error", "code": "bad_answer", "message": "no such question, or not one of its answers"})
            return False
        slot["value"] = value
        slot["event"].set()
        return True

    def _question(self, payload: Dict[str, Any]) -> bool:
        """A question that ends the turn: the button the person presses is their next message."""
        self.emit({"event": "ask", "id": uuid.uuid4().hex[:8], "turn": self._cur_turn(), **payload})
        return True

    def ask_code(self, who: str) -> str:
        """A second-factor code typed by the person. It goes to the tool that needs it and nowhere else: not to the model, not to the history."""
        return self._ask({"mode": "confirm", "kind": "code", "free": True, "facts": {"who": who},
                          "question": f"Type the code for {who}.", "options": []})

    def ask_payment(self, request: Dict[str, Any]) -> bool:
        """A payment the broker will not decide alone: no waiver covers it, or it is over the caps. The person decides."""
        question = f"Pay {request['amount']} to {request['recipient']} ({request['reason']}) with {request['system']}?"
        return self._ask({"mode": "confirm", "kind": "payment", "facts": request, "question": question,
                          "options": [{"label": "Allow", "value": "confirm"}, {"label": "Deny", "value": "cancel"}]}) == "confirm"

    def permission_confirm(self, tool: str, arguments: Dict[str, Any]) -> bool:
        """The permission manager's question, put to the person in the conversation."""
        shown = json.dumps(arguments, ensure_ascii=False)[:300]
        return self._ask({"mode": "confirm", "kind": "permission", "tool": tool, "arguments": arguments,
                          "question": f"Allow {tool} {shown}?",
                          "options": [{"label": "Allow", "value": "confirm"}, {"label": "Deny", "value": "cancel"}]}) == "confirm"

    def enable_planner(self, client: Any) -> None:
        """Gives the conversation the planner's tools and the question tool; every change to a plan asks."""
        from harness.planner_tools import Confirmer, register_ask_tool, register_planner_tools
        self.planner = client
        register_planner_tools(self.agent.tools, client, Confirmer(self.current, ask=self._ask))
        register_ask_tool(self.agent.tools, self.current, self._question)

    def _plans_note(self) -> Optional[str]:
        """What the conversation should be told about the plans: only when they changed since it last was, and before the
        person's message, so what follows a message from outside is never a claim about plans."""
        if self.planner is None:
            return None
        from harness.planner_tools import plans_summary
        summary = plans_summary(self.planner)
        if summary == self._summary:
            return None
        self._summary = summary
        return "[harness] The user's plans now:\n" + summary if summary else "[harness] The user has no active plans now."

    # -- commands --------------------------------------------------------------------------------------

    def _begin_main(self, turn: str, text: str, origin: str = "user", attachments: Optional[List[str]] = None) -> Task:
        """Starts a turn of the person's own conversation: what the agent should know first, then their message."""
        agent = self.agent
        agent.cancel.clear()
        if self._pending_note:
            agent.messages.append({"role": "user", "content": self._pending_note})
            self._pending_note = None
        note = self._plans_note()
        if note:
            agent.messages.append({"role": "user", "content": note})
        self._add_message(agent, text, origin, attachments)
        retrieved = self.built.retrieved_message(text) if origin == "user" else None
        agent.untrusted_since_user = agent.untrusted_since_user or bool(retrieved)
        if retrieved:
            agent.messages.append(retrieved)
        self._save()                                   # the message is kept before the model sees it
        task = Task(turn, text, agent)
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

    def send(self, turn_id: str, text: str, origin: str = "user", attachments: Optional[List[str]] = None) -> bool:
        with self._lock:
            busy = self._workers > 0
            if busy and origin != "user":
                return False                           # a note from the planner never interrupts a task: it waits for the next quiet moment
            if not busy:
                self.blocked = None
                self._workers += 1
                self.busy, self.turn_id = True, turn_id
        if busy:                                       # a task is under way: the message is looked at before its next model call
            msg: Dict[str, Any] = {"turn": turn_id, "text": text, **({"attachments": attachments} if attachments else {})}
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
        task = self._begin_main(turn_id, text, origin, attachments)
        self.emit({"event": "accepted", "turn": turn_id} if origin == "user" else {"event": "woke", "turn": turn_id, "text": text[:300]})
        threading.Thread(target=self._work, args=(task,), name="session-task", daemon=True).start()
        return True

    def _add_message(self, agent: Any, text: str, origin: str, attachments: Optional[List[str]], steer: bool = False) -> None:
        """Puts the person's message in the conversation: with files attached, they come first as data in blocks of their own and the
        person's words after; what the person can confirm with is only their words."""
        if origin == "user" and attachments and self.attachment_store is not None:
            from harness.attachments import compose_message
            full, inlined = compose_message(self.attachment_store, self.session_id, attachments, text, self.window_tokens())
            agent.add_user_message(full, steer=steer, shown=text)
            agent.untrusted_since_user = True               # a file can say anything: nothing confirms a change to a plan after it
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

    def idle(self) -> bool:
        """Nothing under way, nothing waiting, nobody asked: a quiet moment."""
        with self._lock:
            return (self._workers == 0 and not self._waiting_n and not self.blocked and not (self.queue or self.inbox or self.urgent or self.stack))

    def poll_plans(self) -> Optional[str]:
        """Looks at the plans in a quiet moment and, for what is new and needs the person (a change proposed by the planner, a
        step it cannot do, a step that failed), starts a turn that tells them. Each thing is announced once."""
        if self.planner is None or not self.idle():
            return None
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
            fresh = [(k, t) for k, t in news if k not in self._announced]
            if fresh:
                key, text = fresh[0]
                self._announced.add(key)
                self._save()
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
            self.agent.messages.append({"role": "user", "content": self._pending_note})
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

    def _safe_point(self, task: Task) -> Optional[str]:
        """Between two model calls nothing is half done: the messages that came meanwhile are routed here, and the task is put
        aside if something urgent is waiting."""
        verdict = None
        while True:
            with self._lock:
                msg = self.inbox.pop(0) if self.inbox else None
            if msg is None:
                break
            how, why = self.router.route(msg["text"], self._context_of(task))
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

    def _run_task(self, task: Task) -> str:
        """Runs one task to where it stops, with new tries while the model is only busy. Returns "done", "suspended" or "blocked"."""
        agent = task.agent
        attempt = 0
        self._tl.task = task
        self.turn_id = task.turn
        task.interrupt = False
        agent.cancel.clear()
        self.emit({"event": "started", "turn": task.turn, "side": task.side})
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
            status = agent.error_status
            if status is None or status not in TRANSIENT_STATUS:
                self._block(task, "refused" if status else "error", agent.error)
                return "blocked"
            if attempt >= len(self.waits):
                self._block(task, "model_unavailable", f"{agent.error} (after {attempt} new tries)")
                return "blocked"
            health = self.health()
            if health["reachable"] and not health["loaded"] and not health["loading"] and not health["auto_load"]:
                self._block(task, "model_not_loaded", "the model is not loaded and the server will not load it by itself")
                return "blocked"
            wait = self.waits[attempt]
            attempt += 1
            self.emit({"event": "retrying", "turn": task.turn, "attempt": attempt, "of": len(self.waits), "wait_s": wait,
                       "status": status, "error": agent.error, "reachable": health["reachable"], "loading": health["loading"]})
            if not self._wait(wait, agent.cancel):
                agent.stop_reason = al.CANCELLED
                return "done"

    def _more(self) -> bool:
        return bool(self.stack or self.queue or self.urgent or self.inbox)

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
                final = next((t.assistant_content for t in reversed(agent.turns) if not t.tool_calls and t.assistant_content), "")
                self._save()
                self.emit({"event": "done", "turn": task.turn, "stop_reason": agent.stop_reason, "error": agent.error, "final": final,
                           "tokens": agent.total_tokens, "more": self._more() and not agent.cancel.is_set()})
                self.tasks.pop(task.turn, None)
                task = self._next_after(task)
        except BaseException as exc:                       # the process stays up: the task ends with an error
            self._block(task, "crash", f"{type(exc).__name__}: {exc}")
        finally:
            self._tl.task = None
            with self._lock:
                self._workers -= 1
                self.busy = self._workers > 0

    def _next_after(self, finished: Optional[Task]) -> Optional[Task]:
        """What to do when a task ends: an urgent request first, then the task that was put aside, then what waited its turn."""
        if finished is not None and finished.agent.cancel.is_set():
            with self._lock:
                self.stack, self.queue, self.urgent, self.inbox = [], [], [], []      # Stop means stop
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
                back.agent.messages.append({"role": "user", "content": RESUME_NOTE})
                back.agent.cancel.clear()
                self.emit({"event": "resumed", "turn": back.turn})
                return back
            elif self.queue:
                msg = self.queue.pop(0)
                side = False
            else:
                return None
        if side:
            return self._begin_side(msg["turn"], msg["text"], msg.get("attachments"))
        self.emit({"event": "accepted", "turn": msg["turn"]})
        return self._begin_main(msg["turn"], msg["text"], "user", msg.get("attachments"))

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
    parser.add_argument("--payments-dir", default="", help="the person's payment systems and limits: the agent may ask to pay and the broker decides")
    parser.add_argument("--browser", action="store_true", help="offer the browser tools: Edge or Chrome on this PC, for forms, purchases and bookings")
    parser.add_argument("--browser-headless", action="store_true", help="the browser has no window")
    parser.add_argument("--browser-private", action="store_true", help="the browser may open pages on this PC and its network")
    parser.add_argument("--browser-profile", default="", help="a profile folder that keeps the browser's sign-ins between conversations")
    parser.add_argument("--vault-dir", default="", help="the person's vault: the model writes {{vault:ENTRY.FIELD}} and the value is filled in for it")
    parser.add_argument("--skills", action="append", default=[], metavar="DIR")
    parser.add_argument("--ask-server", action="store_true", help="ask for approvals on the server's pages instead of in the conversation")
    parser.add_argument("--ask-only", action="store_true", help="answer without tools: a greeting does not become a loop")
    parser.add_argument("--no-planner", action="store_true", help="do not offer the planner's tools")
    parser.add_argument("--capabilities", action="store_true", help="offer the search for skills, plugins and MCP servers found on this PC, and the visual analysis")
    parser.add_argument("--router", choices=["model", "rules"], default="model",
                        help="how a message that comes during a task is judged when its words do not decide: by the model, or not at all")
    parser.add_argument("--pipeline", choices=["off", "light", "full"], default="full",
                        help="the check of an answer against what the request asked: nothing, measured only, or measured and judged")
    parser.add_argument("--context-patterns", default="", metavar="JSON", help="what shapes the context (harness/context_patterns.py)")
    parser.add_argument("--context-threshold", type=float, default=0.7, help="the share of the context window at which pruning or a summary starts")
    parser.add_argument("--end-turn-bias", type=float, default=0.0, help="0 or less: how much less ready the model is to end its turn (0 is off)")
    parser.add_argument("--attachments-dir", default="", help="where the conversation's attached files are kept (with --outputs-dir: the tools for them)")
    parser.add_argument("--outputs-dir", default="", help="where the files the agent makes for the person are kept")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--lsp", action="store_true", help="offer diagnostics, go_to_definition and find_references when a language server is found")
    parser.add_argument("--no-checkpoints", action="store_true")
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
    from harness.headless import split_secret_specs
    secret_names, secret_hosts = split_secret_specs(args.secret_env)
    sid = args.session_id or uuid.uuid4().hex[:12]
    confirm = None
    if args.ask_server:
        from harness.approval_client import ServerApprover
        confirm = ServerApprover(args.endpoint, args.api_key, sid)
    history = Path(args.history_dir) if args.history_dir else data_dir_for(Path(args.workspace).resolve()) / "sessions" / sid
    cfg = AgentConfig(workspace=args.workspace, endpoint=args.endpoint, api_key=args.api_key, max_turns=args.max_turns,
                      permission_mode=args.permission_mode, allow=args.allow, deny=args.deny, secret_names=secret_names,
                      secret_hosts=secret_hosts, use_memory=not args.no_memory, use_checkpoints=not args.no_checkpoints,
                      skill_paths=args.skills, confirm=confirm, run_id=sid,
                      context_patterns=json.loads(args.context_patterns) if args.context_patterns else {}, context_threshold=args.context_threshold, end_turn_bias=args.end_turn_bias, snapshot_dir=str(history / "snapshots"),
                      attachments_dir=args.attachments_dir or None, outputs_dir=args.outputs_dir or None,
                      capabilities=args.capabilities and not args.ask_only, lsp=args.lsp and not args.ask_only, council=not args.ask_only,
                      vault_dir="" if args.ask_only else args.vault_dir,
                      payments_dir="" if args.ask_only else args.payments_dir, browser=args.browser and not args.ask_only,
                      browser_headless=args.browser_headless, browser_private=args.browser_private, browser_profile=args.browser_profile)
    from harness.client import BasaltClient
    from harness.llm import client_for, judge_for
    from harness.llm.judge import router_judge
    here = BasaltClient(base_url=cfg.endpoint, api_key=cfg.api_key)          # the model loaded in this Basalt
    try:
        built = build_agent(cfg, client=client_for("executor", here), summarizer=client_for("compaction", here))
    except Exception as exc:
        emit({"event": "error", "code": "start", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    if args.ask_only:
        from harness.tools.registry import ToolRegistry
        built.agent.tools = ToolRegistry()
    store = SessionStore(history, redact=built.agent.secrets.mask_secrets if built.agent.secrets else None)
    router = Router(router_judge(judge_for("router", here)) if args.router == "model" else None)
    if hasattr(built.agent.context, "on_removed"):
        built.agent.context.on_removed = store.archive
    built.agent.pipeline = Pipeline(args.pipeline, (lambda q, o, c, j=judge_for("pipeline", here): j.choose(q, o, c)) if args.pipeline == "full" else None)
    session = Session(built, store, emit, session_id=sid, health=lambda: server_health(cfg.endpoint, cfg.api_key), router=router)
    if args.attachments_dir:
        from harness.attachments import AttachmentStore
        session.attachment_store = AttachmentStore(Path(args.attachments_dir))
    session.endpoint, session.api_key = cfg.endpoint, cfg.api_key
    if built.agent.secrets is not None:
        built.agent.secrets.ask_code = session.ask_code
    if built.payment_confirm is not None:
        built.payment_confirm.ask = session.ask_payment
    if not args.ask_server:
        built.permissions.confirm_callback = session.permission_confirm      # the questions are put in the conversation
    if not args.ask_only and not args.no_planner:
        from harness.integrations.planner_client import PlannerClient
        session.enable_planner(PlannerClient(cfg.endpoint, cfg.api_key))
    restored = session.restore()
    emit({"event": "ready", "session": sid, "restored": restored, "pid": os.getpid(), "blocked": session.blocked})

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(args.heartbeat):
            emit({"event": "heartbeat", "busy": session.busy, "turn": session.turn_id, "asking": session.waiting,
                  "waiting_requests": len(session.queue) + len(session.inbox) + len(session.urgent), "put_aside": len(session.stack),
                  "idle_s": session.idle_seconds()})
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
                ids = [i for i in cmd.get("attachments", []) if isinstance(i, str)][:20] if isinstance(cmd.get("attachments"), list) else []
                session.send(str(cmd.get("id") or uuid.uuid4().hex[:8]), cmd["text"], attachments=ids or None)
            elif name == "answer":
                session.answer(cmd.get("id"), cmd.get("value"))
            elif name == "promote":
                session.promote(str(cmd.get("id")))
            elif name == "drop":
                session.drop(str(cmd.get("id")))
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
