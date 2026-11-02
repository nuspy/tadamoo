"""A web read made twice in one request of the person is not made again: the second call is told where the first one's text is.

Only the tools that read the web are covered, and only within one request (the web changes between requests). The guard
runs no tool and decides nothing about permissions: it answers a call whose exact twin already worked in this request
from the conversation, and records only calls that worked.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tadamoo.context import masked_artifact

WEB_READ_TOOLS = frozenset(("browser_open", "browse_url", "search_web", "find_online_capability"))


class RepeatGuard:
    def __init__(self, tools: Iterable[str] = WEB_READ_TOOLS) -> None:
        self.tools = frozenset(tools)
        self._done: Dict[str, Tuple[str, int]] = {}      # a call's signature -> (call id, step of the request it was made at)
        self._base = 0

    def restart(self, turns_so_far: int) -> None:
        """A new request of the person: nothing counts as read, and steps are counted from here."""
        self._done.clear()
        self._base = turns_so_far

    def signature(self, name: str, args: Any) -> str:
        """What makes two calls the same call: the tool and its arguments; empty for a tool that is not covered."""
        if name not in self.tools or not isinstance(args, dict):
            return ""
        return json.dumps({"tool": name, "args": args}, sort_keys=True, default=str)

    def remember(self, name: str, args: Any, call_id: str, output: Any, turns_so_far: int) -> None:
        """Notes a call that worked; a result that is an error is not kept, so trying again stays possible."""
        key = self.signature(name, args)
        if key and not (isinstance(output, dict) and output.get("error")) and key not in self._done:
            self._done[key] = (call_id, turns_so_far - self._base + 1)

    def again(self, name: str, args: Any, messages: List[Dict[str, Any]]) -> Optional[str]:
        """The facts to answer a repeated call with, or None when it should run: it is new, or the first one's text is no longer in the conversation."""
        first = self._done.get(self.signature(name, args))
        if first is None:
            return None
        call_id, step = first
        held = next((m for m in reversed(messages) if m.get("role") == "tool" and m.get("tool_call_id") == call_id), None)
        if held is None:
            return None
        said = f"Not run again: {name} was already called with these same arguments at step {step} of this request. "
        if not held.get("masked"):
            return said + "Its text is above in the conversation. The call was not repeated."
        path = masked_artifact(held.get("content"))
        if path is None:
            return None
        return said + f"Its text was masked to save context and is saved at {path}. The call was not repeated."
