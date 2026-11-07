"""The Chat's conversations with the agent: one long-lived process each, watched and started again when needed.

Each conversation is `python -m tadamoo session`, spoken to in lines of JSON (see tadamoo/session.py). This keeps the
list of conversations, starts and stops the processes, buffers their events for the page, and supervises them:

* a process that dies or stops giving signs of life is started again from its saved history, and the turn it was in goes
  on (the model is told the call in hand may have run); a message it never confirmed is sent again; the waits between
  restarts grow up to `RESTART_WAITS[-1]` and never end the retries: only the person stops them;
* a conversation idle for `idle_s` has its process closed (it is dormant: the history stays, the next message wakes it);
* a confirmation the process asks (a change to a plan, a risky action) is kept with its buttons until the person answers; the
  conversation is `asking` meanwhile and a turn waiting for the person is never taken for a stuck one;
* a question that only the person can answer, in an `unattended` conversation, does not stop it: it is kept in `pending` for the
  person to answer whenever they come back, and the process goes on with other work;
* when the process says it is blocked (a refusal that waiting cannot change), the conversation waits for the person.
"""
from __future__ import annotations

import collections
import copy
import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from datetime import datetime

from tadamoo import context_patterns as context_patterns_mod
from tadamoo import questions as qs
from tadamoo.personal_skills.store import PersonalSkills, SkillError
from tadamoo.personal_skills.values import resolve as resolve_values
from tadamoo.vault import valid_code
from serve.coding_prefs import project_key
from tadamoo.roles import GENERAL as GENERAL_ROLE, ROLE_IDS, valid as valid_role
from tadamoo.worktrees import WorktreeError, Worktrees
from serve import ROOT
from serve.api import logit_bias
from serve.agent_runs import MODES, RunBusy, RunError
from serve.approvals import ApprovalBoard
from serve.conv_nodes import NodeStore
from serve.deliveries import DeliveryError, DeliveryQueue
from serve.session_budget import SessionBudget
from serve.tasks import _write_json
from serve.util import peer
from serve.util.winjob import contain

EVENTS_KEPT = 3000
SESSIONS_FILE = "agent_sessions.json"
TITLE_CHARS = 80
PLACE_KEEP_S = 86_400           # a position older than this is no longer handed to a conversation
OPTIONS = ("resume", "close")
RESTART_WAITS = (0.0, 2.0, 5.0, 15.0, 30.0, 60.0, 120.0, 180.0, 300.0)       # before each new start of a process that keeps dying; the last repeats
RESTART_RESET_S = 600.0                                                     # restarts older than this no longer lengthen the wait
ASK_FIELDS = ("id", "mode", "kind", "question", "options", "facts", "tool", "arguments", "free", "free_prefix", "explain", "intro", "questions", "notes", "source", "offer")
COMPARES_FILE = "compares.json"
NODES_FILE = "nodes.jsonl"
NODES_MIGRATED = "nodes.migrated"
MERGE_BRANCH_CHARS = 10_000                                                 # how much of each branch the model reads when two are merged
MERGE_SHARED_CHARS = 3_000
MERGE_PART_CHARS = 1_500
MERGE_TOKENS = 1_500
COMPARE_SETTINGS = ("mode", "max_turns", "ask_only", "end_turn_bias")      # what the two sides of a comparison may differ in
EFFECTS_FILE = "tool_effects.json"                                          # the effect class of each tool this conversation has used
SNIPPET_CHARS = 80
LEVELS = ("", "very_low", "low", "normal", "high", "very_high", "highest")     # how much of pictures or diagrams the person wants; empty is none
EFFORTS = ("", "none", "low", "medium", "high")                             # how long the model thinks; empty is its own default
SLASH_COMMANDS = ("status", "tools", "hooks", "mcp", "memory", "permissions", "context", "compact", "audit", "mcp-reload", "skills-reload")     # tadamoo/commands.py
FILE_DIFF_BYTES = 200_000
ALIAS = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
MAX_SPAWN_DEPTH = 2
MAX_SPAWN_CHILDREN = 3
RULES_TEXT_MAX = 20_000
SENDER_PATTERN = re.compile(r"^[A-Za-z0-9_*?-]{1,40}$")
SENDERS_MAX = 10
TOKEN_HEADER = "X-Tadamoo-Session"
UNLIMITED_STEPS = -1                                                        # `max_turns` for no limit on the steps of an answer


def check_steps(value: Any) -> int:
    """The most model steps one answer may take: -1 for no limit, or 1 and more."""
    if not isinstance(value, int) or isinstance(value, bool) or not (value == UNLIMITED_STEPS or value >= 1):
        raise RunError("The number of steps is -1 for no limit, or a whole number from 1.", "bad_steps")
    return value


def check_end_turn_bias(value: Any) -> float:
    """The end-of-turn bias: a number from the API's minimum to 0 (empty and 0 are off)."""
    try:
        logit_bias.add_end_turn({"end_turn_bias": value}, [0])
    except ValueError as e:
        raise RunError(str(e), "bad_policy") from None
    return float(value or 0)


class Live:
    """What exists only while a process runs."""

    def __init__(self, proc: "subprocess.Popen[str]") -> None:
        self.proc = proc
        self.wlock = threading.Lock()
        self.ready = threading.Event()
        self.pid = 0                                # the agent's own process id, as it reported it (a launcher may stand between)
        self.last_seen = time.monotonic()           # any line from the process
        self.ending = False                         # the manager is ending it on purpose
        self.after_ready: List[Dict[str, Any]] = []   # commands to give it as soon as it says ready


