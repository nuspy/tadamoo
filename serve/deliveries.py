"""Messages that reach a conversation from a schedule, a plan or another conversation, and what becomes of them.

The server keeps a message until the conversation it is for takes it: that conversation may be asleep, at work, or gone. A delivery
is written to journal/deliveries.json before it is tried and ends in a state the person can see: `delivered`, or `dead` with the
reason and the whole original message. Nothing is dropped silently.

  pending     waiting for the target to be free (or for its answer, once the message was written to it)
  delivered   the target's process took it as a turn
  dead        it could not be delivered: `unreachable` (no such conversation, closed, would not start), `busy_timeout` (at work for too
              long), `loop` (the message already went through the target more than twice), `budget` (the target spent its day)
  dismissed   the person looked at a dead letter and put it away

The target's process answers each message with the id of the delivery, and drops an id it has already taken, so a message written
twice (a restart between the write and the answer) is one turn. The server never waits on a lock for the target: it asks the
conversation manager for the target's state, and writes only to a conversation that is idle or asleep.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence

FILE = "deliveries.json"
MESSAGE_MAX = 8000
BUSY_TIMEOUT_S = 30 * 60.0          # at work for longer than this, a message for it is dead
ACK_TIMEOUT_S = 90.0                # written to the target and not answered: written again
KEEP = 500
LOOP_LIMIT = 2                      # times a message may already have passed through its target before it is stopped
FREE_STATES = ("idle", "dormant")
PROVENANCE = re.compile(r"^(?:cron(?::[A-Za-z0-9_.-]{1,40})?|plan:[A-Za-z0-9_-]{1,64}|agent:[A-Za-z0-9_-]{1,40})$")
REASONS = ("unreachable", "busy_timeout", "loop", "budget")


class DeliveryError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class Port(Protocol):
    """What the queue needs from the conversation manager."""

    def resolve(self, ref: Any) -> Optional[str]:
        """The id of the conversation a reference (its id, or its alias) names, or None."""

    def state_of(self, sid: str) -> Optional[str]:
        """dormant, starting, busy, idle, asking, blocked or closed; None for a conversation that is not there."""

    def push_delivery(self, sid: str, delivery: Dict[str, Any]) -> bool:
        """Writes the message to the conversation's process (starting it if it sleeps); False when that is not possible."""

    def over_budget(self, sid: str) -> bool:
        """The conversation spent what it may spend today."""


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink(missing_ok=True)


