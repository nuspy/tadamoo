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
import copy
import difflib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from harness import context_patterns as context_patterns_mod
from harness.vault import valid_code
from harness.worktrees import WorktreeError, Worktrees
from serve import ROOT
from serve.api import logit_bias
from serve.agent_runs import MAX_ALLOW, MODES, SAFE_ENV_KEYS, RunBusy, RunError
from serve.approvals import ApprovalBoard
from serve.tasks import _write_json
from serve.util.winjob import contain

TEXT_CHARS = 20_000
EVENTS_KEPT = 3000
SESSIONS_FILE = "agent_sessions.json"
TITLE_CHARS = 80
OPTIONS = ("resume", "close")
ASK_FIELDS = ("id", "mode", "kind", "question", "options", "facts", "tool", "arguments", "free")
COMPARES_FILE = "compares.json"
COMPARE_SETTINGS = ("mode", "max_turns", "ask_only", "end_turn_bias")      # what the two sides of a comparison may differ in
EFFECTS_FILE = "tool_effects.json"                                          # the effect class of each tool this conversation has used
SNIPPET_CHARS = 80
FILE_DIFF_BYTES = 200_000


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
                 default_workspace: Optional[Path] = None, router: str = "model", pipeline: str = "full",
                 attachments_dir: Optional[Path] = None, outputs_dir: Optional[Path] = None, worktrees: Optional[Worktrees] = None,
                 vault_dir: Optional[Path] = None, payments_dir: Optional[Path] = None,
                 browser_prefs: Optional[Callable[[], Dict[str, bool]]] = None, browser_profile: Optional[Path] = None,
                 plan_effect: Optional[Callable[[str, str, str, str, str], Any]] = None, compare_wait_s: float = 1800.0) -> None:
        self.directory = Path(directory) / "sessions"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.endpoint, self.api_key, self.board = endpoint, api_key, board
        self.router = router                                    # how a message that comes during a task is judged: "model" or "rules"
        self.attachments_dir, self.outputs_dir = attachments_dir, outputs_dir      # the conversation's attached files and the files the agent made
        self.browser_profile = browser_profile                  # where the browser keeps its sign-ins when the person chose that
        self.browser_prefs = browser_prefs                      # whether the conversations have a browser, and whether it shows a window
        self.payments_dir = payments_dir                        # the person's payment systems and limits, for the agent's request_payment
        self.vault_dir = vault_dir                              # the person's vault: the conversation's process fills placeholders from it
        self.pipeline = pipeline                                # the check of an answer against its request: "off", "light" or "full"
        self.worktrees = worktrees or Worktrees(Path(directory) / "worktrees")      # a conversation's own copy of a git folder
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
        self.plan_effect = plan_effect                          # told what a conversation did while it worked on a plan's step (plan, step, tool, effect, what)
        self._step: Dict[str, Tuple[Any, Any]] = {}            # the plan step each conversation is working on, from its own tool calls
        self._calls: Dict[str, Dict[str, Tuple[str, Dict[str, Any], Any]]] = {}      # tool calls started and not yet ended
        self._learned: Dict[str, Dict[str, str]] = {}
        self.compare_wait_s = compare_wait_s
        self._compare_lock = threading.Lock()                   # the two sides of a comparison, and two comparisons, run one after the other
        self.compares: List[Dict[str, Any]] = self._load_compares()
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
            shown.append({k: (v[:TEXT_CHARS] if isinstance(v, str) else v) for k, v in m.items() if k in ("role", "content", "tool_calls", "name", "tool_call_id", "attachments")})
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
            if m.get("role") == "user" and isinstance(text, str) and text.startswith("<documents>\n"):
                from harness.attachments import split_documents
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
        new = secrets.token_hex(4)
        copy = None
        if rec.get("worktree"):
            try:
                copy = self.worktrees.create(new, rec["workspace"])
            except WorktreeError as e:
                raise RunError(f"The branch needs its own copy of the folder: {e}", "no_copy") from None
        child = {**{k: v for k, v in rec.items() if k not in ("worktree", "workdir")}, "id": new, "title": ((rec.get("title") or "") + " (branch)").strip()[:TITLE_CHARS], "branched_from": {"session": sid, "turn": turn},
                 "created": time.time(), "updated": time.time(), "state": "dormant", "blocked": None, "turns": turn}
        if copy:
            child["worktree"], child["workdir"] = copy, copy["workdir"]
        with self._lock:
            target = self.history_dir(new)
            target.mkdir(parents=True, exist_ok=True)
            _write_json(target / "history.json", {"session": new, "turns": turn, "saved": time.time(), "messages": kept, "pending": []})
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
        from harness.rewind import session_impact
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

    def go_back(self, sid: str, turn: Any, accept_irreversible: bool = False) -> Dict[str, Any]:
        """The conversation goes back to the end of the person's `turn`-th request: what came after is taken off (a copy is kept in the
        conversation's folder). It is the conversation only: files the agent changed are put back from the Changes panel, not from here,
        and an action that cannot be undone stays done: going back over one needs `accept_irreversible`."""
        self._check_turn(turn)
        with self._lock:
            rec = self._record(sid)
            if rec["state"] in ("busy", "starting", "asking"):
                raise RunError("Wait for the agent to finish, or stop it, before going back.", "busy")
        impact = self.back_impact(sid, turn)
        if impact["not_undone"] and accept_irreversible is not True:
            raise RunError("Some of the actions after that point cannot be undone by going back.", "needs_yes")
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
            if not isinstance(text, str) or not text.strip() or len(text) > TEXT_CHARS:
                raise RunError(f"Each continuation needs a message of at most {TEXT_CHARS} characters.", "text_missing")
            settings = {k: v[k] for k in COMPARE_SETTINGS if k in v}
            if settings.get("mode") not in (None, *[m for m in MODES if m != "auto"]):
                raise RunError("A comparison runs in default, plan or read-only mode: automatic mode is not offered here.", "bad_mode")
            if "max_turns" in settings and (not isinstance(settings["max_turns"], int) or isinstance(settings["max_turns"], bool) or not 1 <= settings["max_turns"] <= 50):
                raise RunError("The number of steps must be between 1 and 50.", "bad_steps")
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
        end = time.monotonic() + self.compare_wait_s
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
                return m["content"][:TEXT_CHARS]
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
                if step == (args.get("plan_id"), args.get("node_id")) and args.get("status") in ("completed", "failed", "skipped"):
                    self._step.pop(sid, None)
            elif step and effect in ("reversible", "irreversible") and not tool.startswith("plan_") and self.plan_effect is not None:
                from harness.rewind import describe_call
                try:
                    self.plan_effect(step[0], step[1], tool, effect, describe_call(tool, args))
                except Exception:
                    pass                                          # the plan not taking the note never stops the conversation

    # -- creating ----------------------------------------------------------------------------------------

    def create(self, workspace: Any, mode: Any = "default", allow: Any = None, ask_only: bool = False, title: Any = "",
               confirm_auto: bool = False, max_turns: Any = 15, context_patterns: Any = None,
               context_policy: Any = "stop", context_threshold: Any = 0.8, end_turn_bias: Any = None, worktree: Any = "auto") -> Dict[str, Any]:
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
        if context_policy not in ("stop", "rolling", "compact"):
            raise RunError("The policy for a full context is stop, rolling or compact.", "bad_policy")
        if isinstance(context_threshold, bool) or not isinstance(context_threshold, (int, float)) or not 0.3 <= context_threshold <= 1.0:
            raise RunError("The threshold is a share of the context window from 0.3 to 1.", "bad_policy")
        try:
            logit_bias.add_end_turn({"end_turn_bias": end_turn_bias}, [0])
        except ValueError as e:
            raise RunError(str(e), "bad_policy") from None
        patterns = context_patterns_mod.check_config(context_patterns) if context_patterns else {}
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
        rec = {"id": sid, "context_patterns": patterns, "context_threshold": float(context_threshold), "end_turn_bias": float(end_turn_bias or 0), "title": str(title or "").strip()[:TITLE_CHARS], "workspace": str(path), "mode": mode,
               "allow": [r.strip() for r in rules], "ask_only": bool(ask_only), "max_turns": max_turns,
               "created": time.time(), "updated": time.time(), "state": "dormant", "blocked": None, "turns": 0}
        if copy:
            rec["worktree"], rec["workdir"] = copy, copy["workdir"]
        with self._lock:
            self.records.append(rec)
            self._events[sid], self._seq[sid] = collections.deque(maxlen=EVENTS_KEPT), 0
            self._save()
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
        cmd = [self.python, "-m", "harness", "session", "--session-id", sid, "--history-dir", str(history), "--workspace", rec.get("workdir") or rec["workspace"],
               "--endpoint", self.endpoint, "--permission-mode", rec["mode"], "--max-turns", str(rec.get("max_turns", 15)), "--router", self.router, "--pipeline", self.pipeline, "--capabilities", "--lsp"]
        for rule in rec.get("allow", []):
            cmd += ["--allow", rule]
        if rec.get("ask_only"):
            cmd.append("--ask-only")
        elif self.vault_dir:
            cmd += ["--vault-dir", str(self.vault_dir)]
        if not rec.get("ask_only") and self.payments_dir:
            cmd += ["--payments-dir", str(self.payments_dir)]
        prefs = self.browser_prefs() if self.browser_prefs else {}
        if not rec.get("ask_only") and prefs.get("enabled"):
            cmd += ["--browser"] + (["--browser-headless"] if prefs.get("headless") else []) + (["--browser-private"] if prefs.get("private") else [])                    + (["--browser-profile", str(self.browser_profile)] if prefs.get("persistent") and self.browser_profile else [])
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
            elif kind == "rule_added":
                rule = str(event.get("rule") or "").strip()
                rules = list(rec.get("allow", []))
                if rule and len(rule) <= 200 and "\n" not in rule and rule not in rules and len(rules) < MAX_ALLOW:
                    self._set(rec, allow=rules + [rule])             # a rule the person chose in the question stays for this conversation's next starts
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
            live.after_ready = [dict(unconfirmed)] if unconfirmed else [{"cmd": "resume"}]

    def _write(self, sid: str, live: Live, cmd: Dict[str, Any]) -> bool:
        try:
            with live.wlock:
                live.proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
                live.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    # -- the page's commands -----------------------------------------------------------------------------

    def _check_attachments(self, sid: str, ids: Any) -> List[Dict[str, Any]]:
        """The cards of the files a message names; each must be attached to this conversation."""
        if not ids:
            return []
        if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i, str) for i in ids) or self.attachments_dir is None:
            raise RunError("Attach up to 20 files that were uploaded to this conversation.", "bad_attachments")
        from harness.attachments import AttachmentStore
        store = AttachmentStore(self.attachments_dir)
        try:
            return [store.get(sid, i) for i in ids]
        except KeyError:
            raise RunError("One of the attached files is not there any more: attach it again.", "bad_attachments") from None

    def send(self, sid: str, text: Any, attachments: Any = None) -> Dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise RunError("Write a message.", "text_missing")
        cards = self._check_attachments(sid, attachments)
        if len(text) > TEXT_CHARS:
            raise RunError(f"The message is too long ({len(text)} characters; at most {TEXT_CHARS}).", "text_long")
        with self._lock:
            rec = self._record(sid)
            if rec["state"] == "closed":
                raise RunError("That conversation is closed.", "closed")
            if rec["state"] not in ("busy", "asking", "starting") and not rec.get("worktree"):
                other = next((r for r in self.records if r["id"] != sid and r.get("workspace") == rec["workspace"] and not r.get("worktree")
                              and r["state"] in ("busy", "asking", "starting")), None)
                if other is not None:
                    raise RunError("Another conversation is working in this folder. Wait for it, or start this one in a folder under git: "
                                   "it then works in its own copy.", "folder_busy")
            turn = secrets.token_hex(3)              # a message that comes while the agent works is looked at by the agent itself
            live = self._ensure(sid)
            cmd = {"cmd": "send", "id": turn, "text": text, **({"attachments": [c["id"] for c in cards]} if cards else {})}
            self._unconfirmed[sid] = cmd
            if not rec.get("title"):
                rec["title"] = " ".join(text.split())[:TITLE_CHARS]
            keep = rec["state"] in ("busy", "asking") and live.ready.is_set()       # a task is under way: its state is not changed
            self._set(rec, blocked=None, question=None, **({} if keep else {"state": "starting" if not live.ready.is_set() else "busy"}))
            if not live.ready.is_set():
                live.after_ready.append(cmd)
            elif not self._write(sid, live, cmd):
                raise RunError("The agent process did not take the message; try again.", "write_failed")
        self._push(sid, {"event": "user", "turn": turn, "text": text, **({"attachments": [{k: c[k] for k in ("id", "name", "family", "size")} for c in cards]} if cards else {})})
        return {"turn": turn, "session": self.get(sid)}

    def answer(self, sid: str, ask_id: Any, value: Any) -> Dict[str, Any]:
        """The person's answer to a confirmation: it must be one of the buttons of the question that is waiting."""
        with self._lock:
            rec = self._record(sid)
            ask = rec.get("ask")
            live = self._live.get(sid)
            if not ask or live is None or ask.get("id") != ask_id:
                raise RunError("That question is not waiting for an answer any more.", "not_asking")
            free = bool(ask.get("free"))
            if free:
                if not valid_code(value):
                    raise RunError("A code is up to 32 letters, digits, spaces or dashes.", "bad_answer")
            elif value not in [o.get("value") for o in ask.get("options") or []]:
                raise RunError("That is not one of the answers.", "bad_answer")
            self._set(rec, state="busy", ask=None)
            if not self._write(sid, live, {"cmd": "answer", "id": ask_id, "value": value}):
                raise RunError("The agent process did not take the answer; try again.", "write_failed")
        self._push(sid, {"event": "answered", "ask": ask_id, "value": "" if free else value})        # a code is not kept in the conversation
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