class SessionManager:
    def __init__(self, directory: str | Path, endpoint: str, api_key: str = "", board: Optional[ApprovalBoard] = None,
                 python: Optional[str] = None, root: Path = ROOT, command: Optional[Callable[[str, Dict[str, Any], Path], List[str]]] = None,
                 idle_s: Optional[float] = None, beat_timeout_s: Optional[float] = None,
                 restart_waits: Tuple[float, ...] = RESTART_WAITS, tick_s: float = 1.0, start_wait_s: float = 60.0,
                 default_workspace: Optional[Path] = None, router: str = "model", pipeline: str = "full",
                 attachments_dir: Optional[Path] = None, outputs_dir: Optional[Path] = None, worktrees: Optional[Worktrees] = None,
                 vault_dir: Optional[Path] = None, payments_dir: Optional[Path] = None,
                 browser_prefs: Optional[Callable[[], Dict[str, bool]]] = None, browser_profile: Optional[Path] = None,
                 plan_effect: Optional[Callable[[str, str, str, str, str], Any]] = None, compare_wait_s: Optional[float] = None,
                 skills_dir: Optional[Path] = None, projects: Any = None, personal_dir: Optional[Path] = None, personal_keys: Optional[Path] = None,
                 learn: Optional[Callable[[Dict[str, Any]], Any]] = None, knowledge_flags: Optional[Callable[[], List[str]]] = None,
                 rule_of_two: Optional[Callable[[], str]] = None, allow_rules: Any = None, rule_of_two_prefs: Any = None,
                 intake_flags: Optional[Callable[[], List[str]]] = None,
                 gate_flags: Optional[Callable[[], List[str]]] = None, coding: Any = None,
                 compaction_flags: Optional[Callable[[], List[str]]] = None, past: Any = None, exec_level_file: Optional[Path] = None, persistence_file: Optional[Path] = None, lessons_dir: Optional[Path] = None, next_steps_file: Optional[Path] = None, file_owners_path: Optional[Path] = None,
                 forgetter: Any = None, cards: Any = None) -> None:
        self.directory = Path(directory) / "sessions"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.endpoint, self.api_key, self.board = endpoint, api_key, board
        self.router = router                                    # how a message that comes during a task is judged: "model" or "rules"
        self.attachments_dir, self.outputs_dir = attachments_dir, outputs_dir      # the conversation's attached files and the files the agent made
        self.skills = PersonalSkills(skills_dir) if skills_dir else None            # the person's skills, which a conversation can run
        self.projects = projects                                # the folders of the Agent tab, whose own registry lists the conversations held in them
        self.personal_dir, self.personal_keys = personal_dir, personal_keys     # the person's own memories and the key that opens them: each conversation takes in the notes that matter
        self.learn = learn                                      # told what the person wrote and what was searched for it, when a turn ends, to learn from
        self._said: Dict[str, Dict[str, Any]] = {}              # per conversation, until its turn ends: what the person wrote and what was searched
        self._no_learning: set = set()                          # conversations that are not the person's talking (a routine's)
        self.next_steps_file = next_steps_file                  # the choice for the card of next steps after a task: one file, read again by each conversation
        self.lessons_dir = lessons_dir                          # the global lessons: one folder; the project's own are in its .basalt folder
        self.persistence_file = persistence_file                # the choice for "work until done": one file, read again by each conversation when it changes
        self.exec_level_file = exec_level_file                  # how freely the code the model writes runs: one file, read again by each conversation when it changes
        self.file_owners_path = file_owners_path                # which conversation owns a shared file: one file, read again by each conversation on every write and edit
        self.past = past                                        # the index of earlier conversations (tadamoo/past.py); each conversation is added to it when a turn ends
        self.cards = cards                                      # told when a conversation's turn ended, to write its card once it is quiet (serve/card_worker.py)
        self.forgetter = forgetter                              # takes a conversation back out of the index and the memories (tadamoo/forget.py)
        self._routine_runs: Dict[str, str] = {}                 # a routine's conversation -> the routine's name, when the person switched its indexing on
        self._place: Optional[Dict[str, Any]] = None            # where the person's phone last said they were: held in memory only
        self._place_sent: Dict[str, float] = {}                 # per conversation process: the receipt time of the position it was given
        self.browser_profile = browser_profile                  # where the browser keeps its sign-ins when the person chose that
        self.browser_prefs = browser_prefs                      # whether the conversations have a browser, and whether it shows a window
        self.knowledge_flags = knowledge_flags                  # the options for the project notes, the wiki and the reference documents (Settings, Memories)
        self.intake_flags = intake_flags                        # the options for the look at a request, the questions and the suggestions (Settings, General)
        self.coding = coding                                    # what the person accepted for each folder (serve/coding_prefs.py)
        self.compaction_flags = compaction_flags                # the options for the summary of older steps inside a long turn (Settings, General)
        self.gate_flags = gate_flags                            # the options for the verify gate, the budget's last report and the model-error proposal (Settings, Gate)
        self.allow_rules = allow_rules                          # the rules the person allowed for every conversation (serve/allow_rules.py)
        self.rule_of_two_prefs = rule_of_two_prefs              # where "always" is written when the person stops the Rule of Two's question for good
        self.rule_of_two = rule_of_two                          # how strict the Rule of Two is for a conversation that starts now (tadamoo/rule_of_two.py)
        self.payments_dir = payments_dir                        # the person's payment systems and limits, for the agent's request_payment
        self.vault_dir = vault_dir                              # the person's vault: the conversation's process fills placeholders from it
        self.pipeline = pipeline                                # the check of an answer against its request: "off", "light" or "full"
        self.worktrees = worktrees or Worktrees(Path(directory) / "worktrees")      # a conversation's own copy of a git folder
        self.default_workspace = Path(default_workspace) if default_workspace else None     # where a conversation works when none is named
        self.python, self.root = python or sys.executable, Path(root)
        self.command = command or self._command
        self._limits = {"idle_s": idle_s, "beat_timeout_s": beat_timeout_s, "compare_wait_s": compare_wait_s}   # given: fixed (0: no limit); None: the person's limits.json
        self.restart_waits, self.tick_s, self.start_wait_s = restart_waits, tick_s, start_wait_s
        self._restart_token: Dict[str, object] = {}            # a restart waiting for its time; dropping the token cancels it
        self._lock = threading.RLock()
        self._live: Dict[str, Live] = {}
        self._events: Dict[str, Deque[Dict[str, Any]]] = {}
        self._seq: Dict[str, int] = {}
        self._restarts: Dict[str, List[float]] = {}
        self._unconfirmed: Dict[str, Dict[str, Any]] = {}      # the message sent and not yet accepted
        self.plan_effect = plan_effect                          # told what a conversation did while it worked on a plan's step (plan, step, tool, effect, what)
        self._step: Dict[str, Tuple[Any, Any]] = {}            # the plan step each conversation is working on, from its own tool calls
        self._calls: Dict[str, Dict[str, Tuple[str, Dict[str, Any], Any]]] = {}      # tool calls started and not yet ended
        self._learned: Dict[str, Dict[str, str]] = {}
        self._compare_lock = threading.Lock()                   # the two sides of a comparison, and two comparisons, run one after the other
        self.compares: List[Dict[str, Any]] = self._load_compares()
        self.records: List[Dict[str, Any]] = self._load()
        self.nodes = NodeStore(self.directory / NODES_FILE)         # every conversation's messages as shared immutable nodes
        self._migrate_nodes()
        self._token_key = secrets.token_bytes(32)               # signs the name a conversation's process presents when it calls the server
        self.budget = SessionBudget()                           # tokens spent today, per conversation (serve/session_budget.py)
        self._chains: Dict[str, List[str]] = {}                 # per conversation: the conversations the message that woke it came through
        self.deliveries = DeliveryQueue(Path(directory), self)  # messages from schedules, plans and other conversations, until they are taken
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._supervise, name="session-supervisor", daemon=True)
        self._thread.start()
        peer.add_provider(self.agent_pids)

    def agent_pids(self) -> List[int]:
        """The process ids of the agents running now: whatever a connection from below one of them runs is the agent's own code."""
        with self._lock:
            return [live.pid for live in self._live.values() if live.pid and live.proc.poll() is None]

    def _limit(self, name: str) -> Optional[float]:
        """A time limit in seconds, None for no limit: the value given to this manager, else the person's (serve/limits.py)."""
        given = self._limits.get(name)
        if given is None:
            from serve import limits
            return limits.get(name)
        return given if given > 0 else None

    @property
    def idle_s(self) -> Optional[float]:
        return self._limit("idle_s")

    @property
    def beat_timeout_s(self) -> Optional[float]:
        return self._limit("beat_timeout_s")

    @property
    def compare_wait_s(self) -> Optional[float]:
        return self._limit("compare_wait_s")

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

    def workspace_file(self, sid: str, path: Any) -> Tuple[Path, str]:
        """A file in the folder the conversation works in (its own copy when it has one), for the person to save: only a regular file inside that folder,
        never in the agent's own `.tadamoo` or in `.git`."""
        rec = self.get(sid)
        root = Path(rec.get("workdir") or rec["workspace"]).resolve()
        text = str(path or "").replace("\\", "/").strip("/")
        if not text or len(text) > 400 or "\0" in text:
            raise RunError("Name the file, with its path inside the folder.", "bad_path")
        target = (root / text).resolve()
        try:
            inside = target.relative_to(root)
        except ValueError:
            raise RunError("That file is not inside the folder this conversation works in.", "bad_path") from None
        if any(part in (".tadamoo", ".git") for part in inside.parts) or not target.is_file():
            raise RunError("That file is not there, or it is one the agent keeps for itself.", "no_file")
        return target, target.name

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return {"sessions": [dict(r) for r in sorted(self.records, key=lambda r: r.get("updated", 0), reverse=True)],
                    "default_workspace": str(self.default_workspace) if self.default_workspace else ""}

    def _registry(self, workspace: Any) -> None:
        """Tells the folder's own registry what conversations are held in it."""
        if self.projects is not None and workspace:
            with self._lock:
                rows = [dict(r) for r in self.records]
            self.projects.sync(workspace, rows)

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
            shown.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "name", "tool_call_id", "attachments", "ts", "raw", "opt", "explain", "skipped")})
        return {"messages": shown, "turns": data.get("turns", 0), "session": dict(rec)}

    def _read_history(self, sid: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads((self.history_dir(sid) / "history.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._history_from_nodes(sid)
        return data if isinstance(data, dict) else None

    def _history_from_nodes(self, sid: str) -> Optional[Dict[str, Any]]:
        """The history of a conversation that has no file of its own yet (a fork): its chain of nodes."""
        with self._lock:
            rec = next((r for r in self.records if r["id"] == sid), None)
        if not rec or not rec.get("node") or self.nodes.get(rec["node"]) is None:
            return None
        return {"session": sid, "turns": rec.get("turns", 0), "saved": time.time(), "messages": self.nodes.chain(rec["node"]), "pending": []}

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
            if m.get("role") == "user" and isinstance(text, str) and text.startswith("<documents>\n"):
                from tadamoo.attachments import split_documents
                words, names = split_documents(text)
                m = {**m, "content": words, "attachments": names}
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
        leaf = self.nodes.ingest(kept)
        return self._new_child(rec, sid, "(branch)", leaf, turn, {"session": sid, "turn": turn})

    def _new_child(self, rec: Dict[str, Any], sid: str, suffix: str, leaf: Optional[str], turns: int, branched_from: Dict[str, Any],
                   **more: Any) -> Dict[str, Any]:
        """A conversation with `rec`'s settings whose history is the chain ending at node `leaf`: no file is written until it runs."""
        new = secrets.token_hex(4)
        copy = None
        if rec.get("worktree"):
            try:
                copy = self.worktrees.create(new, rec["workspace"])
            except WorktreeError as e:
                raise RunError(f"The branch needs its own copy of the folder: {e}", "no_copy") from None
        child = {**{k: v for k, v in rec.items() if k not in ("worktree", "workdir", "rewinds", "compare", "merged_from")}, "id": new,
                 "title": ((rec.get("title") or "") + " " + suffix).strip()[:TITLE_CHARS], "branched_from": branched_from, "node": leaf,
                 "created": time.time(), "updated": time.time(), "state": "dormant", "blocked": None, "turns": turns, **more}
        if copy:
            child["worktree"], child["workdir"] = copy, copy["workdir"]
        with self._lock:
            self.records.append(child)
            self._events[new], self._seq[new] = collections.deque(maxlen=EVENTS_KEPT), 0
            self._save()
        return dict(child)

    def _split(self, sid: str, data: Dict[str, Any], turn: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """The messages up to the end of the person's `turn`-th request, and those after it."""
        kept: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        users = 0
        for msg in self._transcript(sid, data):
            if msg.get("role") == "user":
                users += 1
            if users > turn:
                dropped.append(msg)
            else:
                kept.append({k: v for k, v in msg.items() if k not in ("masked", "stripped")})
        if users < turn:
            raise RunError(f"That conversation has only {users} requests.", "bad_turn")
        return kept, dropped

    @staticmethod
    def _check_turn(turn: Any) -> None:
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            raise RunError("Say which request to go back to: a number from 1.", "bad_turn")

    def _tool_effects(self, sid: str) -> Dict[str, str]:
        """The effect class each tool declared when this conversation used it (see `_learn`)."""
        with self._lock:
            if sid not in self._learned:
                try:
                    found = json.loads((self.history_dir(sid) / EFFECTS_FILE).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    found = {}
                found = found if isinstance(found, dict) else {}
                self._learned[sid] = {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}
            return dict(self._learned[sid])

    def _learn(self, sid: str, name: str, effect: Any) -> None:
        if effect not in ("pure", "reversible", "irreversible") or self._tool_effects(sid).get(name) == effect:
            return
        with self._lock:
            self._learned[sid][name] = effect
            try:
                self.history_dir(sid).mkdir(parents=True, exist_ok=True)
                _write_json(self.history_dir(sid) / EFFECTS_FILE, self._learned[sid])
            except OSError:
                pass                                        # the class is then read from the known tools next time

    def back_impact(self, sid: str, turn: Any) -> Dict[str, Any]:
        """What going back to the end of request `turn` would take off the conversation, and which of the actions in it stay done: files
        changed in the conversation's copy can be put back from the Changes panel; a command, a call, a payment, a message sent, a tool
        of unknown effect or a file outside the copy cannot."""
        from tadamoo.rewind import session_impact
        self._check_turn(turn)
        with self._lock:
            rec = self._record(sid)
        data = self._read_history(sid)
        if data is None:
            raise RunError("That conversation has nothing to go back in.", "empty")
        _, dropped = self._split(sid, data, turn)
        found = session_impact(dropped, rec.get("workdir") or rec.get("workspace"), self._tool_effects(sid))
        return {"turn": turn, "removes": {"requests": sum(1 for m in dropped if m.get("role") == "user"), "messages": len(dropped)},
                "reversible": found["reversible"], "not_undone": found["not_undone"], "needs_yes": bool(found["not_undone"])}

    def go_back(self, sid: str, turn: Any) -> Dict[str, Any]:
        """The conversation goes back to the end of the person's `turn`-th request: what came after is taken off (a copy is kept in the
        conversation's folder). It is the conversation only: files the agent changed are put back from the Changes panel, not from here,
        and an action that cannot be undone stays done (it is listed in the `rewinds` mark). A run in progress is stopped by it."""
        self._check_turn(turn)
        with self._lock:
            rec = self._record(sid)
        impact = self.back_impact(sid, turn)
        with self._lock:
            live = self._live.get(sid)
            if live is not None:
                live.ending = True
                self._write(sid, live, {"cmd": "close"})
                self._set(rec, state="dormant", blocked=None)
        if live is not None:
            self._end(live)
        data = self._read_history(sid)
        if data is None:
            raise RunError("That conversation has nothing to go back in.", "empty")
        kept, _ = self._split(sid, data, turn)
        folder = self.history_dir(sid)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"before-going-back-{stamp}.json"
        (folder / backup).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for name in ("archive.jsonl", "inflight.json"):
            try:
                (folder / name).replace(folder / f"{name}.{stamp}") if name == "archive.jsonl" else (folder / name).unlink()
            except OSError:
                pass
        _write_json(folder / "history.json", {**{k: v for k, v in data.items() if k not in ("messages", "pending", "announced")},
                                              "turns": turn, "saved": time.time(), "messages": kept, "pending": []})
        with self._lock:
            now = self._record(sid)
            mark = {"turn": turn, "was": now.get("turns", 0), "at": time.time(), "backup": backup, "still_done": impact["not_undone"][:20]}
            self._set(now, state="dormant", blocked=None, question=None, ask=None, turns=turn, waiting_requests=0, put_aside=0,
                      rewinds=[*(now.get("rewinds") or []), mark][-50:])
        self._push(sid, {"event": "went_back", "turn": turn, "still_done": impact["not_undone"][:20]})
        return self.get(sid)

    # -- the tree of a conversation's branches -------------------------------------------------------------------

    def _request_text(self, sid: str, turn: int, cache: Dict[str, List[str]]) -> str:
        if sid not in cache:
            data = self._read_history(sid)
            cache[sid] = [" ".join(str(m.get("content") or "").split()) for m in (self._transcript(sid, data) if data else []) if m.get("role") == "user"]
        asked = cache[sid]
        return asked[turn - 1][:SNIPPET_CHARS] if 0 < turn <= len(asked) else ""

    def tree(self) -> Dict[str, Any]:
        """Every conversation placed under the one it was branched from, at the request it was branched after. A conversation that went
        back keeps a mark of where. Conversations whose parent is gone are roots of their own."""
        with self._lock:
            records = [dict(r) for r in self.records]
        known = {r["id"] for r in records}
        children: Dict[Any, List[Dict[str, Any]]] = {}
        roots: List[Dict[str, Any]] = []
        for r in sorted(records, key=lambda r: (r.get("created") or 0)):
            parent = (r.get("branched_from") or {}).get("session")
            if parent in known and parent != r["id"]:
                children.setdefault(parent, []).append(r)
            else:
                roots.append(r)
        cache: Dict[str, List[str]] = {}
        seen: set = set()

        def build(r: Dict[str, Any]) -> Dict[str, Any]:
            seen.add(r["id"])
            origin = r.get("branched_from") or {}
            turn = origin.get("turn") if isinstance(origin.get("turn"), int) else 0
            mine = sorted(children.get(r["id"], []), key=lambda c: ((c.get("branched_from") or {}).get("turn") or 0, c.get("created") or 0))
            return {"id": r["id"], "title": r.get("title") or r["id"], "state": r.get("state"), "turns": r.get("turns", 0), "created": r.get("created"),
                    "updated": r.get("updated"), "workspace": r.get("workspace"), "copy": bool(r.get("worktree")),
                    "branched_from": {"session": origin["session"], "turn": turn, "request": self._request_text(origin["session"], turn, cache)} if origin.get("session") else None,
                    "rewinds": [{**{k: m.get(k) for k in ("turn", "was", "at")}, "still_done": len(m.get("still_done") or [])} for m in r.get("rewinds") or []][-10:],
                    "compare": r.get("compare"), "children": [build(c) for c in mine if c["id"] not in seen]}

        return {"roots": [build(r) for r in sorted(roots, key=lambda r: -(r.get("updated") or 0))], "count": len(records)}

    # -- the messages as nodes: where a conversation stands, what changing to it costs, merging two ---------------------

    def sync_node(self, sid: str) -> Optional[str]:
        """Puts the conversation's messages in the node store (those it shares with others are found, not added) and notes its leaf."""
        with self._lock:
            rec = self._record(sid)
        data = self._read_history(sid)
        if data is None:
            return rec.get("node")
        leaf = self.nodes.ingest({k: v for k, v in m.items() if k not in ("masked", "stripped")} for m in self._transcript(sid, data))
        if leaf != rec.get("node"):
            with self._lock:
                rec["node"] = leaf
                self._save()
        return leaf

    def _migrate_nodes(self) -> None:
        """Once: every conversation that exists is put in the store, and a branch's file that holds nothing the store does not is dropped
        (it is rebuilt from the nodes when needed). The ids come from the content, so the result does not depend on the order."""
        marker = self.directory / NODES_MIGRATED
        if marker.exists():
            return
        try:
            for rec in sorted(self.records, key=lambda r: (r.get("created") or 0, r["id"])):
                leaf = self.sync_node(rec["id"])
                if rec.get("branched_from") and rec.get("state") in ("dormant", "closed"):
                    self._drop_copy(rec, leaf)
            marker.write_text("1", encoding="utf-8")
        except (OSError, KeyError, ValueError):
            pass                                                # tried again at the next start

    def _drop_copy(self, rec: Dict[str, Any], leaf: Optional[str]) -> None:
        folder = self.history_dir(rec["id"])
        data = self._read_history(rec["id"])
        if not leaf or data is None or not (folder / "history.json").exists() or (folder / "archive.jsonl").exists() or (folder / "inflight.json").exists():
            return
        extra = set(data) - {"session", "turns", "saved", "messages", "pending"}
        plain = [{k: v for k, v in m.items() if k not in ("masked", "stripped")} for m in data.get("messages", []) if isinstance(m, dict)]
        if not extra and not data.get("pending") and data.get("turns") == rec.get("turns") and plain == self.nodes.chain(leaf):
            (folder / "history.json").unlink(missing_ok=True)

    def switch_cost(self, sid: str, current: Any = None) -> Dict[str, Any]:
        """What changing to conversation `sid` would ask of the model: the tokens of its history that have to be read again if the server no
        longer holds them, and how many of those the conversation `current` (the one last run) shares with it, which the server probably does."""
        leaf = self.sync_node(sid)
        other = None
        if current:
            with self._lock:
                self._record(str(current))
            other = self.sync_node(str(current))
        total = self.nodes.tokens(leaf)
        shared = self.nodes.tokens(self.nodes.common_ancestor(leaf, other)) if other else 0
        return {"session": sid, "messages": len(self.nodes.path_to(leaf)), "tokens": total, "shared_with_current": shared,
                "if_cache_cold": total, "if_cache_warm": total - shared, "excludes": "system prompt and tool list"}

    @staticmethod
    def _digest(messages: List[Dict[str, Any]], limit: int) -> str:
        lines = []
        for m in messages:
            text = " ".join(str(m.get("content") or "").split())
            if m.get("role") in ("user", "assistant") and text:
                lines.append(f"{'Person' if m['role'] == 'user' else 'Assistant'}: {text[:MERGE_PART_CHARS]}")
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                names = ", ".join(str((c.get("function") or {}).get("name") or "?") for c in m["tool_calls"] if isinstance(c, dict))
                lines.append(f"Assistant used: {names}")
        out = "\n".join(lines)
        return out if len(out) <= limit else "...\n" + out[-limit:]

    def merge(self, sid_a: Any, sid_b: Any, instruction: Any = "") -> Dict[str, Any]:
        """A new conversation that continues the first and ends with a synthesis the model writes, at the person's request, of what the two
        branches did after the point where they parted. Both are read, neither is changed."""
        from tadamoo.client import TadamooError, extract_reasoning_and_content
        if not isinstance(sid_a, str) or not isinstance(sid_b, str) or sid_a == sid_b:
            raise RunError("Choose two different conversations to merge.", "bad_merge")
        text = str(instruction or "").strip()
        recs = []
        for sid in (sid_a, sid_b):
            with self._lock:
                rec = self._record(sid)
                if rec["state"] in ("busy", "starting", "asking"):
                    raise RunError("Wait for the agent to finish, or stop it, before merging.", "busy")
                recs.append(dict(rec))
        leaves = [self.sync_node(sid_a), self.sync_node(sid_b)]
        if not all(leaves):
            raise RunError("Both conversations need some messages to be merged.", "empty")
        base = self.nodes.common_ancestor(*leaves)
        shared = len(self.nodes.path_to(base))
        branches = [self.nodes.chain(leaf)[shared:] for leaf in leaves]
        if not any(branches):
            raise RunError("The two conversations end at the same message: there is nothing to merge.", "bad_merge")
        titles = [r.get("title") or r["id"] for r in recs]
        system = ("Two branches of one conversation continued from the same point. Write one answer that combines what both found, says where they "
                  "disagree and which is better supported, and what to do next. Use the language of the conversation. Add nothing that is in neither branch.")
        asked = ("The person asks: " + text) if text else "The person asks for the two branches to be merged and summarized."
        user = (f"The conversation up to where the branches part:\n{self._digest(self.nodes.chain(base), MERGE_SHARED_CHARS) or '(nothing)'}\n\n"
                f"Branch A, \"{titles[0]}\":\n{self._digest(branches[0], MERGE_BRANCH_CHARS) or '(nothing after that point)'}\n\n"
                f"Branch B, \"{titles[1]}\":\n{self._digest(branches[1], MERGE_BRANCH_CHARS) or '(nothing after that point)'}\n\n{asked}")
        try:
            resp = self._skill_client().chat_completion(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                                                        temperature=0.2, max_tokens=MERGE_TOKENS, extra={"reasoning_effort": "none"}, timeout=600.0)
        except TadamooError as exc:
            raise RunError(f"The model could not be reached: {exc}", "model") from None
        _, answer = extract_reasoning_and_content((resp.get("choices") or [{}])[0].get("message") or {})
        if not answer:
            raise RunError("The model gave no synthesis.", "model")
        ask = text or f"Merge the branches \"{titles[0]}\" and \"{titles[1]}\" into one."
        ask_node = self.nodes.add({"role": "user", "content": ask, "ts": time.time()}, leaves)
        leaf = self.nodes.add({"role": "assistant", "content": answer, "ts": time.time()}, [ask_node])
        turns = sum(1 for m in self.nodes.chain(leaf) if m.get("role") == "user")
        return self._new_child(recs[0], sid_a, "(merge)", leaf, turns, {"session": sid_a, "turn": recs[0].get("turns", 0)}, merged_from=[sid_a, sid_b])

    def node_tree(self) -> Dict[str, Any]:
        """Every message node, in runs: messages that nothing branches from are one entry, and a fork, a merge or a conversation's current
        leaf ends a run. Runs no conversation reaches any more (what going back took off) are shown too, with no conversation."""
        with self._lock:
            ids = [r["id"] for r in self.records]
        for sid in ids:
            try:
                self.sync_node(sid)
            except (RunError, OSError):
                pass
        with self._lock:
            records = [dict(r) for r in self.records]
        nodes = {n["id"]: n for n in self.nodes.all()}
        kids: Dict[str, List[str]] = {}
        roots: List[str] = []
        for n in nodes.values():
            (kids.setdefault(n["parents"][0], []) if n["parents"] and n["parents"][0] in nodes else roots).append(n["id"])
        at: Dict[str, List[Dict[str, Any]]] = {}
        for r in records:
            if r.get("node") in nodes:
                at.setdefault(r["node"], []).append({"id": r["id"], "title": r.get("title") or r["id"], "state": r.get("state"), "turns": r.get("turns", 0)})
        merges = {n["id"]: n["parents"][1:] for n in nodes.values() if len(n["parents"]) > 1}

        def request(nid: str) -> str:
            m = nodes[nid]["message"]
            return " ".join(str(m.get("content") or "").split())[:SNIPPET_CHARS] if m.get("role") == "user" else ""

        def run(start: str) -> Dict[str, Any]:
            run_ids, cur = [start], start
            while len(kids.get(cur, [])) == 1 and cur not in at and kids[cur][0] not in merges:
                cur = kids[cur][0]
                run_ids.append(cur)
            return {"id": start, "last": cur, "messages": len(run_ids), "request": next((request(i) for i in run_ids if request(i)), ""),
                    "sessions": at.get(cur, []), "merges": [{"node": m, "sessions": at.get(m, [])} for m in merges.get(start, [])],
                    "children": [run(c) for c in kids.get(cur, [])]}

        return {"nodes": len(nodes), "roots": [run(r) for r in roots]}

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

    # -- two continuations from one point, one after the other ---------------------------------------------------

    def _load_compares(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads((self.directory / COMPARES_FILE).read_text(encoding="utf-8"))
            items = data.get("compares") if isinstance(data, dict) else None
        except (OSError, ValueError):
            items = None
        out = [c for c in (items or []) if isinstance(c, dict) and c.get("id") and isinstance(c.get("sides"), list)]
        for c in out:
            if c.get("state") == "running":                      # the thread that ran it did not survive the server
                c["state"] = "interrupted"
                for side in c["sides"]:
                    if side.get("state") in ("waiting", "running"):
                        side["state"] = "interrupted"
        return out

    def _save_compares(self) -> None:
        _write_json(self.directory / COMPARES_FILE, {"schema": "1.0", "compares": self.compares})

    def _compare(self, cid: str) -> Dict[str, Any]:
        found = next((c for c in self.compares if c["id"] == cid), None)
        if found is None:
            raise RunError("That comparison is not in the list any more.", "gone")
        return found

    def compare_start(self, sid: str, turn: Any, variants: Any) -> Dict[str, Any]:
        """Two continuations from the end of request `turn`: each is a branch that gets its own request (and, if it names them, its own
        mode, number of steps, question-only flag or end-of-turn bias). They run one after the other, not together: there is one GPU."""
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            raise RunError("Say which request to continue from: a number from 1.", "bad_turn")
        if not isinstance(variants, list) or len(variants) != 2 or any(not isinstance(v, dict) for v in variants):
            raise RunError("Give exactly two continuations to compare.", "bad_variants")
        sides = []
        for v in variants:
            text = v.get("text")
            if not isinstance(text, str) or not text.strip():
                raise RunError("Each continuation needs a message.", "text_missing")
            settings = {k: v[k] for k in COMPARE_SETTINGS if k in v}
            if settings.get("mode") not in (None, *MODES):
                raise RunError("Say a mode that exists: " + ", ".join(MODES) + ".", "bad_mode")
            if "max_turns" in settings:
                check_steps(settings["max_turns"])
            if "ask_only" in settings and not isinstance(settings["ask_only"], bool):
                raise RunError("Question-only is true or false.", "bad_policy")
            if "end_turn_bias" in settings:
                try:
                    logit_bias.add_end_turn({"end_turn_bias": settings["end_turn_bias"]}, [0])
                except ValueError as e:
                    raise RunError(str(e), "bad_policy") from None
            sides.append({"label": str(v.get("label") or "")[:40], "text": text, "settings": settings, "session": None, "state": "waiting"})
        with self._lock:
            rec = self._record(sid)
            if rec["state"] in ("busy", "starting", "asking"):
                raise RunError("Wait for the agent to finish, or stop it, before comparing.", "busy")
        data = self._read_history(sid)
        if data is None:
            raise RunError("That conversation has nothing to continue from.", "empty")
        self._split(sid, data, turn)                                  # the point must exist
        cid = secrets.token_hex(4)
        entry = {"id": cid, "from": {"session": sid, "turn": turn}, "created": time.time(), "state": "running", "sides": sides}
        with self._lock:
            self.compares.append(entry)
            self._save_compares()
        threading.Thread(target=self._run_compare, args=(cid,), name=f"session-compare-{cid}", daemon=True).start()
        return copy.deepcopy(entry)

    def _tag_side(self, child_id: str, cid: str, index: int, side: Dict[str, Any], base_title: str) -> None:
        with self._lock:
            self._set(self._record(child_id), compare={"id": cid, "side": "ab"[index]}, title=f"{base_title} ({'AB'[index]})".strip()[:TITLE_CHARS],
                      **{k: v for k, v in side["settings"].items()})

    def _wait_side(self, sid: str) -> str:
        wait = self.compare_wait_s                                  # None: as long as the side works
        end = time.monotonic() + wait if wait else float("inf")
        while not self._stop.is_set() and time.monotonic() < end:
            state = self.get(sid)["state"]
            if state == "idle":
                return "done"
            if state in ("blocked", "asking"):
                return "needs_you"                                  # the person answers it from the branch; the next side does not wait for that
            if state in ("closed", "dormant"):
                return "ended"
            time.sleep(0.1)
        try:
            self.cancel(sid)
        except RunError:
            pass
        return "timeout"

    def _run_compare(self, cid: str) -> None:
        with self._compare_lock:
            with self._lock:
                entry = self._compare(cid)
                origin = dict(entry["from"])
                base_title = self._record(origin["session"]).get("title") or ""
            for index in range(2):
                with self._lock:
                    side = entry["sides"][index]
                    side["state"] = "running"
                    self._save_compares()
                try:
                    child = self.branch(origin["session"], origin["turn"])
                    self._tag_side(child["id"], cid, index, side, base_title)
                    with self._lock:
                        side["session"] = child["id"]
                        self._save_compares()
                    self.send(child["id"], side["text"])
                    outcome = self._wait_side(child["id"])
                    error = ""
                except RunError as exc:
                    outcome, error = "failed", str(exc)
                with self._lock:
                    side["state"] = outcome
                    if error:
                        side["error"] = error
                    self._save_compares()
            with self._lock:
                entry["state"] = "done"
                self._save_compares()

    def compare_list(self) -> Dict[str, Any]:
        with self._lock:
            return {"compares": [{k: c[k] for k in ("id", "from", "created", "state")} | {"sides": [{k: s.get(k) for k in ("label", "session", "state")} for s in c["sides"]]}
                                 for c in sorted(self.compares, key=lambda c: -c["created"])]}

    def _answer_of(self, sid: str) -> str:
        data = self._read_history(sid)
        for m in reversed(self._transcript(sid, data) if data else []):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
                return m["content"]
        return ""

    def _side_files(self, sid: Optional[str]) -> Optional[Dict[str, str]]:
        """{path: status} of what the side changed in its copy of the folder; None when it has no copy (or no branch yet)."""
        if not sid:
            return None
        with self._lock:
            info = self._record(sid).get("worktree")
        if not info:
            return None
        try:
            self.worktrees.ensure(info)
            return {c["path"]: c["status"] for c in self.worktrees.changes(info)}
        except WorktreeError:
            return None

    def _file_text(self, sid: str, path: str) -> List[str]:
        with self._lock:
            info = self._record(sid).get("worktree")
        target = Path(info["dir"]) / path
        try:
            raw = target.read_bytes()[:FILE_DIFF_BYTES]
        except OSError:
            return []
        return [] if b"\0" in raw else raw.decode("utf-8", errors="replace").splitlines()

    def compare_result(self, cid: str) -> Dict[str, Any]:
        """Both sides' answers next to each other, a diff of the two answers and, when both worked in a copy of the folder, which files each
        changed and a diff of each file between the two."""
        with self._lock:
            entry = copy.deepcopy(self._compare(cid))
        sides = []
        for side in entry["sides"]:
            sid = side.get("session")
            sides.append({**{k: side.get(k) for k in ("label", "text", "settings", "state", "error", "session")},
                          "answer": self._answer_of(sid) if sid else "", "files": self._side_files(sid)})
        a, b = sides
        answers = list(difflib.unified_diff(a["answer"].splitlines(), b["answer"].splitlines(), "A", "B", lineterm="", n=2))
        files = None
        if a["files"] is not None and b["files"] is not None:
            files = []
            for path in sorted(set(a["files"]) | set(b["files"])):
                if path.startswith("-") or ".." in Path(path).parts:
                    continue
                before, after = self._file_text(a["session"], path), self._file_text(b["session"], path)
                diff = list(difflib.unified_diff(before, after, f"A/{path}", f"B/{path}", lineterm="", n=2))
                files.append({"path": path, "a": a["files"].get(path), "b": b["files"].get(path), "same": not diff and a["files"].get(path) == b["files"].get(path),
                              "diff": "\n".join(diff)[:60_000]})
        return {"id": cid, "from": entry["from"], "state": entry["state"], "sides": sides, "answers_diff": "\n".join(answers), "files": files}

    # -- what the process says while it works: the effects it learned and the plan step it is on -----------------

    def _watch(self, sid: str, event: Dict[str, Any]) -> None:
        data = event.get("data") if event.get("event") == "agent" else None
        if not isinstance(data, dict):
            return
        kind, name, call_id = data.get("type"), data.get("name"), data.get("call_id")
        if kind == "tool_start" and isinstance(name, str):
            args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
            if name in ("search_web",) and isinstance(args.get("query"), str):
                with self._lock:
                    heard = self._said.get(sid)
                    if heard is not None and len(heard["searches"]) < 10:
                        heard["searches"].append(args["query"])
            self._learn(sid, name, data.get("effect"))
            calls = self._calls.setdefault(sid, {})
            if len(calls) > 50:
                calls.pop(next(iter(calls)))
            calls[call_id] = (name, data.get("arguments") if isinstance(data.get("arguments"), dict) else {}, data.get("effect"))
        elif kind == "tool_end":
            call = self._calls.get(sid, {}).pop(call_id, None)
            if call is None or data.get("status") != "completed":
                return
            tool, args, effect = call
            step = self._step.get(sid)
            if tool == "plan_step_start" and " started" in str(data.get("output") or "")[:300]:
                self._step[sid] = (args.get("plan_id"), args.get("node_id"))
            elif tool == "plan_mark":
                if step == (args.get("plan_id"), args.get("node_id")) and args.get("status") in ("completed", "failed", "skipped", "timed_out"):
                    self._step.pop(sid, None)
            elif step and effect in ("reversible", "irreversible") and not tool.startswith("plan_") and self.plan_effect is not None:
                from tadamoo.rewind import describe_call
                try:
                    self.plan_effect(step[0], step[1], tool, effect, describe_call(tool, args))
                except Exception:
                    pass                                          # the plan not taking the note never stops the conversation

    # -- creating ----------------------------------------------------------------------------------------

    def create(self, workspace: Any, mode: Any = "default", allow: Any = None, ask_only: bool = False, title: Any = "",
               max_turns: Any = UNLIMITED_STEPS, context_patterns: Any = None,
               context_policy: Any = "stop", context_threshold: Any = 0.8, end_turn_bias: Any = None, worktree: Any = "auto", effort: Any = "",
               images: Any = "", charts: Any = "", unattended: bool = False, role: Any = GENERAL_ROLE, private: bool = False,
               alias: Any = "", cross_commands: Any = None, parent: Any = "", operating_rules: Any = None,
               budget_day_tokens: Any = None) -> Dict[str, Any]:
        from serve.agent_runs import AgentRunner
        if budget_day_tokens is not None and (not isinstance(budget_day_tokens, int) or isinstance(budget_day_tokens, bool) or budget_day_tokens < 1):
            raise RunError("The day's budget is a whole number of tokens from 1, or empty for no limit.", "bad_budget")
        depth = 0
        if parent:
            parent_rec = next((r for r in self.records if r["id"] == parent), None)
            if parent_rec is None:
                raise RunError("The conversation that asked for this one is not there any more.", "gone")
            depth = int(parent_rec.get("depth", 0)) + 1
            if depth > MAX_SPAWN_DEPTH:
                raise RunError(f"A conversation started this way may not itself start one more than {MAX_SPAWN_DEPTH} deep.", "too_deep")
            if unattended:
                live = sum(1 for r in self.records if r.get("parent") == parent and r.get("unattended") and r["state"] != "closed")
                if live >= MAX_SPAWN_CHILDREN:
                    raise RunError(f"This conversation already has {MAX_SPAWN_CHILDREN} background conversations of its own running; close one first.", "too_many_children")
        if (workspace is None or workspace == "") and self.default_workspace is not None:
            self.default_workspace.mkdir(parents=True, exist_ok=True)
            workspace = str(self.default_workspace)
        path = AgentRunner.check_workspace(workspace)
        if mode not in MODES:
            raise RunError("The mode must be one of: " + ", ".join(MODES) + ".", "bad_mode")
        for name, level in (("images", images), ("charts", charts)):
            if level not in LEVELS:
                raise RunError(f"How much {name} are wanted is one of: off, " + ", ".join(LEVELS[1:]) + ".", "bad_visuals")
        if effort not in EFFORTS:
            raise RunError("The thinking level is none, low, medium or high; empty is the model's own.", "bad_effort")
        if not valid_role(role):
            raise RunError("The role must be one of: " + ", ".join(ROLE_IDS) + ".", "bad_role")
        rules = allow if isinstance(allow, list) else []
        if any(not isinstance(r, str) or not r.strip() or len(r) > 200 or "\n" in r for r in rules):
            raise RunError("Allow rules are lines of text.", "bad_rules")
        check_steps(max_turns)
        if context_policy not in ("stop", "rolling", "compact"):
            raise RunError("The policy for a full context is stop, rolling or compact.", "bad_policy")
        if isinstance(context_threshold, bool) or not isinstance(context_threshold, (int, float)) or not 0.3 <= context_threshold <= 1.0:
            raise RunError("The threshold is a share of the context window from 0.3 to 1.", "bad_policy")
        check_end_turn_bias(end_turn_bias)
        alias = self._check_alias(alias)
        senders = self._check_senders(cross_commands)
        patterns =context_patterns_mod.check_config(context_patterns) if context_patterns else {}
        if context_policy != "stop":
            patterns = context_patterns_mod.with_policy(context_patterns or {}, context_policy)
        if worktree is True:
            worktree = "always"
        if worktree in (False, None):
            worktree = "off"
        if worktree not in ("off", "auto", "always"):
            raise RunError("The copy of the folder is off, auto or always.", "bad_worktree")
        sid = secrets.token_hex(4)
        copy = self._copy_for(sid, path, worktree)
        rec = {"id": sid, "context_patterns": patterns, "context_threshold": float(context_threshold), "end_turn_bias": float(end_turn_bias or 0), "title": str(title or "").strip()[:TITLE_CHARS], "workspace": str(path), "mode": mode, "role": role, "effort": effort, "images": images, "charts": charts,
               "allow": [r.strip() for r in rules], "ask_only": bool(ask_only), "max_turns": max_turns, "unattended": unattended is True, "parent": parent or "", "depth": depth,
               "budget_day_tokens": budget_day_tokens,
               "created": time.time(), "updated": time.time(), "state": "dormant", "blocked": None, "turns": 0}
        if copy:
            rec["worktree"], rec["workdir"] = copy, copy["workdir"]
        if private is True:
            rec["private"] = True                                  # left out of the index of earlier conversations and of what is learned about the person
        if senders:
            rec["cross_commands"] = senders
        if operating_rules is not None:
            text = str(operating_rules).strip()[:RULES_TEXT_MAX]
            if text:
                path = self.history_dir(sid) / "rules.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                rec["rules_file"] = str(path)
        with self._lock:
            if alias:
                self._alias_free(alias, sid)
                rec["alias"] = alias
            self.records.append(rec)
            self._events[sid], self._seq[sid] = collections.deque(maxlen=EVENTS_KEPT), 0
            self._save()
        if self.projects is not None:
            self.projects.touch(rec["workspace"])
        self._registry(rec["workspace"])
        return dict(rec)

    def _copy_for(self, sid: str, path: Path, mode: str) -> Optional[Dict[str, Any]]:
        """Its own copy of the folder when it is asked for ("always") or when another open conversation already works in that folder
        ("auto"); None when the conversation works in the folder itself."""
        if mode == "off":
            return None
        if mode == "auto":
            with self._lock:
                crowded = any(r.get("workspace") == str(path) and r["state"] != "closed" and not r.get("worktree") for r in self.records)
            if not crowded:
                return None
        try:
            return self.worktrees.create(sid, path)
        except WorktreeError as e:
            if mode == "auto":
                return None                         # not a git folder: it works in place, and `send` keeps two from working at once
            raise RunError(str(e), "no_copy") from None

    # -- starting a process ------------------------------------------------------------------------------

    def _command(self, sid: str, rec: Dict[str, Any], history: Path) -> List[str]:
        cmd = [self.python, "-m", "tadamoo", "session", "--session-id", sid, "--history-dir", str(history), "--workspace", rec.get("workdir") or rec["workspace"],
               "--endpoint", self.endpoint, "--permission-mode", rec["mode"], "--max-turns", str(rec.get("max_turns", UNLIMITED_STEPS)), "--router", self.router, "--pipeline", self.pipeline, "--capabilities", "--lsp", "--report-changes", "--page-files"]
        if rec.get("unattended"):
            cmd.append("--unattended")
        for pattern in rec.get("cross_commands") or []:
            cmd += ["--cross-commands", pattern]
        if rec.get("rules_file"):
            cmd += ["--rules-file", rec["rules_file"]]
        if self.file_owners_path:
            cmd += ["--file-owners", str(self.file_owners_path)]
        if rec.get("orchestration") or rec.get("alias") or rec.get("unattended") or rec.get("parent"):
            cmd.append("--orchestration")
        if rec.get("role") and rec["role"] != GENERAL_ROLE and not rec.get("ask_only"):
            cmd += ["--role", rec["role"]]
        if self.coding is not None and not rec.get("ask_only"):
            cmd += self.coding.flags(rec["workspace"])
        rules = list(rec.get("allow", []))
        rules += [r for r in (self.allow_rules.read() if self.allow_rules else []) if r not in rules]
        for rule in rules:
            cmd += ["--allow", rule]
        if rec.get("effort"):
            cmd += ["--effort", rec["effort"]]
        for flag in ("images", "charts"):
            if rec.get(flag):
                cmd += [f"--{flag}", rec[flag]]
        if self.past is not None:
            cmd += ["--past-index", str(self.past.path)]
        if self.skills is not None:
            cmd += ["--personal-skills-dir", str(self.skills.dir)]
        if self.exec_level_file:
            cmd += ["--exec-level-file", str(self.exec_level_file)]
        if self.persistence_file:
            cmd += ["--persistence-file", str(self.persistence_file)]
        if self.lessons_dir:
            cmd += ["--lessons-dir", str(self.lessons_dir)]
        if self.next_steps_file:
            cmd += ["--next-steps-file", str(self.next_steps_file)]
        if self.personal_dir:
            cmd += ["--personal-memory", str(self.personal_dir)] + (["--personal-memory-keys", str(self.personal_keys)] if self.personal_keys else [])
        if rec.get("ask_only"):
            cmd.append("--ask-only")
        elif self.vault_dir:
            cmd += ["--vault-dir", str(self.vault_dir)]
        if not rec.get("ask_only") and self.payments_dir:
            cmd += ["--payments-dir", str(self.payments_dir)]
        if self.rule_of_two:
            cmd += ["--rule-of-two", rec.get("rule_of_two") or self.rule_of_two()]
        prefs = self.browser_prefs() if self.browser_prefs else {}
        if not rec.get("ask_only") and prefs.get("enabled"):
            cmd += ["--browser"] + (["--browser-headless"] if prefs.get("headless") else []) + (["--browser-private"] if prefs.get("private") else [])                    + (["--browser-profile", str(self.browser_profile)] if prefs.get("persistent") and self.browser_profile else [])
            cmd += ["--browser-consent", str(prefs.get("consent", "follow")), "--browser-permissions", str(prefs.get("permissions", "follow"))]
        if self.knowledge_flags:
            cmd += self.knowledge_flags()
        if self.intake_flags:
            cmd += self.intake_flags()
        if self.gate_flags and not rec.get("ask_only"):
            cmd += self.gate_flags()
        if self.compaction_flags:
            cmd += self.compaction_flags()
        if rec.get("context_patterns"):
            cmd += ["--context-patterns", json.dumps(rec["context_patterns"])]
        if rec.get("context_threshold"):
            cmd += ["--context-threshold", str(rec["context_threshold"])]
        if rec.get("end_turn_bias"):
            cmd += ["--end-turn-bias", str(rec["end_turn_bias"])]
        if self.attachments_dir and self.outputs_dir and not rec.get("ask_only"):
            cmd += ["--attachments-dir", str(self.attachments_dir), "--outputs-dir", str(self.outputs_dir)]
        elif self.attachments_dir:
            cmd += ["--attachments-dir", str(self.attachments_dir)]
        return cmd

    def _spawn(self, sid: str) -> Live:
        rec = self._record(sid)
        if rec.get("worktree"):
            try:
                self.worktrees.ensure(rec["worktree"])
            except WorktreeError as e:
                raise RunError(f"The copy of the folder for this conversation could not be brought back: {e}", "no_copy") from None
        history = self.history_dir(sid)
        history.mkdir(parents=True, exist_ok=True)
        if not (history / "history.json").exists():
            made = self._history_from_nodes(sid)                  # a fork gets its file when it first runs
            if made is not None:
                _write_json(history / "history.json", made)
        env = dict(os.environ)
        env.update({"PYTHONUTF8": "1", "PYTHONPATH": str(self.root), "TADAMOO_COMPRESS": "1"})
        if self.api_key:
            env["TADAMOO_API_KEY"] = self.api_key
        env["TADAMOO_SESSION_ID"], env["TADAMOO_SESSION_TOKEN"] = sid, self.session_token(sid)
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
        self._place_sent.pop(sid, None)                     # a new process holds no position yet
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
        if kind == "delivery":                                   # the queue's lock is never taken inside this manager's
            item = self.deliveries.acked(str(event.get("id") or ""), str(event.get("state") or ""), sid)
            if item is not None and item["state"] == "delivered":
                with self._lock:
                    self._chains[sid] = list(item["chain"])
            self._push(sid, {"event": "delivery", "id": event.get("id"), "state": event.get("state")})
            return
        with self._lock:
            rec = next((r for r in self.records if r["id"] == sid), None)
            if rec is None or self._live.get(sid) is not live:
                return
            if kind == "heartbeat":
                waiting, aside = int(event.get("waiting_requests") or 0), int(event.get("put_aside") or 0)
                if (rec.get("waiting_requests", 0), rec.get("put_aside", 0)) != (waiting, aside):
                    self._set(rec, waiting_requests=waiting, put_aside=aside)       # what waits behind the task in hand
                return
            if kind == "ready":
                live.pid = int(event.get("pid") or 0)
                blocked = event.get("blocked")
                pending, live.after_ready = live.after_ready, []
                self._set(rec, state="blocked" if blocked else "busy" if any(c.get("cmd") != "answer" for c in pending) else "idle", blocked=blocked,
                          pending=[self._pending_view(i) for i in event.get("parked") or [] if isinstance(i, dict)])
                live.ready.set()
                if not blocked:                              # a block is the person's to lift, so nothing is given on
                    for cmd in pending:
                        self._write(sid, live, cmd)
            elif kind in ("accepted", "woke"):                        # a message of the person, or a note of the planner
                self._unconfirmed.pop(sid, None)
                self._set(rec, state="busy", blocked=None, ask=None, question=None, suggestions=None)
            elif kind == "suggestions":
                self._set(rec, suggestions=event.get("items") if isinstance(event.get("items"), list) else None)
            elif kind == "ask":
                shown = {k: event.get(k) for k in ASK_FIELDS}
                if event.get("mode") == "confirm":
                    self._set(rec, state="asking", ask=shown)
                else:
                    self._set(rec, question=shown)
                    if shown.get("offer") and self.coding is not None:
                        self.coding.mark_offered(rec["workspace"])              # the card that offers the folder's own checks is shown once
            elif kind == "pending":                                   # a question for the person that does not stop the process
                shown = {k: event.get(k) for k in ASK_FIELDS if k != "mode"}
                self._set(rec, pending=[i for i in rec.get("pending", []) if i.get("id") != shown["id"]] + [{**shown, "source": event.get("source"), "turn": event.get("turn"), "at": event.get("t") or time.time()}])
            elif kind == "pending_done":
                self._set(rec, pending=[i for i in rec.get("pending", []) if i.get("id") != event.get("id")])
            elif kind == "rule_added":
                rule = str(event.get("rule") or "").strip()
                rules = list(rec.get("allow", []))
                if event.get("scope") == "global" and self.allow_rules is not None:
                    try:
                        self.allow_rules.add(rule)                   # "always": every conversation from now on
                    except ValueError:
                        pass
                elif rule and len(rule) <= 200 and "\n" not in rule and rule not in rules:
                    self._set(rec, allow=rules + [rule])             # a rule the person chose in the question stays for this conversation's next starts
            elif kind == "rule_of_two_off":
                if event.get("scope") == "global" and self.rule_of_two_prefs is not None:
                    self.rule_of_two_prefs.write("off")
                else:
                    self._set(rec, rule_of_two="off")
            elif kind == "queued":
                self._unconfirmed.pop(sid, None)                     # the process has the message; it will look at it before its next model call
            elif kind in ("started", "resumed"):
                if rec["state"] not in ("asking", "blocked"):
                    self._set(rec, state="busy")
            elif kind == "done":
                self.budget.record(sid, event.get("tokens"))
                if not event.get("more"):
                    threading.Thread(target=self._flush_heard, args=(sid,), name="learn-flush", daemon=True).start()
                    threading.Thread(target=self._index_and_card, args=(sid,), name="past-index", daemon=True).start()
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
        self._watch(sid, event)
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
            self._set(rec, state="starting")
            self._schedule_restart(sid, rec, why)

    def _schedule_restart(self, sid: str, rec: Dict[str, Any], why: str) -> None:
        """Starts the process again after a wait that grows with the restarts of the last minutes and never ends the retries; the
        person stops them by stopping or closing the conversation. Called with the lock held."""
        now = time.monotonic()
        recent = [t for t in self._restarts.get(sid, []) if now - t < RESTART_RESET_S]
        self._restarts[sid] = recent + [now]
        wait = self.restart_waits[min(len(recent), len(self.restart_waits) - 1)]
        token = object()
        self._restart_token[sid] = token
        self._push(sid, {"event": "restarting", "why": why, "attempt": len(recent) + 1, "wait_s": wait})
        threading.Thread(target=self._restart_after, args=(sid, token, wait, why), name=f"restart-{sid}", daemon=True).start()

    def _restart_after(self, sid: str, token: object, wait: float, why: str) -> None:
        if wait > 0 and self._stop.wait(wait):
            return
        with self._lock:
            if self._restart_token.get(sid) is not token or self._stop.is_set():
                return                                                  # stopped, closed, or started by a new message meanwhile
            self._restart_token.pop(sid, None)
            rec = next((r for r in self.records if r["id"] == sid), None)
            if rec is None or rec["state"] != "starting" or self._live.get(sid) is not None:
                return
            unconfirmed = self._unconfirmed.get(sid)
            try:
                live = self._spawn(sid)
            except RunError as exc:                                     # it could not even start: try again later, the same way
                self._schedule_restart(sid, rec, str(exc))
                return
            live.after_ready = [dict(unconfirmed)] if unconfirmed else [{"cmd": "resume"}]

    def _stop_restart(self, sid: str) -> bool:
        """The person stopped a conversation that was waiting to be started again."""
        with self._lock:
            return self._restart_token.pop(sid, None) is not None

    @staticmethod
    def _pending_view(item: Dict[str, Any]) -> Dict[str, Any]:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        return {**{k: payload.get(k) for k in ASK_FIELDS if k != "mode"}, "id": item.get("id"), "source": item.get("source"),
                "turn": item.get("turn"), "at": item.get("at")}

    def _write(self, sid: str, live: Live, cmd: Dict[str, Any]) -> bool:
        try:
            with live.wlock:
                live.proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
                live.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    # -- naming a conversation, and messages between conversations -------------------------------------------

    @staticmethod
    def _check_alias(alias: Any) -> str:
        text = str(alias or "").strip().lower()
        if not text:
            return ""
        if not ALIAS.match(text) or re.fullmatch(r"[0-9a-f]{8}", text):
            raise RunError("A name is 2 to 32 letters, digits or dashes, and is not an id.", "bad_alias")
        return text

    def _alias_free(self, alias: str, sid: str) -> None:
        if any(r.get("alias") == alias and r["id"] != sid and r["state"] != "closed" for r in self.records):
            raise RunError(f"Another conversation is already called '{alias}'.", "alias_taken")

    @staticmethod
    def _check_senders(patterns: Any) -> List[str]:
        if patterns is None:
            return []
        if not isinstance(patterns, list) or len(patterns) > SENDERS_MAX or any(not isinstance(p, str) or not SENDER_PATTERN.match(p) for p in patterns):
            raise RunError(f"The conversations that may give orders are a short list (at most {SENDERS_MAX}) of ids, names or patterns with * and ?.", "bad_senders")
        return sorted(set(patterns))

    def set_alias(self, sid: str, alias: Any) -> Dict[str, Any]:
        """Gives the conversation a name other conversations and schedules can use (empty takes it off)."""
        name = self._check_alias(alias)
        with self._lock:
            rec = self._record(sid)
            if name:
                self._alias_free(name, sid)
            self._set(rec, alias=name)
        return self.get(sid)

    def set_cross_commands(self, sid: str, patterns: Any) -> Dict[str, Any]:
        """Which conversations may give this one orders: what a conversation sends it is data unless the sender is named here."""
        senders = self._check_senders(patterns if patterns is not None else [])
        with self._lock:
            rec = self._record(sid)
            self._set(rec, cross_commands=senders)
            self._tell_if_running(sid, {"cmd": "cross_commands", "patterns": senders})
        return self.get(sid)

    def set_budget(self, sid: str, budget_day_tokens: Any) -> Dict[str, Any]:
        """How many tokens this conversation may use today; a delivery to it is refused once it has spent that much (empty: no limit)."""
        if budget_day_tokens is not None and (not isinstance(budget_day_tokens, int) or isinstance(budget_day_tokens, bool) or budget_day_tokens < 1):
            raise RunError("The day's budget is a whole number of tokens from 1, or empty for no limit.", "bad_budget")
        with self._lock:
            rec = self._record(sid)
            self._set(rec, budget_day_tokens=budget_day_tokens)
        return self.get(sid)

    def resolve(self, ref: Any) -> Optional[str]:
        """The id a reference names: an id, or the name of a conversation that is not closed."""
        text = str(ref or "").strip().lower()
        if not text:
            return None
        with self._lock:
            if any(r["id"] == text for r in self.records):
                return text
            return next((r["id"] for r in self.records if r.get("alias") == text and r["state"] != "closed"), None)

    def state_of(self, sid: str) -> Optional[str]:
        with self._lock:
            rec = next((r for r in self.records if r["id"] == sid), None)
            return rec["state"] if rec else None

    def over_budget(self, sid: str) -> bool:
        rec = next((r for r in self.records if r["id"] == sid), None)
        return rec is not None and self.budget.over(sid, rec.get("budget_day_tokens"))

    def label_of(self, sid: str) -> str:
        """How a conversation is named in a message it sends: its name, else its id."""
        with self._lock:
            rec = next((r for r in self.records if r["id"] == sid), None)
        return str((rec or {}).get("alias") or sid)

    def session_token(self, sid: str) -> str:
        return hmac.new(self._token_key, sid.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def sender_of(self, header: Any) -> Optional[str]:
        """The conversation a process says it is (`<id>.<token>`), when the token is the one this server gave it; None otherwise."""
        sid, _, token = str(header or "").partition(".")
        if not sid or not token or self.resolve(sid) != sid:
            return None
        return sid if hmac.compare_digest(token, self.session_token(sid)) else None

    def push_delivery(self, sid: str, delivery: Dict[str, Any]) -> bool:
        """Writes a delivered message to the process of a conversation, starting it if it sleeps. It is a turn with its provenance, not the person's."""
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                return False
            live = self._ensure(sid)
            turn = secrets.token_hex(3)
            cmd = {"cmd": "send", "id": turn, "text": delivery["message"], "provenance": delivery["provenance"], "delivery": delivery["id"]}
            if not live.ready.is_set():
                live.after_ready.append(cmd)
            elif not self._write(sid, live, cmd):
                return False
        self._push(sid, {"event": "incoming", "turn": turn, "text": delivery["message"], "provenance": delivery["provenance"], "delivery": delivery["id"]})
        return True

    def deliver(self, ref: Any, message: Any, sender: str = "", provenance: str = "", run_id: str = "") -> Dict[str, Any]:
        """A message for another conversation. From a conversation (`sender`) it is `agent:<its name>` and carries the line of conversations
        it came through; from a schedule or a plan the caller says which (`provenance`)."""
        if sender:
            with self._lock:
                chain = list(self._chains.get(sender, [])) + [sender]
            provenance = f"agent:{self.label_of(sender)}"
        else:
            chain = []
        try:
            return {"delivery": self.deliveries.submit(ref, message, provenance, chain, run_id, sender)}
        except DeliveryError as exc:
            raise RunError(str(exc), exc.code) from None

    def retry_delivery(self, delivery_id: str) -> Dict[str, Any]:
        try:
            return self.deliveries.retry(delivery_id)
        except DeliveryError as exc:
            raise RunError(str(exc), exc.code) from None

    def dismiss_delivery(self, delivery_id: str) -> Dict[str, Any]:
        try:
            return self.deliveries.dismiss(delivery_id)
        except DeliveryError as exc:
            raise RunError(str(exc), exc.code) from None

    def status(self, ref: Any) -> Dict[str, Any]:
        """What another conversation needs to know about this one: who and where it is, whether it is free, and what is stuck waiting for it."""
        sid = self.resolve(ref)
        if sid is None:
            raise RunError(f"No conversation is called '{ref}'.", "gone")
        rec = self.get(sid)
        with self._lock:
            last = next((e for e in reversed(self._events.get(sid, ())) if e.get("event") == "done" and e.get("final")), None)
        return {"id": sid, "alias": rec.get("alias", ""), "title": rec.get("title", ""), "state": rec["state"], "workspace": rec["workspace"],
                "unattended": bool(rec.get("unattended")), "turns": rec.get("turns", 0), "updated": rec.get("updated", 0),
                "pending_messages": self.deliveries.pending_count(sid), "dead_letters": self.deliveries.dead_count(sid),
                "last_answer": str(last.get("final") or "")[:300] if last else "",
                "budget_day_tokens": rec.get("budget_day_tokens"), "used_today": self.budget.used_today(sid)}

    def digest(self, ref: Any, n: Any = 20) -> Dict[str, Any]:
        """The last `n` things that happened to or from this conversation: its own answers, messages it took in, and messages it sent
        that are still pending or could not be delivered; oldest first. For an idea of what an unattended run did without reading its
        whole history."""
        sid = self.resolve(ref)
        if sid is None:
            raise RunError(f"No conversation is called '{ref}'.", "gone")
        try:
            count = max(1, min(int(n), 100))
        except (TypeError, ValueError):
            raise RunError("The count is a number.", "bad_limit") from None
        rec = self.get(sid)
        rows: List[Tuple[float, str]] = []
        with self._lock:
            events = list(self._events.get(sid, ()))
        for e in events:
            when = float(e.get("t") or 0)
            if e.get("event") == "done" and e.get("final"):
                rows.append((when, f"done: {str(e['final'])[:160]}"))
            elif e.get("event") == "incoming":
                rows.append((when, f"from {e.get('provenance', '')}: {str(e.get('text', ''))[:160]}"))
            elif e.get("event") == "user":
                rows.append((when, f"you: {str(e.get('text', ''))[:160]}"))
        for d in self.deliveries.entries():
            when = float(d.get("created") or 0)
            if d.get("sender") == sid:                                   # a message this conversation sent: only it has this record
                rows.append((when, f"sent to {d.get('target', '')}: {d.get('state')}" + (f" — {d.get('reason')}" if d.get("state") == "dead" else "")))
            elif d.get("session") == sid and d.get("state") in ("pending", "dead"):
                # delivered ones are already an "incoming" event above; a pending or dead one never reaches it, so it is shown here instead
                rows.append((when, f"from {d.get('provenance', '')}: {'waiting to be delivered' if d['state'] == 'pending' else 'not delivered — ' + d.get('reason', '')}"))
        rows.sort(key=lambda r: r[0])
        lines = [f"{time.strftime('%H:%M', time.localtime(t))} {text}" for t, text in rows[-count:]]
        return {"id": sid, "alias": rec.get("alias", ""), "lines": lines, "pending_messages": self.deliveries.pending_count(sid),
                "dead_letters": self.deliveries.dead_count(sid)}

    # -- the page's commands -----------------------------------------------------------------------------

    def _check_attachments(self, sid: str, ids: Any) -> List[Dict[str, Any]]:
        """The cards of the files a message names; each must be attached to this conversation."""
        if not ids:
            return []
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or self.attachments_dir is None:
            raise RunError("Attach files that were uploaded to this conversation.", "bad_attachments")
        from tadamoo.attachments import AttachmentStore
        store = AttachmentStore(self.attachments_dir)
        try:
            return [store.get(sid, i) for i in ids]
        except KeyError:
            raise RunError("One of the attached files is not there any more: attach it again.", "bad_attachments") from None

    def forget_place(self) -> None:
        with self._lock:
            self._place = None
            self._place_sent.clear()

    def _place_for(self, sid: str, raw: Any) -> Optional[Dict[str, Any]]:
        """The position to hand to the conversation's process with this message: the one the phone just sent, or the last one it sent when the
        process does not hold it yet; None when there is nothing new to hand over. A position older than a day is not handed over."""
        from tadamoo.place import clean
        held = clean(raw)
        with self._lock:
            if held is not None:
                self._place = held
            held = self._place
            if held is None or time.time() - held["got"] + held["fix_age_s"] > PLACE_KEEP_S:
                return None
            if self._place_sent.get(sid) == held["got"]:
                return None
            self._place_sent[sid] = held["got"]
            return held

    def send(self, sid: str, text: Any, attachments: Any = None, place: Any = None, optimize: Any = None) -> Dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise RunError("Write a message.", "text_missing")
        cards = self._check_attachments(sid, attachments)
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            turn = secrets.token_hex(3)              # a message that comes while the agent works is looked at by the agent itself
            self._chains.pop(sid, None)              # the person is talking: whatever woke it before is no longer the line it works in
            live = self._ensure(sid)
            held = self._place_for(sid, place)
            cmd = {"cmd": "send", "id": turn, "text": text, **({"attachments": [c["id"] for c in cards]} if cards else {}), **({"place": held} if held else {}),
                   **({"optimize": False} if optimize is False else {})}      # "Send my original instead": this message is not rewritten
            self._unconfirmed[sid] = cmd
            if not rec.get("title"):
                rec["title"] = " ".join(text.split())[:TITLE_CHARS]
            keep = rec["state"] in ("busy", "asking") and live.ready.is_set()       # a task is under way: its state is not changed
            self._set(rec, blocked=None, question=None, suggestions=None, **({} if keep else {"state": "starting" if not live.ready.is_set() else "busy"}))
            if not live.ready.is_set():
                live.after_ready.append(cmd)
            elif not self._write(sid, live, cmd):
                raise RunError("The agent process did not take the message; try again.", "write_failed")
        self._push(sid, {"event": "user", "turn": turn, "text": text, **({"attachments": [{k: c[k] for k in ("id", "name", "family", "size")} for c in cards]} if cards else {})})
        self._hear(sid, rec, text)
        return {"turn": turn, "session": self.get(sid)}

    # -- what the person wrote, for their memories -------------------------------------------------------

    def keep_out_of_memory(self, sid: str) -> None:
        """A conversation that is not the person talking (a routine's): nothing in it is learned."""
        with self._lock:
            self._no_learning.add(sid)

    # -- the index of earlier conversations ------------------------------------------------------------

    def routine_run(self, sid: str, name: str, indexed: bool) -> None:
        """A routine's conversation: it goes into the index of earlier conversations only when the person switched that on for the routine."""
        with self._lock:
            if indexed:
                self._routine_runs[sid] = name

    def index_past(self, sid: str) -> bool:
        """Adds what was said in a conversation to the index of earlier conversations (replacing what the index had of it); false when it is left out or fails."""
        if self.past is None:
            return False
        try:
            with self._lock:
                rec = dict(self._record(sid))
                routine = self._routine_runs.get(sid, "")
                if (sid in self._no_learning or rec.get("unattended")) and not routine:
                    return False
                if rec.get("private"):
                    return False
            workspace = str(rec.get("workspace") or "")
            chat = not workspace or (self.default_workspace is not None and Path(workspace) == self.default_workspace)
            messages = self.history(sid)["messages"]
            self.past.add_session(sid, messages, title=rec.get("title") or "", project="" if chat else Path(workspace).name,
                                  kind="routine" if routine else "chat" if chat else "agent", routine=routine, at=rec.get("updated") or time.time())
            with self._lock:
                if self._record(sid).get("private"):          # marked private while it was being indexed
                    self.past.drop(sid)
                    return False
            return True
        except Exception:
            return False                                        # the index never gets in the way of the conversation

    def _index_and_card(self, sid: str) -> None:
        if self.index_past(sid) and self.cards is not None:
            try:
                self.cards.touch(sid)
            except Exception:
                pass                                          # the card is never the conversation's concern

    def index_all_past(self) -> int:
        """Adds the conversations saved before the index existed, or changed since, to the index; returns how many it indexed."""
        if self.past is None:
            return 0
        kept = {s["session"]: s["at"] for s in self.past.sessions()}
        with self._lock:
            todo = [r["id"] for r in self.records if (r.get("updated") or 0) > kept.get(r["id"], -1) + 1e-6 and r["id"] not in self._no_learning and not r.get("unattended") and not r.get("private")]
        return sum(1 for sid in todo if self.index_past(sid))

    def _hear(self, sid: str, rec: Dict[str, Any], text: str) -> None:
        if self.learn is None or sid in self._no_learning or rec.get("private"):
            return
        workspace = str(rec.get("workspace") or "")
        project = "" if not workspace or (self.default_workspace and Path(workspace) == self.default_workspace) else Path(workspace).name
        with self._lock:
            heard = self._said.setdefault(sid, {"texts": [], "searches": [], "project": project})
            if len(heard["texts"]) < 10:
                heard["texts"].append(text)

    def _flush_heard(self, sid: str) -> None:
        """The turn ended: what the person wrote in it, and what was searched for them, goes to be learned from when the GPU is free."""
        with self._lock:
            heard = self._said.pop(sid, None)
        if not heard or self.learn is None:
            return
        for n, text in enumerate(heard["texts"]):
            last = n == len(heard["texts"]) - 1
            try:
                self.learn({"text": text, "project": heard["project"], "conversation": sid, "searches": heard["searches"] if last else []})
            except Exception:
                pass                                          # learning never gets in the way of the conversation

    def _skill_source(self, sid: str, turns: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """The requests the person chose (by number, as the page counts them) and the steps taken to answer them."""
        from tadamoo.personal_skills import distill
        if not isinstance(turns, list) or not turns or len(turns) > distill.MAX_REQUESTS or any(not isinstance(t, int) or isinstance(t, bool) or t < 1 for t in turns):
            raise RunError(f"Choose the requests to make the skill from: from 1 to {distill.MAX_REQUESTS}.", "bad_turns")
        with self._lock:
            self._record(sid)
        data = self._read_history(sid)
        if data is None:
            raise RunError("That conversation has nothing to make a skill from.", "empty")
        messages = self._transcript(sid, data)
        every = distill.requests_of(messages)
        if max(turns) > len(every):
            raise RunError(f"That conversation has only {len(every)} requests.", "bad_turns")
        chosen = [r for r in every if r["n"] in set(turns)]
        steps = distill.steps_of(messages, every, turns)
        if not any(s["kind"] == "tool" for s in steps):
            raise RunError("Nothing was done in those requests that a skill could repeat: no tool was used.", "nothing_to_repeat")
        return chosen, steps

    def _skill_client(self) -> Any:
        from tadamoo.client import TadamooClient
        return TadamooClient(base_url=self.endpoint, api_key=self.api_key or None)

    def skill_propose(self, sid: str, turns: Any) -> Dict[str, Any]:
        """The first step of making a skill from requests: the values that probably change, named and explained."""
        from tadamoo.client import TadamooError
        from tadamoo.personal_skills import distill
        chosen, steps = self._skill_source(sid, turns)
        try:
            proposal = distill.propose(self._skill_client(), chosen, steps)
        except TadamooError as exc:
            raise RunError(f"The model could not be reached: {exc}", "model") from None
        return {**proposal, "requests": [{"n": r["n"], "raw": r["raw"]} for r in chosen],
                "steps": [{"kind": s["kind"], "turn": s["turn"], **({"name": s["name"], "ok": s["ok"]} if s["kind"] == "tool" else {})} for s in steps]}

    def routine_skill(self, sid: str, name: str) -> Dict[str, Any]:
        """A script that repeats what a routine's run did, written from its recorded steps with every value the model proposed kept as a
        parameter. The person reads it before anything changes; `ai_calls` counts the places left to the language model."""
        from tadamoo.client import TadamooError
        from tadamoo.personal_skills import distill
        chosen, steps = self._skill_source(sid, [1])
        try:
            proposal = distill.propose(self._skill_client(), chosen, steps)
            description = str(proposal.get("description") or name)[:500]
            params = proposal.get("params") or []
            written = distill.write_script(self._skill_client(), name[:60], description, params, chosen, steps)
        except TadamooError as exc:
            raise RunError(f"The model could not be reached: {exc}", "model") from None
        script = written.get("script") or ""
        return {"name": name[:60], "description": description, "params": params, "script": script, "problems": written.get("problems") or [],
                "tool_steps": sum(1 for st in steps if st["kind"] == "tool"), "ai_calls": len(re.findall(r"(?<![\w.])ai\(", script))}

    def skill_script(self, sid: str, turns: Any, name: Any, description: Any, params: Any) -> Dict[str, Any]:
        """The second step: the Python script, written from the requests, the recorded steps and the parameters the person kept."""
        from tadamoo.client import TadamooError
        from tadamoo.personal_skills import distill
        from tadamoo.personal_skills.values import check_spec
        if not isinstance(params, list) or len(params) > 20 or any(not isinstance(p, dict) for p in params):
            raise RunError("Send the parameters you kept.", "bad_params")
        for p in params:
            problems = check_spec(p)
            if problems:
                raise RunError("; ".join(problems), "bad_params")
        chosen, steps = self._skill_source(sid, turns)
        try:
            written = distill.write_script(self._skill_client(), str(name or "")[:60], str(description or "")[:500], params, chosen, steps)
        except TadamooError as exc:
            raise RunError(f"The model could not be reached: {exc}", "model") from None
        keep = ("name", "label", "explanation", "type", "default", "required", "choices", "item_type", "format")
        return {**written, "skill": {"name": str(name or "")[:60], "description": str(description or "")[:500],
                                     "params": [{k: p[k] for k in keep if k in p} for p in params], "source": {"conversation": sid, "requests": sorted(set(turns))}}}

    def run_skill(self, sid: str, slug: Any, values: Any = None) -> Dict[str, Any]:
        """Runs one of the person's skills in this conversation with the values they gave (a date in words is worked out now; a value left out is the
        default, else the last one used, else it is told to the model as missing). Nothing asks before it runs: its tool calls are
        decided by the conversation's own rules."""
        from serve.personal_skills_api import describe
        if self.skills is None or not isinstance(slug, str) or (values is not None and not isinstance(values, dict)):
            raise RunError("Choose a skill and give its values.", "bad_skill")
        try:
            skill = self.skills.get(slug)
        except SkillError as exc:
            raise RunError(str(exc), "no_skill") from None
        typed, errors = resolve_values(skill.get("params") or [], values or {}, datetime.now(), self.skills.last_values(slug))
        if errors:
            raise RunError("; ".join(f"{k}: {v}" for k, v in errors.items()), "bad_values")
        text = describe(skill, typed)
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            if rec["state"] in ("busy", "asking", "starting"):
                raise RunError("Wait for the agent to finish, or stop it, before running a skill.", "busy")
            turn = secrets.token_hex(3)
            live = self._ensure(sid)
            cmd = {"cmd": "skill", "id": turn, "name": skill["name"], "display": text, "script": skill["script"],
                   "values": typed, "timeout": skill.get("timeout", 1800), "cache_dir": str(self.skills.dir / slug / ".cache")}
            self._unconfirmed[sid] = cmd
            if not rec.get("title"):
                rec["title"] = " ".join(skill["name"].split())[:TITLE_CHARS]
            self._set(rec, blocked=None, question=None, state="starting" if not live.ready.is_set() else "busy")
            if not live.ready.is_set():
                live.after_ready.append(cmd)
            elif not self._write(sid, live, cmd):
                raise RunError("The agent process did not take the skill; try again.", "write_failed")
        self.skills.note_run(slug, typed)
        self._push(sid, {"event": "user", "turn": turn, "text": text})
        return {"turn": turn, "session": self.get(sid)}

    def _answer_card(self, sid: str, rec: Dict[str, Any], card: Dict[str, Any], value: Any) -> Optional[str]:
        """The answer to a card of questions (the lock is held), checked against the card this server stored, not against what the page says
        it showed. Returns the message the model reads, or None when the person chose to answer in the chat."""
        try:
            checked = qs.check_answer(card, qs.parse_answer(value))
        except qs.QuestionError as e:
            raise RunError(str(e), "bad_answer") from None
        if checked["status"] == "chat":
            self._set(rec, question=None)
            return None
        offer = card.get("offer")
        if checked["status"] == "submitted" and isinstance(offer, dict) and self.coding is not None:      # the checks the person ticked are accepted for this folder
            picked = (checked["answers"].get(offer.get("question")) or {}).get("picked") or []
            self.coding.settle_offer(rec["workspace"], [cmd for oid, cmd in (offer.get("commands") or {}).items() if oid in picked])
        return qs.format_answer(card, checked)

    def answer(self, sid: str, ask_id: Any, value: Any) -> Dict[str, Any]:
        """The person's answer to a confirmation: it must be one of the buttons of the question that is waiting. The answer to a card of
        questions is put in words here and sent as the person's next message."""
        text: Optional[str] = None
        with self._lock:
            rec = self._record(sid)
            card = rec.get("question")
            if isinstance(card, dict) and card.get("questions") and card.get("id") == ask_id and not rec.get("ask"):
                text = self._answer_card(sid, rec, card, value)
                if text is None:                                       # the person answers in the chat: their next message is the answer
                    self._push(sid, {"event": "answered", "ask": ask_id, "value": "chat"})
                    return self.get(sid)
                offered, workspace = isinstance(card.get("offer"), dict), rec["workspace"]
        if text is not None:
            if offered:
                self.update_checks(workspace)                          # what the person accepted reaches the conversations of this folder now
            self._push(sid, {"event": "answered", "ask": ask_id, "value": "answered"})
            return self.send(sid, text)["session"]
        with self._lock:
            rec = self._record(sid)
            ask = rec.get("ask")
            live = self._live.get(sid)
            parked = next((i for i in rec.get("pending", []) if i.get("id") == ask_id), None)
            if parked is not None:                                  # a question left for the person: answered whenever, the process applies it
                return self._answer_pending(sid, rec, live, parked, value)
            if not ask or live is None or ask.get("id") != ask_id:
                raise RunError("That question is not waiting for an answer any more.", "not_asking")
            free = bool(ask.get("free"))
            if free:
                if not valid_code(value):
                    raise RunError("A code is up to 32 letters, digits, spaces or dashes.", "bad_answer")
            elif value not in [o.get("value") for o in ask.get("options") or []] and not (        # one answer may be a sentence after a fixed prefix
                    ask.get("free_prefix") and isinstance(value, str) and value.startswith(ask["free_prefix"]) and len(value) <= len(ask["free_prefix"]) + 500):
                raise RunError("That is not one of the answers.", "bad_answer")
            self._set(rec, state="busy", ask=None)
            if not self._write(sid, live, {"cmd": "answer", "id": ask_id, "value": value}):
                raise RunError("The agent process did not take the answer; try again.", "write_failed")
        self._push(sid, {"event": "answered", "ask": ask_id, "value": "" if free else value})        # a code is not kept in the conversation
        return self.get(sid)

    def _answer_pending(self, sid: str, rec: Dict[str, Any], live: Optional[Live], item: Dict[str, Any], value: Any) -> Dict[str, Any]:
        """The answer to a pending question (the lock is held): checked like any other, then handed to the process, which starts first when
        the conversation is dormant. The conversation's state does not change here: the process says what it does with it."""
        if item.get("free"):
            if not valid_code(value):
                raise RunError("A code is up to 32 letters, digits, spaces or dashes.", "bad_answer")
        elif value not in [o.get("value") for o in item.get("options") or []] and not (
                item.get("free_prefix") and isinstance(value, str) and value.startswith(item["free_prefix"]) and len(value) <= len(item["free_prefix"]) + 500):
            raise RunError("That is not one of the answers.", "bad_answer")
        cmd = {"cmd": "answer", "id": item["id"], "value": value}
        if live is not None and live.ready.is_set() and live.proc.poll() is None and not live.ending:
            if not self._write(sid, live, cmd):
                raise RunError("The agent process did not take the answer; try again.", "write_failed")
        else:
            live = self._ensure(sid)
            self._set(rec, state="starting")
            live.after_ready = live.after_ready + [cmd]
        self._push(sid, {"event": "answered", "ask": item["id"], "value": "" if item.get("free") else value})
        return dict(rec)

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
            rec = self._record(sid)
            live = self._live.get(sid)
            if live is None:
                if self._restart_token.pop(sid, None) is not None:       # waiting to be started again: stop that
                    self._set(rec, state="dormant")
                    self._push(sid, {"event": "restart_stopped"})
                    return dict(rec)
                raise RunError("That conversation is not running.", "not_running")
            self._write(sid, live, {"cmd": "cancel"})
        return self.get(sid)

    def _tell_if_running(self, sid: str, cmd: Dict[str, Any]) -> bool:
        """Hands a setting to the conversation's process if there is one; a process started later takes it from the record."""
        live = self._live.get(sid)
        return bool(live is not None and live.ready.is_set() and live.proc.poll() is None and not live.ending and self._write(sid, live, cmd))

    def set_mode(self, sid: str, mode: Any) -> Dict[str, Any]:
        """Changes what the conversation may do without asking, from its next action on."""
        if mode not in MODES:
            raise RunError("The mode must be one of: " + ", ".join(MODES) + ".", "bad_mode")
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            if rec.get("ask_only"):
                raise RunError("A conversation without tools has no mode to change.", "bad_mode")
            self._set(rec, mode=mode)
            told = self._tell_if_running(sid, {"cmd": "mode", "mode": mode})
        if not told:
            self._push(sid, {"event": "mode", "mode": mode})
        return self.get(sid)

    def set_role(self, sid: str, role: Any) -> Dict[str, Any]:
        """Changes what the conversation's agent is for, from its next model call on. The tool list the model receives stays the same."""
        if not valid_role(role):
            raise RunError("The role must be one of: " + ", ".join(ROLE_IDS) + ".", "bad_role")
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            if rec.get("ask_only"):
                raise RunError("A conversation without tools has no role to change.", "bad_role")
            self._set(rec, role=role)
            told = self._tell_if_running(sid, {"cmd": "role", "role": role})
        if not told:
            self._push(sid, {"event": "role", "role": role})
        return self.get(sid)

    def update_checks(self, workspace: Any) -> int:
        """Tells the running conversations of a folder what the person accepted for it now. Returns how many were told."""
        if self.coding is None:
            return 0
        got = self.coding.effective(workspace)
        with self._lock:
            sids = [r["id"] for r in self.records if project_key(r.get("workspace", "")) == project_key(workspace) and r["state"] != "closed" and not r.get("ask_only")]
            return sum(1 for sid in sids if self._tell_if_running(sid, {"cmd": "checks", "safe": got["safe"], "checks": got["checks"]}))

    def update_intake(self, mode: Any) -> int:
        """Tells the running conversations how a request is looked at now; one started later takes it from the saved choice. Returns how many were told."""
        with self._lock:
            sids = [r["id"] for r in self.records if r["state"] != "closed"]
            return sum(1 for sid in sids if self._tell_if_running(sid, {"cmd": "intake", "mode": mode}))

    def update_optimize(self, on: bool) -> int:
        """Tells the running conversations whether Optimize prompt is on now; one started later takes it from the saved choice. Returns how many were told."""
        with self._lock:
            sids = [r["id"] for r in self.records if r["state"] != "closed"]
            return sum(1 for sid in sids if self._tell_if_running(sid, {"cmd": "optimize", "on": bool(on)}))

    def set_effort(self, sid: str, level: Any) -> Dict[str, Any]:
        """How long the model thinks in this conversation, from its next model call on."""
        if level not in EFFORTS:
            raise RunError("The thinking level is none, low, medium or high; empty is the model's own.", "bad_effort")
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            self._set(rec, effort=level)
            told = self._tell_if_running(sid, {"cmd": "effort", "level": level})
        if not told:
            self._push(sid, {"event": "effort", "level": level})
        return self.get(sid)

    def set_settings(self, sid: str, max_turns: Any = None, end_turn_bias: Any = None) -> Dict[str, Any]:
        """Changes the steps one answer may take and the end-of-turn bias of a conversation, from its next run on; what is not given stays."""
        changes: Dict[str, Any] = {}
        if max_turns is not None:
            changes["max_turns"] = check_steps(max_turns)
        if end_turn_bias is not None:
            changes["end_turn_bias"] = check_end_turn_bias(end_turn_bias)
        if not changes:
            raise RunError("Say the number of steps or the end-of-turn bias.", "bad_steps")
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            self._set(rec, **changes)
            told = self._tell_if_running(sid, {"cmd": "settings", **changes})
        if not told:
            self._push(sid, {"event": "settings", **changes})
        return self.get(sid)

    def rename(self, sid: str, title: Any) -> Dict[str, Any]:
        text = " ".join(str(title or "").split())[:TITLE_CHARS]
        if not text:
            raise RunError("Give the conversation a name.", "bad_title")
        with self._lock:
            rec = self._record(sid)
            self._set(rec, title=text)
        self._registry(rec.get("workspace"))
        return self.get(sid)

    def favorite(self, sid: str, value: Any) -> Dict[str, Any]:
        """Marks a conversation as one of the person's favourites (or not); the list shows those apart. Its place in time is not changed."""
        if not isinstance(value, bool):
            raise RunError("Say whether it is a favourite: true or false.", "bad_favorite")
        with self._lock:
            rec = self._record(sid)
            rec["favorite"] = value
            self._save()
        return self.get(sid)

    def set_private(self, sid: str, value: Any) -> Dict[str, Any]:
        """Marks a conversation as private (or not): it is then left out of the index of earlier conversations and nothing is learned from it for
        the person's memories. Turning it on also takes it out of the index and drops what waited to be learned; notes already made stay until it is forgotten."""
        if not isinstance(value, bool):
            raise RunError("Say whether it is private: true or false.", "bad_private")
        with self._lock:
            rec = self._record(sid)
            rec["private"] = value
            self._save()
            if value:
                self._said.pop(sid, None)
        if value:
            try:
                if self.forgetter is not None:
                    self.forgetter.keep_out(sid)
                elif self.past is not None:
                    self.past.drop(sid)
            except Exception:
                pass                                          # the mark holds either way: nothing new is taken
        else:
            threading.Thread(target=self.index_past, args=(sid,), name="past-index", daemon=True).start()
        return self.get(sid)

    def forget(self, sid: str) -> Dict[str, Any]:
        """Removes everything taken from a conversation: its pieces in the index of earlier conversations and the memories learned from it. The
        conversation itself stays; what is said in it later is taken again unless it is marked private."""
        with self._lock:
            self._record(sid)
            self._said.pop(sid, None)
        if self.forgetter is None:
            raise RunError("The memories are not available here.", "no_memory")
        return {"forgotten": self.forgetter.forget(sid)}

    def delete(self, sid: str) -> Dict[str, Any]:
        """Takes a conversation away for good: its process, its saved messages, the files attached to it and made in it, and its own copy
        of the folder with whatever was not applied. The branches made from it stay, as conversations of their own."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(sid)):
            raise RunError("That conversation is not in the list any more.", "gone")
        with self._lock:
            rec = self._record(sid)
            live = self._live.get(sid)
            self._restart_token.pop(sid, None)
            if live is not None:
                live.ending = True
                self._write(sid, live, {"cmd": "close"})
        if live is not None:
            self._end(live)
        if self.board:
            self.board.refuse_all(sid)
        if rec.get("worktree"):
            try:
                self.worktrees.discard(rec["worktree"])
            except (WorktreeError, OSError):
                pass                                           # what is left of the copy is theirs to remove; the conversation still goes
        folders = [self.history_dir(sid)] + [Path(d) / sid for d in (self.attachments_dir, self.outputs_dir) if d]
        with self._lock:
            self.records = [r for r in self.records if r["id"] != sid]
            for table in (self._live, self._events, self._seq, self._restarts, self._unconfirmed, self._step, self._calls, self._learned):
                table.pop(sid, None)
            self._save()
            leaves = [r["node"] for r in self.records if r.get("node")]
        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)
        self.nodes.keep(leaves)                                 # the messages only this conversation had go with it
        self._registry(rec.get("workspace"))
        return {"deleted": sid}

    def slash(self, sid: str, name: Any, args: Any = "") -> Dict[str, Any]:
        """A slash command that needs the conversation's process (tadamoo/commands.py); its answer comes as a `command` event."""
        if name not in SLASH_COMMANDS:
            raise RunError("That is not a command of the conversation.", "bad_command")
        text = str(args or "")[:2000]
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            live = self._ensure(sid)
        if not live.ready.wait(60):
            raise RunError("The conversation did not start in time; try again.", "start_failed")
        cid = secrets.token_hex(3)
        with self._lock:
            if not self._write(sid, live, {"cmd": "command", "id": cid, "name": name, "args": text}):
                raise RunError("The agent process did not take the command; try again.", "write_failed")
        return {"id": cid}

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

    # -- the conversation's own copy of the folder ---------------------------------------------------------------

    def _copy(self, sid: str, idle: bool = False) -> Dict[str, Any]:
        with self._lock:
            rec = self._record(sid)
            info = rec.get("worktree")
            if not info:
                raise RunError("This conversation works in the folder itself, not in a copy.", "no_copy")
            if idle and rec["state"] in ("busy", "starting", "asking"):
                raise RunError("Wait for the agent to finish, or stop it, first.", "busy")
            return info

    def copy_changes(self, sid: str) -> Dict[str, Any]:
        info = self._copy(sid)
        try:
            self.worktrees.ensure(info)
            return {"changes": self.worktrees.changes(info)}
        except WorktreeError as e:
            raise RunError(str(e), "no_copy") from None

    def copy_diff(self, sid: str, path: Any) -> Dict[str, Any]:
        info = self._copy(sid)
        if not isinstance(path, str) or not path or path.startswith("-") or ".." in Path(path).parts:
            raise RunError("Say which file.", "bad_path")
        try:
            return {"path": path, "diff": self.worktrees.diff(info, path)}
        except WorktreeError as e:
            raise RunError(str(e), "no_copy") from None

    def copy_apply(self, sid: str, paths: Any = None) -> Dict[str, Any]:
        """Put what the conversation changed in its copy into the person's folder (all of it, or the files named) as staged changes."""
        info = self._copy(sid, idle=True)
        if paths is not None and (not isinstance(paths, list) or any(not isinstance(p, str) or not p or p.startswith("-") for p in paths)):
            raise RunError("Name the files as a list of paths.", "bad_path")
        try:
            done = self.worktrees.apply(info, paths)
        except WorktreeError as e:
            raise RunError(str(e), "copy_conflict" if e.code == "conflict" else "no_copy") from None
        with self._lock:
            self._set(self._record(sid), worktree=info)
        return {"applied": done["applied"], "session": self.get(sid)}

    def copy_discard(self, sid: str) -> Dict[str, Any]:
        """Close the conversation and throw its copy away, with whatever was not applied."""
        info = self._copy(sid, idle=True)
        self.close(sid)
        self.worktrees.discard(info)
        with self._lock:
            self._set(self._record(sid), worktree=None, workdir=None, copy_discarded=True)
        return self.get(sid)

    def close(self, sid: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._record(sid)
            live = self._live.get(sid)
            if live is not None:
                live.ending = True
                self._write(sid, live, {"cmd": "close"})
            self._restart_token.pop(sid, None)
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

    def note(self, sid: str, event: Dict[str, Any]) -> None:
        """Puts an event of the server's own (not the process's) in the conversation's stream, for the page to show."""
        with self._lock:
            self._record(sid)
        self._push(sid, event)

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
        self.deliveries.pump()
        with self._lock:
            live_items = list(self._live.items())
            records = {r["id"]: r for r in self.records}
        from serve.agent_runs import _kill_tree
        for sid, live in live_items:
            rec = records.get(sid)
            if rec is None or live.ending or live.proc.poll() is not None:
                continue
            silent = now - live.last_seen
            beat, idle = self.beat_timeout_s, self.idle_s        # None: no limit
            if beat and silent >= beat:
                self._push(sid, {"event": "stalled", "why": "no sign of life for %d s" % silent})
                _kill_tree(live.proc)                            # the reader sees the end and starts it again
            elif idle and rec["state"] == "idle" and time.time() - rec.get("updated", 0) >= idle:
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
