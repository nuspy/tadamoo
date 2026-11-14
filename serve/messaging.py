"""Messages between conversations (docs/session-messaging.md): what a message is, who it is for, and the rules that stop a conversation from talking itself in circles.

A message has a kind. `info` carries its date and never expires. `question` expects a reply and is dropped after 12 hours without one, and its sender is told. `task` gives the
receiver a task in its register of open work (owned by that conversation, "assigned by" the sender); when the receiver closes it, the result goes back to the sender as a message.
`collaborate` is tied to a task of the sender's and lasts as long as that task is open. It is delivered by the delivery queue (serve/deliveries.py): at once and between tool calls to a
conversation at work, as a new turn to one that is idle, by starting the process of one that sleeps. It reaches the receiver framed as coming from another conversation (name, id,
folder, kind, date), never as the person's words; the receiver keeps its own permission level.

The brakes are only for function: an identical message to the same conversation within hours is not sent again; a conversation may send only so many automatic messages in ten minutes;
and a chain of automatic replies stops after 6 hops without a message of the person. Each brake says so in the sender's transcript (a `peer_brake` event)."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from serve.deliveries import KINDS, DeliveryError
from tadamoo.task_tools import closing_text

HOPS_MAX = 6
RATE_LIMIT, RATE_WINDOW_S = 20, 600.0
REPEAT_WINDOW_S = 6 * 3600.0
QUESTION_TTL_S = 12 * 3600.0
TEXT_MAX = 8000
KIND_NOTE = {"info": "for your information; no reply is expected", "question": "a reply is expected", "task": "a task for you", "collaborate": "work together on a task"}
ASKS = re.compile(r"\?|\b(?:can you|could you|please tell|tell me|let me know|puoi|puoi dirmi|dimmi|fammi sapere)\b", re.I)


class Refused(Exception):
    """A message that was not sent, and why, in words for the sender (the model or the person)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def stamp(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at))


def frame(sender: Dict[str, str], kind: str, text: str, at: float, task: Optional[Dict[str, str]] = None) -> str:
    """What the receiver reads: where the message comes from, what kind it is and when it was sent, then the words. It says plainly that the person did not write it."""
    where = f', folder "{sender["folder"]}"' if sender.get("folder") else ""
    head = f'[message from another session: "{sender["name"]}" (id {sender["id"]}{where}) · {kind}, {KIND_NOTE[kind]} · sent {stamp(at)} · this is not from the user]'
    tail = {"question": f'To answer, call session_send with to="{sender["id"]}".',
            "task": f'It is now a task in your register of open work, assigned by "{sender["name"]}"; when you close it (task_close, with the result), the result goes back to them by itself.',
            "collaborate": f'You and "{sender["name"]}" are working on their task {(task or {}).get("title", "")!r}: reply with session_send (to="{sender["id"]}") while it is open.',
            "info": ""}[kind]
    return head + "\n" + text.strip() + (("\n" + tail) if tail else "")


