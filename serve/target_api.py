"""The Target Loop on the server: each conversation's targets, the thread that runs one, and the routes the page uses.

A target belongs to a conversation and is kept with it (`targets.json` in the conversation's own folder), so the dialog can list every earlier
one. The loop itself is tadamoo/target.py; this connects it to a conversation of the Chat or the Agent tab (the tasks are sent to it as ordinary
messages, so queue, steering, questions, the Stop button and the permission gate are the conversation's own), to the model for the structured
calls, and to the task registry of serve/tasks.py.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from tadamoo import target as tg
from tadamoo.client import TadamooClient
from serve.agent_runs import RunError
from serve.tasks import TaskJournal, _write_json

TARGETS_FILE = "targets.json"
POLL_S = 0.4
LOST_S = 8.0                                    # a conversation idle this long after a message with no sign of it: the turn is taken for lost
CANCEL_WAIT_S = 30.0
OUTPUT_CHARS = 4000


class SessionConversation:
    """One conversation of the Chat, seen as the loop needs it: send a prompt and wait for the answer, with what the tools did."""

    def __init__(self, sessions: Any, sid: str, changes: Optional[Callable[[], List[Dict[str, Any]]]] = None, poll_s: float = POLL_S,
                 lost_s: float = LOST_S) -> None:
        self.sessions, self.sid, self.changes, self.poll_s, self.lost_s = sessions, sid, changes, poll_s, lost_s

    def run(self, prompt: str, cancel: threading.Event) -> tg.TurnResult:
        after = self.sessions.events(self.sid)["next"]
        turn = self.sessions.send(self.sid, prompt)["turn"]
        calls: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        steered = asked = cancelled = False
        final, reason, error = "", "final", ""
        sent_at = time.monotonic()
        quiet_since: Optional[float] = None
        stop_sent_at = 0.0
        while True:
            if cancel.is_set() and not cancelled:
                cancelled, stop_sent_at = True, time.monotonic()
                self.cancel()
            batch = self.sessions.events(self.sid, after)
            after = batch["next"]
            finished = False
            for e in batch["events"]:
                kind, mine = e.get("event"), e.get("turn") in (turn, None)
                if kind == "routed" and e.get("turn") == turn and e.get("how") == "steer":
                    steered = True
                elif kind == "agent" and mine and isinstance(e.get("data"), dict):
                    d = e["data"]
                    if d.get("type") == "tool_start":
                        calls[d.get("call_id")] = {"name": d.get("name"), "arguments": d.get("arguments") or {}, "status": "running", "output": "",
                                                   **({"effect": d["effect"]} if d.get("effect") else {}), **({"gate": True} if d.get("gate") else {})}
                        order.append(d.get("call_id"))
                    elif d.get("type") == "tool_end" and d.get("call_id") in calls:
                        calls[d["call_id"]].update(status=d.get("status") or "completed", output=str(d.get("output") or "")[:OUTPUT_CHARS])
                elif kind == "done" and (e.get("turn") == turn or steered and not e.get("more") or asked):
                    final, reason, error = str(e.get("final") or final), str(e.get("stop_reason") or "final"), str(e.get("error") or "")
                    asked = reason == "asked"                        # the person answers in the conversation; that answer's turn is the next one
                    finished = not asked
            if finished:
                break
            now = time.monotonic()
            state = batch.get("state")
            if state == "closed":
                reason, error = "error", "the conversation was closed"
                break
            if state in ("idle", "dormant") and not batch["events"] and not asked:
                quiet_since = quiet_since or now
                if now - quiet_since > self.lost_s and now - sent_at > self.lost_s:
                    reason, error = "error", "the conversation went quiet without answering"
                    break
            else:
                quiet_since = None
            if cancelled and now - stop_sent_at > CANCEL_WAIT_S:
                break
            cancel.wait(self.poll_s)
        if cancelled:
            reason = "cancelled"
        return tg.TurnResult(final=final, stop_reason=reason, error=error, tools=[calls[c] for c in order if c in calls])

    def cancel(self) -> None:
        try:
            self.sessions.cancel(self.sid)
        except RunError:
            pass

    def file_exists(self, path: str) -> bool:
        try:
            self.sessions.workspace_file(self.sid, path)
            return True
        except RunError:
            return False

    def parked(self) -> List[str]:
        return [str(q.get("question") or "")[:300] for q in self.sessions.get(self.sid).get("pending") or []]

    def changes_since(self, since: float) -> List[Dict[str, Any]]:
        return [e for e in (self.changes() if self.changes else []) if isinstance(e, dict) and e.get("time", 0) >= since]

    def say(self, event: Dict[str, Any]) -> None:
        try:
            self.sessions.note(self.sid, event)
        except RunError:
            pass


class TargetManager:
    def __init__(self, sessions: Any, tasks: TaskJournal, changes: Optional[Callable[[], List[Dict[str, Any]]]] = None,
                 llm_factory: Optional[Callable[[], tg.StructuredLLM]] = None, conversation: Optional[Callable[[str], Any]] = None,
                 waits: Tuple[float, ...] = tg.MODEL_WAITS) -> None:
        self.sessions, self.tasks, self.changes, self.waits = sessions, tasks, changes, waits
        self.llm_factory = llm_factory or (lambda: tg.StructuredLLM(TadamooClient(base_url=sessions.endpoint, api_key=sessions.api_key or None)))
        self.conversation = conversation or (lambda sid: SessionConversation(sessions, sid, changes))
        self._lock = threading.RLock()
        self._targets: Dict[str, List[Dict[str, Any]]] = {}
        self._loops: Dict[str, Tuple[threading.Thread, threading.Event]] = {}

    # -- the files ------------------------------------------------------------------------------------------------

    def _path(self, sid: str) -> Path:
        return self.sessions.history_dir(sid) / TARGETS_FILE

    def _items(self, sid: str) -> List[Dict[str, Any]]:
        """The conversation's targets (the lock is held). One that was running when the server ended is stopped: no loop survives it."""
        if sid not in self._targets:
            try:
                data = json.loads(self._path(sid).read_text(encoding="utf-8"))
                items = [t for t in data.get("targets", []) if isinstance(t, dict) and t.get("id")]
            except (OSError, ValueError, AttributeError):
                items = []
            for t in items:
                if t.get("state") == "running" and t["id"] not in self._loops:
                    t.update(state="stopped", stop_reason="interrupted", phase="", current="")
            self._targets[sid] = items
        return self._targets[sid]

    def _save(self, sid: str) -> None:
        with self._lock:
            items = self._items(sid)[-tg.KEEP_TARGETS:]
            self._targets[sid] = items
            self.sessions.history_dir(sid).mkdir(parents=True, exist_ok=True)
            _write_json(self._path(sid), {"schema": "1.0", "targets": items})

    def _find(self, sid: str, tid: str) -> Dict[str, Any]:
        self.sessions.get(sid)                                    # raises RunError (gone) for a conversation that is not there
        for t in self._items(sid):
            if t["id"] == tid:
                return t
        raise tg.TargetError("That target is not in the list any more.", "gone")

    def _view(self, target: Dict[str, Any]) -> Dict[str, Any]:
        return tg.state_view(copy.deepcopy(target))

    # -- reading --------------------------------------------------------------------------------------------------

    def listing(self, sid: str) -> Dict[str, Any]:
        self.sessions.get(sid)
        with self._lock:
            items = [self._view(t) for t in self._items(sid)]
        running = next((t["id"] for t in items if t["state"] == "running"), None)
        return {"targets": list(reversed(items)), "active": running, "defaults": tg.clean_options({})}

    def get(self, sid: str, tid: str) -> Dict[str, Any]:
        with self._lock:
            return self._view(self._find(sid, tid))

    # -- starting and stopping ---------------------------------------------------------------------------------------

    def create(self, sid: str, text: Any, options: Any = None, origin: str = "") -> Dict[str, Any]:
        if not isinstance(text, str):
            raise tg.TargetError("Write the target.", "text_missing")
        rec = self.sessions.get(sid)
        if rec.get("state") == "closed":
            raise tg.TargetError("That conversation is closed.", "closed")
        with self._lock:
            if any(t["state"] == "running" for t in self._items(sid)):
                raise tg.TargetError("A target is already running in this conversation; stop it first.", "busy")
            target = tg.new_target(text, options, sid, origin)
            self._items(sid).append(target)
            self._save(sid)
            self._start(target)
            return self._view(target)

    def _start(self, target: Dict[str, Any]) -> None:
        sid, cancel = target["session"], threading.Event()
        conv = self.conversation(sid)
        loop = tg.TargetLoop(target, conv, self.llm_factory(), self.tasks, lambda t: self._save(t["session"]), cancel, self._lock, self.waits)

        def body() -> None:
            try:
                loop.run()
            finally:
                with self._lock:
                    self._loops.pop(target["id"], None)
        thread = threading.Thread(target=body, name=f"target-{target['id']}", daemon=True)
        self._loops[target["id"]] = (thread, cancel)
        thread.start()

    def stop(self, sid: str, tid: str) -> Dict[str, Any]:
        with self._lock:
            target = self._find(sid, tid)
            loop = self._loops.get(tid)
            if target["state"] != "running" or loop is None:
                raise tg.TargetError("That target is not running.", "not_running")
            loop[1].set()
            self._touch(target, phase="stopping")
        self.conversation(sid).cancel()
        return self.get(sid, tid)

    def _touch(self, target: Dict[str, Any], **changes: Any) -> None:
        target.update(changes, updated=time.time())
        self._save(target["session"])

    # -- the person's edits ---------------------------------------------------------------------------------------------

    def annotate(self, sid: str, tid: str, cid: Any, note: Any = None, status: Any = None) -> Dict[str, Any]:
        """The person's note on a criterion, and optionally its status: marked achieved or blocked by hand, or put back to pending so the loop judges it again."""
        if note is not None and (not isinstance(note, str) or len(note) > 4000):
            raise tg.TargetError("A note is text of up to 4000 characters.", "bad_note")
        if status is not None and status not in ("achieved", "blocked", "pending"):
            raise tg.TargetError("The person can mark a criterion achieved or blocked, or put it back to pending.", "bad_status")
        with self._lock:
            target = self._find(sid, tid)
            c = next((x for x in target["criteria"] if x["id"] == cid), None)
            if c is None:
                raise tg.TargetError("That criterion is not in the target.", "gone")
            if note is not None:
                c["note"] = note.strip()
            if status is not None:
                c["status"] = status
                c["by"] = "" if status == "pending" else "person"
                if status == "achieved":
                    c.update(missing="", flags=[], evidence=c["evidence"] or "", refs=c.get("refs", []))
                if status == "pending":
                    c["flags"] = []
            self._touch(target)
            return self._view(target)

    def rerun(self, sid: str, tid: Any, mode: Any) -> Dict[str, Any]:
        """Takes a target up again. "resume" goes on with the same target (what is achieved stays); "copy" makes a new one from the same text and options."""
        if mode not in ("resume", "copy"):
            raise tg.TargetError("Run it again as a copy or resume it.", "bad_mode")
        with self._lock:
            target = self._find(sid, tid)
            if any(t["state"] == "running" for t in self._items(sid)):
                raise tg.TargetError("A target is already running in this conversation; stop it first.", "busy")
            if mode == "copy":
                return self.create(sid, target["text"], target["options"], origin=target["id"])
            if target["state"] == "achieved":
                raise tg.TargetError("That target is already achieved; run it as a copy.", "achieved")
            for c in target["criteria"]:
                if c["by"] != "person" and c["status"] == "blocked":
                    c["status"] = "pending"                           # parked for a reason that may have gone: the loop judges it again
                    c["flags"] = []
            target.update(state="running", stop_reason="", phase="starting", approach_tried=False, ended=0)
            self._save(sid)
            self._start(target)
            return self._view(target)

    def forget(self, sid: str, tid: str) -> Dict[str, Any]:
        with self._lock:
            target = self._find(sid, tid)
            if target["state"] == "running":
                raise tg.TargetError("Stop the target first.", "running")
            self._targets[sid] = [t for t in self._items(sid) if t["id"] != tid]
            self._save(sid)
        return self.listing(sid)

    def shutdown(self) -> None:
        with self._lock:
            loops = list(self._loops.values())
        for _, cancel in loops:
            cancel.set()
        for thread, _ in loops:
            thread.join(2.0)

    # -- the routes ------------------------------------------------------------------------------------------------------

    def get_route(self, sid: str, rest: List[str]) -> Dict[str, Any]:
        if not rest:
            return self.listing(sid)
        if len(rest) == 1:
            return {"target": self.get(sid, rest[0])}
        raise tg.TargetError("not found", "gone")

    def post_route(self, sid: str, rest: List[str], req: Dict[str, Any]) -> Dict[str, Any]:
        if not rest:
            return {"target": self.create(sid, req.get("text"), req.get("options"))}
        if len(rest) == 2:
            tid, action = rest
            if action == "stop":
                return {"target": self.stop(sid, tid)}
            if action == "notes":
                return {"target": self.annotate(sid, tid, req.get("criterion"), req.get("note"), req.get("status"))}
            if action == "rerun":
                return {"target": self.rerun(sid, tid, req.get("mode"))}
            if action == "forget":
                return self.forget(sid, tid)
        raise tg.TargetError("not found", "gone")
