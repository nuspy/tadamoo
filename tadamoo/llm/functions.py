"""The functions inside TaDaMoo that use a language model, and what each of them needs from its provider.

Only these internal functions can be pointed at another provider. What other applications see of this server stays the
loaded model, whatever is chosen here (see `docs/harness.md`). A function names a kind of work:

* `voice`: what the voice service asks of the model: the copilot, the spoken replies of a meeting or a call, the avatar's direct brain, the minutes;
* `chat`: a conversation with tools or without (Chat, the agent's loop, the planner's re-plan, summaries);
* `judge`: a short typed answer to a question (a choice, a score): the router of messages, the checks of the pipeline, the
  triage of a plan's events. A judge can be the loaded model, or a service that only judges (Jev).

`data` says what passes through the function: `personal` data (the person's plans, files, conversations) may not leave this PC
for a cloud provider without the person's yes for that function.
"""
from __future__ import annotations

import re
from typing import Any, Dict

LOCAL = ""                                         # no provider assigned: the model loaded in TaDaMoo, as always

FUNCTIONS: Dict[str, Dict[str, Any]] = {
    "chat": {"label": "Chat", "kind": "chat", "data": "personal", "tools": False},
    "executor": {"label": "Executor (the agent's loop)", "kind": "chat", "data": "personal", "tools": True},
    "voice": {"label": "Voice (copilot, meetings, calls, avatar)", "kind": "chat", "data": "personal", "tools": False},
    "planner": {"label": "Planner (re-plan)", "kind": "chat", "data": "personal", "tools": False},
    "compaction": {"label": "Context compaction", "kind": "chat", "data": "personal", "tools": False},
    "technical": {"label": "Technical session (quick jobs)", "kind": "chat", "data": "personal", "tools": False},
    "eval": {"label": "Evaluation", "kind": "chat", "data": "general", "tools": True},
    "router": {"label": "Messages during a task (router)", "kind": "judge", "data": "personal"},
    "pipeline": {"label": "Checks of the answer (pipeline)", "kind": "judge", "data": "personal"},
    "triage": {"label": "Plan events (triage)", "kind": "judge", "data": "personal"},
    "relevance": {"label": "Relevance of excerpts", "kind": "judge", "data": "general"},
    "rag_enrich": {"label": "Knowledge archives (summaries and questions)", "kind": "chat", "data": "personal", "tools": False},
}

COUNCIL_PREFIX = "council:"                        # one function for each role of the council, made from the role's id (tadamoo/council.py)
COUNCIL_ID = re.compile(r"^council:[a-z0-9][a-z0-9_-]{0,39}$")


def council_function(role_id: str) -> str:
    return COUNCIL_PREFIX + role_id


def is_council(function: Any) -> bool:
    return isinstance(function, str) and COUNCIL_ID.match(function) is not None


def known(function: Any) -> bool:
    return function in FUNCTIONS or is_council(function)


def names() -> tuple[str, ...]:
    return tuple(FUNCTIONS)


def spec(function: str) -> Dict[str, Any]:
    """What a function needs from its provider. A role of the council is a function made on the fly, with the needs the old fixed members had."""
    if function in FUNCTIONS:
        return FUNCTIONS[function]
    if is_council(function):
        return {"label": f"Council ({function[len(COUNCIL_PREFIX):]})", "kind": "chat", "data": "personal", "tools": False}
    raise KeyError(f"unknown function {function!r}; the functions are {', '.join(FUNCTIONS)}")