class DeliveryQueue:
    def __init__(self, directory: str | Path, port: Port, clock: Callable[[], float] = time.time,
                 busy_timeout_s: float = BUSY_TIMEOUT_S, ack_timeout_s: float = ACK_TIMEOUT_S) -> None:
        self.path = Path(directory) / FILE
        self.port, self.clock = port, clock
        self.busy_timeout_s, self.ack_timeout_s = busy_timeout_s, ack_timeout_s
        self._lock = threading.RLock()
        self.items: List[Dict[str, Any]] = self._load()

    # -- the file --------------------------------------------------------------------------------

    def _load(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("deliveries") if isinstance(data, dict) else None
        except (OSError, ValueError):
            items = None
        out = [i for i in items or [] if isinstance(i, dict) and i.get("id")]
        for item in out:
            item["sent"] = 0.0                                   # nothing is awaiting an answer from a process that is gone
        return out

    def _save(self) -> None:
        live = [i for i in self.items if i["state"] == "pending"]
        rest = [i for i in self.items if i["state"] != "pending"][-KEEP:]
        self.items = sorted(live + rest, key=lambda i: i["created"])
        _write_json(self.path, {"schema": "1.0", "deliveries": self.items})

    # -- sending ---------------------------------------------------------------------------------

    def submit(self, target: Any, message: Any, provenance: Any, chain: Sequence[str] = (), run_id: str = "", sender: str = "") -> Dict[str, Any]:
        """Takes a message for the conversation `target` names and tries to deliver it at once. With a `run_id` the same message is taken
        once: a second call for the same fire and target returns the first."""
        if not isinstance(message, str) or not message.strip():
            raise DeliveryError("Write the message.", "text_missing")
        if len(message) > MESSAGE_MAX:
            raise DeliveryError(f"The message is too long ({len(message)} characters; at most {MESSAGE_MAX}).", "too_long")
        if not isinstance(provenance, str) or not PROVENANCE.match(provenance):
            raise DeliveryError("Say where the message comes from: cron, cron:<id>, plan:<id> or agent:<ref>.", "bad_provenance")
        target = str(target or "").strip()
        if not target:
            raise DeliveryError("Name the conversation the message is for.", "no_target")
        with self._lock:
            if run_id:
                known = next((i for i in self.items if i.get("run_id") == run_id and i["target"] == target and i["state"] in ("pending", "delivered", "dead")), None)
                if known is not None:
                    return dict(known)
            now = self.clock()
            item = {"id": secrets.token_hex(4), "run_id": run_id, "target": target, "session": "", "provenance": provenance, "chain": [str(c) for c in chain][-12:],
                    "sender": sender, "message": message, "state": "pending", "reason": "", "detail": "", "created": now, "first_try": 0.0, "last_try": 0.0,
                    "attempts": 0, "sent": 0.0, "delivered": 0.0}
            self.items.append(item)
            self._save()
            self._attempt(item, now)
            return dict(item)

    def _die(self, item: Dict[str, Any], reason: str, detail: str = "") -> None:
        item.update(state="dead", reason=reason, detail=detail, sent=0.0)

    def _attempt(self, item: Dict[str, Any], now: float) -> None:
        """One look at a pending delivery: dead, written to its target, or still waiting."""
        sid = self.port.resolve(item["target"])
        if sid is None:
            self._die(item, "unreachable", f"No conversation is called '{item['target']}'.")
        elif item["chain"].count(sid) > LOOP_LIMIT:
            self._die(item, "loop", "The message already went through this conversation more than twice.")
        else:
            state = self.port.state_of(sid)
            item["session"] = sid
            if state in (None, "closed"):
                self._die(item, "unreachable", "That conversation is closed.")
            elif state in FREE_STATES and not item["sent"]:
                if self.port.over_budget(sid):
                    self._die(item, "budget", "That conversation has spent what it may spend today.")
                else:
                    item["first_try"] = item["first_try"] or now
                    item["last_try"], item["attempts"] = now, item["attempts"] + 1
                    try:
                        written = self.port.push_delivery(sid, item)
                    except Exception as exc:
                        self._die(item, "unreachable", f"The conversation would not start: {exc}")
                    else:
                        if written:
                            item["sent"] = now
                        elif self.port.state_of(sid) in (None, "closed"):
                            self._die(item, "unreachable", "That conversation could not take the message.")
            elif state not in FREE_STATES and now - item["created"] > self.busy_timeout_s:
                self._die(item, "busy_timeout", f"That conversation was busy for more than {int(self.busy_timeout_s // 60)} minutes.")
        self._save()

    def pump(self, now: Optional[float] = None) -> int:
        """One round over the pending deliveries (the manager's supervisor calls it): waiting ones are tried again, ones written and never
        answered are written again. Returns how many are still pending."""
        now = self.clock() if now is None else now
        with self._lock:
            pending = [i for i in self.items if i["state"] == "pending"]
            for item in pending:
                if item["sent"] and now - item["sent"] > self.ack_timeout_s:
                    item["sent"] = 0.0
                if not item["sent"]:
                    self._attempt(item, now)
            return sum(1 for i in self.items if i["state"] == "pending")

    def acked(self, delivery_id: str, state: str, session: str = "") -> Optional[Dict[str, Any]]:
        """The target's process answered: it took the message (`accepted`), or it was at work and did not (`busy`)."""
        with self._lock:
            item = next((i for i in self.items if i["id"] == delivery_id), None)
            if item is None or item["state"] in ("dismissed",):
                return None
            if state == "accepted":
                item.update(state="delivered", reason="", detail="", delivered=self.clock(), sent=0.0)
            elif item["state"] == "pending":
                item["sent"] = 0.0                               # at work: it stays in the queue
            self._save()
            return dict(item)

    # -- what the person and the status see ---------------------------------------------------------

    def entries(self, state: str = "", session: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(i) for i in reversed(self.items) if (not state or i["state"] == state) and (not session or i["session"] == session)]

    def get(self, delivery_id: str) -> Dict[str, Any]:
        with self._lock:
            item = next((i for i in self.items if i["id"] == delivery_id), None)
            if item is None:
                raise DeliveryError("That message is not in the list any more.", "gone")
            return dict(item)

    def dead_count(self, session: str = "") -> int:
        with self._lock:
            return sum(1 for i in self.items if i["state"] == "dead" and (not session or i["session"] == session))

    def pending_count(self, session: str = "") -> int:
        with self._lock:
            return sum(1 for i in self.items if i["state"] == "pending" and (not session or i["session"] == session))

    def retry(self, delivery_id: str) -> Dict[str, Any]:
        """A dead letter goes back in the queue, with its original message, and is tried at once."""
        with self._lock:
            item = next((i for i in self.items if i["id"] == delivery_id), None)
            if item is None:
                raise DeliveryError("That message is not in the list any more.", "gone")
            if item["state"] != "dead":
                raise DeliveryError("Only a message that could not be delivered is sent again.", "not_dead")
            now = self.clock()
            item.update(state="pending", reason="", detail="", created=now, sent=0.0, first_try=0.0)
            self._save()
            self._attempt(item, now)
            return dict(item)

    def dismiss(self, delivery_id: str) -> Dict[str, Any]:
        with self._lock:
            item = next((i for i in self.items if i["id"] == delivery_id), None)
            if item is None:
                raise DeliveryError("That message is not in the list any more.", "gone")
            if item["state"] != "dead":
                raise DeliveryError("Only a message that could not be delivered is put away.", "not_dead")
            item["state"] = "dismissed"
            self._save()
            return dict(item)