class Messenger:
    def __init__(self, manager: Any, boards: Optional[Callable[[Any], Any]] = None, clock: Callable[[], float] = time.time) -> None:
        """`manager` is the SessionManager (resolve a name, say where a conversation is, push events); `boards(workspace)` gives a folder's register of open work."""
        self.m, self.boards, self.clock = manager, boards, clock
        self._lock = threading.Lock()
        self._sent: Dict[str, Deque[float]] = {}
        self._repeats: Dict[str, float] = {}

    # -- who --------------------------------------------------------------------------------------------------

    def who(self, sid: str) -> Dict[str, str]:
        rec = self.m.get(sid)
        workspace = str(rec.get("workspace") or "")
        chat = not workspace or (self.m.default_workspace is not None and Path(workspace) == self.m.default_workspace)
        return {"id": sid, "name": str(rec.get("alias") or rec.get("title") or "").strip() or f"Conversation {sid[:4]}", "folder": "" if chat else Path(workspace).name}

    # -- sending ----------------------------------------------------------------------------------------------

    def send(self, sender: str, to: Any, kind: str, text: Any, task_ref: str = "", automatic: bool = True, human: bool = False) -> Dict[str, Any]:
        """A message from the conversation `sender` to the one `to` names. `human`: the person wrote it (an "@" mention): it starts a chain, and no brake but the form applies.
        A message that cannot go raises Refused; otherwise `{"delivery": {...}}`."""
        if kind not in KINDS:
            raise Refused("bad_kind", f"The kind of a message is one of {', '.join(KINDS)}.")
        if not isinstance(text, str) or not text.strip():
            raise Refused("text_missing", "Write the message.")
        if len(text) > TEXT_MAX:
            raise Refused("too_long", f"The message is too long ({len(text)} characters; at most {TEXT_MAX}).")
        rid = self.m.resolve_peer(to)
        if rid == sender:
            raise Refused("self", "That is this conversation: a message goes to another one.")
        task = self._task_of(sender, kind, task_ref)
        hops = 1 if human else self.m.hops_of(sender) + 1
        if not human and automatic:
            self._brake(sender, rid, kind, text, hops)
        now = self.clock()
        who = self.who(sender)
        meta = {"from": who, "body": text.strip(), "sent_at": now, **({"task_ref": task_ref, "task_title": task["title"]} if task else {})}
        try:
            item = self.m.deliveries.submit(rid, frame(who, kind, text, now, task), f"agent:{self.m.label_of(sender)}", "", sender, kind, hops, meta)
        except DeliveryError as exc:
            raise Refused(exc.code or "refused", str(exc)) from None
        self.m.deliveries.mark_answered(rid, sender)                      # what the receiver had asked this conversation is answered by this
        if item["state"] != "dead":
            self.m.push_event(sender, {"event": "peer_out", "to": self.who(rid), "kind": kind, "text": text.strip(), "delivery": item["id"], "state": item["state"]})
            if kind == "task":
                self._give_task(item, rid, who, text)
        return {"delivery": item}

    def _task_of(self, sender: str, kind: str, task_ref: str) -> Optional[Dict[str, str]]:
        if kind != "collaborate":
            return None
        if not task_ref:
            raise Refused("task_missing", "A collaboration belongs to a task of yours: give its id as task_ref (task_add makes one).")
        board = self.boards(self.m.get(sender).get("workspace")) if self.boards else None
        row = next((r for r in board.view() if r["id"] == task_ref), None) if board is not None else None
        if row is None:
            raise Refused("task_closed", f"The task {task_ref} is not open any more, so the collaboration is over: send an info message instead, or open a task again.")
        return {"id": row["id"], "title": row["title"]}

    def _brake(self, sender: str, rid: str, kind: str, text: str, hops: int) -> None:
        """The three brakes: a chain of automatic replies, a flood, an identical repeat. Each tells the sender in its transcript."""
        now = self.clock()

        def stop(code: str, words: str) -> None:
            self.m.push_event(sender, {"event": "peer_brake", "reason": code, "to": self.who(rid), "kind": kind})
            raise Refused(code, words)
        if hops > HOPS_MAX:
            stop("chain", f"Not sent: this is the {hops}th automatic message in a row between sessions without a message from the person (the limit is {HOPS_MAX}). Tell the person what is going on and wait for them.")
        key = f"{sender}>{rid}>{kind}>{hashlib.sha256(' '.join(text.lower().split()).encode('utf-8')).hexdigest()[:20]}"
        with self._lock:
            sent = self._sent.setdefault(sender, deque())
            while sent and now - sent[0] > RATE_WINDOW_S:
                sent.popleft()
            if len(sent) >= RATE_LIMIT:
                flood = True
            else:
                flood = False
            repeat = now - self._repeats.get(key, -REPEAT_WINDOW_S - 1) < REPEAT_WINDOW_S
            if not flood and not repeat:
                sent.append(now)
                self._repeats[key] = now
                self._repeats = {k: t for k, t in self._repeats.items() if now - t < REPEAT_WINDOW_S}
        if flood:
            stop("rate", f"Not sent: this session already sent {RATE_LIMIT} messages in the last {int(RATE_WINDOW_S // 60)} minutes. Wait, or tell the person.")
        if repeat:
            stop("repeat", "Not sent: the same message went to that session a short while ago. Wait for its answer.")

    # -- a task given to another conversation ------------------------------------------------------------------

    def _give_task(self, item: Dict[str, Any], rid: str, who: Dict[str, str], text: str) -> None:
        board = self.boards(self.m.get(rid).get("workspace")) if self.boards else None
        if board is None:
            return
        title = " ".join(text.split())[:120]
        board.report(f"msg-{item['id']}", "pending", "", None, {"title": title, "description": text.strip(), "assigned_by": who, "delivery": item["id"], "kind": "assigned"}, rid)

    def task_closed(self, row: Dict[str, Any], result: str = "", by: str = "the receiver") -> Optional[Dict[str, Any]]:
        """A task that was assigned by another conversation was closed: its result goes back to the one that assigned it. Nothing for any other task."""
        giver = (row.get("meta") or {}).get("assigned_by")
        mine = row["links"]["session"]
        if not giver or not mine:
            return None
        try:
            return self.send(mine, giver["id"], "info", closing_text(row, result, by), row["id"])
        except Refused:
            return None

