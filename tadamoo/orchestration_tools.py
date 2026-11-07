"""Tools a conversation uses to reach another one it did not start: session_send, session_status, session_spawn.

They are thin clients of the server (tadamoo/integrations/orchestration_client.py): the conversation runs in its own process and
never holds the server's shared secret, so every call proves who it is with the id and token it was started with. What reaches the
other conversation is a message with a declared origin (tadamoo/provenance.py): the receiver reads it as data from another
conversation unless it was told this sender may give it orders.

These tools are not in every conversation's prompt (see tadamoo/capabilities.py builtin:orchestration): they cost tokens that most
conversations never use, so they are registered only for one built with them from the start, or on request through
load_capability("builtin:orchestration").
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from tadamoo.integrations._http import ServiceError

MESSAGE_MAX = 4000
RULES_MAX = 20_000
RATE_LIMIT = 30                  # messages to one target in an hour
RATE_WINDOW_S = 3600.0
SPAWN_PROMPT_MAX = 8000


def _failure(exc: Exception) -> str:
    if isinstance(exc, ServiceError):
        return f"Could not reach the other conversation ({exc.kind}): {exc.detail or exc}"
    return f"Error: {exc}"


class RateLimiter:
    """At most `limit` events per key within `window_s`; in memory, one conversation's own process."""

    def __init__(self, limit: int = RATE_LIMIT, window_s: float = RATE_WINDOW_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            kept = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(kept) >= self.limit:
                self._hits[key] = kept
                return False
            kept.append(now)
            self._hits[key] = kept
            return True


def register_orchestration_tools(registry: Any, client: Any, limiter: Optional[RateLimiter] = None) -> None:
    from tadamoo.tools.registry import EffectClass

    limiter = limiter or RateLimiter()

    def session_send(ref: str, message: str) -> str:
        if not isinstance(ref, str) or not ref.strip():
            return "Name the conversation the message is for."
        if not isinstance(message, str) or not message.strip():
            return "Write the message."
        if len(message) > MESSAGE_MAX:
            return f"The message is too long ({len(message)} characters; at most {MESSAGE_MAX})."
        if not limiter.allow(ref.strip().lower()):
            return f"Too many messages to '{ref}' in the last hour; wait before sending more."
        try:
            result = client.deliver(ref, message)
        except Exception as exc:
            return _failure(exc)
        delivery = result.get("delivery") or {}
        state = delivery.get("state", "pending")
        if state == "dead":
            return f"Not delivered ({delivery.get('reason', 'unreachable')}): {delivery.get('detail', '')}"
        return f"Message {state}." + (" It is waiting for that conversation to be free." if state == "pending" else "")

    def session_status(ref: str) -> str:
        if not isinstance(ref, str) or not ref.strip():
            return "Name the conversation."
        try:
            status = client.status(ref)
        except Exception as exc:
            return _failure(exc)
        who = status.get("alias") or status["id"]
        line = f"{who}: {status['state']}"
        if status.get("title"):
            line += f' — "{status["title"]}"'
        if status.get("pending_messages"):
            line += f"; {status['pending_messages']} message(s) waiting for it"
        if status.get("dead_letters"):
            line += f"; {status['dead_letters']} could not be delivered"
        if status.get("last_answer"):
            line += f'\nIts last answer: "{status["last_answer"]}"'
        return line

    def session_spawn(workspace: str, prompt: str, background: bool = False, alias: str = "", rules: str = "") -> str:
        if not isinstance(workspace, str) or not workspace.strip():
            return "Give the folder it works in."
        if not isinstance(prompt, str) or not prompt.strip():
            return "Write what it should do."
        if len(prompt) > SPAWN_PROMPT_MAX:
            return f"The task is too long ({len(prompt)} characters; at most {SPAWN_PROMPT_MAX})."
        rules = rules if isinstance(rules, str) else ""
        if background and not rules.strip():
            return ("A background conversation needs operating rules of its own (the `rules` argument): what it must never do, and whom it "
                    "answers to. Write them and try again.")
        if len(rules) > RULES_MAX:
            return f"The rules are too long ({len(rules)} characters; at most {RULES_MAX})."
        try:
            made = client.create(workspace, title=prompt[:80], alias=alias or "", unattended=bool(background), rules=rules)
        except Exception as exc:
            return _failure(exc)
        session = made.get("session") or {}
        sid = session.get("id", "")
        if not sid:
            return "The conversation could not be created."
        try:
            client.send(sid, prompt)
        except Exception as exc:
            return f"Created ({session.get('alias') or sid}) but the task could not be sent: {_failure(exc)}"
        return f"Started {session.get('alias') or sid}, working on: {prompt[:120]}"

    registry.register("session_send", "Sends a short message to another conversation (by its name or id). It arrives as data from this "
                       "conversation, not as an order, unless the other one was told this conversation may give it orders.",
                       {"type": "object", "properties": {"ref": {"type": "string", "description": "The other conversation's name or id"},
                                                         "message": {"type": "string"}}, "required": ["ref", "message"]},
                      EffectClass.IRREVERSIBLE, session_send)
    registry.register("session_status", "Shows whether another conversation (by its name or id) is free or at work, its last answer, and "
                       "whether anything sent to it is waiting or could not be delivered.",
                       {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]}, EffectClass.PURE, session_status, untrusted=True)
    registry.register("session_spawn", "Starts a new conversation on a folder and gives it a task, running on its own. With background=true "
                       "it keeps working unattended after this call returns: give it `rules` (what it must never do, whom it answers to) or "
                       "it is refused.",
                       {"type": "object", "properties": {"workspace": {"type": "string"}, "prompt": {"type": "string"},
                                                         "background": {"type": "boolean"}, "alias": {"type": "string", "description": "a short name for it"},
                                                         "rules": {"type": "string", "description": "required with background=true"}},
                        "required": ["workspace", "prompt"]}, EffectClass.IRREVERSIBLE, session_spawn)
