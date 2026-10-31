"""The technical session: the short slot the local server keeps for the harness's own short calls.

A short call (a triage, a suggestion, a summary of old steps, a judge, a length estimate) sends `side_extra(client)` in its
`extra`: the server then runs it in its technical session, a separate small context, so it neither waits behind a
conversation nor overwrites its remembered text. Without a technical session, or for a cloud route (the key never leaves this
server: harness.llm.client.THIS_SERVER_ONLY), the call is an ordinary one. The server falls back to the ordinary slots by
itself when a call does not fit the session, so a caller only keeps its prompt short."""
from __future__ import annotations

from typing import Any, Dict, Optional

KEY = "cache_slot"
SIDE_CONTEXT_TOKENS = 30000        # the size the server gives the session unless told otherwise (serve.config.SIDE_SLOT_DEFAULT)

_default: Any = None               # the local server's client, when the stack registers one


def use(client: Any) -> None:
    """Register the local server's client, for `side_extra()` called without one."""
    global _default
    _default = client


def side_context(client: Any = None) -> int:
    """The tokens the local server's technical session holds; 0 when it has none or cannot be asked."""
    local = getattr(client if client is not None else _default, "local", None) or (client if client is not None else _default)
    ask = getattr(local, "side_context", None)
    if not callable(ask):
        return 0
    try:
        return max(0, int(ask()))
    except Exception:                                   # an unreachable server is a server without one
        return 0


def side_extra(client: Any = None) -> Dict[str, Any]:
    """{"cache_slot": "side"} for a call to a local server that reports a technical session, else {}."""
    return {KEY: "side"} if side_context(client) > 0 else {}
