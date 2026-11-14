"""The tools a conversation uses to talk to the others: `session_list` and `session_send` (docs/session-messaging.md).

They are in every conversation's own group of the tool list (after the fixed ones), so they never move the cached start of the request. They are thin clients of the server
(tadamoo/integrations/orchestration_client.py): the conversation proves which one it is with the id and token it was started with, and the server frames the message for
the receiver (who sent it, from which folder, which kind, when, and that it is not the person's) and delivers it: between tool calls of a task under way, as a new turn to an idle
conversation, by starting the process of one that sleeps. They touch nothing of the person's: like the register of open work, they are the harness's own channel, and the
receiver stays under its own permission level."""
from __future__ import annotations

from typing import Any, Callable

from tadamoo.integrations._http import ServiceError
from tadamoo.tools.registry import EffectClass, ToolRegistry

KINDS = ("info", "question", "task", "collaborate")
LIST_SHOWN = 15


def _failure(exc: Exception) -> str:
    if isinstance(exc, ServiceError):
        return f"Not sent: {exc.detail or exc}"
    return f"Error: {exc}"


def register_session_tools(registry: ToolRegistry, client: Any, my_id: Callable[[], str]) -> None:
    def session_list(query: str = "", more: int = 0) -> str:
        try:
            page = client.peers(my_id(), query, int(more or 0))
        except Exception as exc:
            return _failure(exc)
        rows = page.get("sessions", [])
        if not rows:
            return "No other session matches." if query else "There is no other session to write to."
        lines = [f'- {r["name"]} (id {r["id"]}{", folder " + r["folder"] if r.get("folder") else ""}, {r.get("state", "")}): {r.get("summary") or "no summary yet"}' for r in rows]
        if page.get("next") is not None:
            lines.append(f"- more: call session_list again with more={page['next']}")
        return "Sessions you can write to, the most recently used first:\n" + "\n".join(lines)

    def session_send(to: str, text: str, kind: str = "info", task_ref: str = "") -> str:
        if kind not in KINDS:
            return f"The kind is one of {', '.join(KINDS)}."
        try:
            out = client.deliver(to, text, kind, task_ref)
        except Exception as exc:
            return _failure(exc)
        item = out.get("delivery") or {}
        state = item.get("state", "pending")
        if state == "dead":
            return f"Not delivered ({item.get('reason', 'unreachable')}): {item.get('detail', '')}"
        return {"info": "Sent as information.", "question": "Sent as a question: it expires if not answered in 12 hours, and you are told.",
                "task": "Sent as a task: it is now in that session's register, and its result comes back to you when it closes it.",
                "collaborate": "Sent for the collaboration on your task: it lasts as long as that task is open."}[kind] + (" It waits for that session to be free." if state == "pending" else "")

    registry.register("session_list", "Lists the other sessions you can write to, most recently used first, with what each is about (its card). Use it to find who to write to; "
                      "`query` filters by name or topic, `more` is the offset of the next page.",
                      {"type": "object", "properties": {"query": {"type": "string"}, "more": {"type": "integer"}}}, EffectClass.PURE, session_list, untrusted=True)
    registry.register("session_send", "Writes to another session (its name or id, from session_list). Do it on your own when something you change concerns what that session works on, "
                      "when you finish what it is waiting for, or when you need something from it; reply to a message from a session by writing back to it. Kinds: `info` (for its "
                      "information, no reply needed), `question` (you expect a reply), `task` (gives it work: it lands in its register of open work and the result comes back to you when it "
                      "closes it), `collaborate` (work together on one of YOUR open tasks: give its id as task_ref; it lasts as long as that task is). The message reaches the session at once "
                      "(between its tool calls, or as a new turn, waking it if needed), labelled as coming from you and not from the person. Identical repeats, floods and long chains "
                      "of automatic replies are stopped, and you are told when one is.",
                      {"type": "object", "properties": {"to": {"type": "string", "description": "the session's name or id"}, "text": {"type": "string"},
                                                        "kind": {"type": "string", "enum": list(KINDS)}, "task_ref": {"type": "string", "description": "for `collaborate`: the id of your task"}},
                       "required": ["to", "text"]}, EffectClass.PURE, session_send)
