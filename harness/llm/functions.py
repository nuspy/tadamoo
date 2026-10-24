"""The functions inside Basalt that use a language model, and what each of them needs from its provider.

Only these internal functions can be pointed at another provider. What other applications see of this server stays the
loaded model, whatever is chosen here (see `docs/harness.md`). A function names a kind of work:

* `chat`: a conversation with tools or without (Chat, the agent's loop, the planner's re-plan, summaries);
* `judge`: a short typed answer to a question (a choice, a score): the router of messages, the checks of the pipeline, the
  triage of a plan's events. A judge can be the loaded model, or a service that only judges (Jev).

`data` says what passes through the function: `personal` data (the person's plans, files, conversations) may not leave this PC
for a cloud provider without the person's yes for that function.
"""
from __future__ import annotations

from typing import Any, Dict

LOCAL = ""                                         # no provider assigned: the model loaded in Basalt, as always

FUNCTIONS: Dict[str, Dict[str, Any]] = {
    "chat": {"label": "Chat", "kind": "chat", "data": "personal", "tools": False},
    "executor": {"label": "Executor (the agent's loop)", "kind": "chat", "data": "personal", "tools": True},
    "planner": {"label": "Planner (re-plan)", "kind": "chat", "data": "personal", "tools": False},
    "compaction": {"label": "Context compaction", "kind": "chat", "data": "personal", "tools": False},
    "eval": {"label": "Evaluation", "kind": "chat", "data": "general", "tools": True},
    "router": {"label": "Messages during a task (router)", "kind": "judge", "data": "personal"},
    "pipeline": {"label": "Checks of the answer (pipeline)", "kind": "judge", "data": "personal"},
    "triage": {"label": "Plan events (triage)", "kind": "judge", "data": "personal"},
    "relevance": {"label": "Relevance of excerpts", "kind": "judge", "data": "general"},
}


def names() -> tuple[str, ...]:
    return tuple(FUNCTIONS)


def spec(function: str) -> Dict[str, Any]:
    if function not in FUNCTIONS:
        raise KeyError(f"unknown function {function!r}; the functions are {', '.join(FUNCTIONS)}")
    return FUNCTIONS[function]
