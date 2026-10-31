"""The technical session: the short slot the local server keeps for the harness's own short calls.

A short call (a triage, a suggestion, a summary of old steps, a judge, a length estimate) sends `side_extra(client)` in its
`extra`: the server then runs it in its technical session, a separate small context, so it neither waits behind a
conversation nor overwrites its remembered text. Without a technical session, or for a cloud route (the key never leaves this
server: harness.llm.client.THIS_SERVER_ONLY), the call is an ordinary one. The server falls back to the ordinary slots by
itself when a call does not fit the session, so a caller only keeps its prompt short."""
from __future__ import annotations

from typing import Any, Dict, Optional

from harness import side_uses

KEY = "cache_slot"
USE_KEY = "side_use"
SIDE_CONTEXT_TOKENS = 30000        # the size the server gives the session unless told otherwise (serve.config.SIDE_SLOT_DEFAULT)

_default: Any = None               # the local server's client, when the stack registers one


def use(client: Any) -> None:
    """Register the local server's client, for `side_extra()` called without one."""
    global _default
    _default = client


def side_context(client: Any = None, use: Optional[str] = None) -> int:
    """The tokens the local server's technical session holds; 0 when it has none, cannot be asked, or `use` is switched off in the settings."""
    if use is not None and not side_uses.enabled(use):
        return 0
    who = client if client is not None else _default
    ask = getattr(who, "side_context", None)                       # a routed client says what its own route has
    if not callable(ask):
        ask = getattr(getattr(who, "local", None), "side_context", None)
    if not callable(ask):
        return 0
    try:
        return max(0, int(ask()))
    except Exception:                                   # an unreachable server is a server without one
        return 0


def side_extra(client: Any = None, use: Optional[str] = None, only: bool = False) -> Dict[str, Any]:
    """{"cache_slot": "side"} for a call to a local server that reports a technical session, else {}. `use` (a name in harness/side_uses.py)
    is switched off there: {}; on: it is named in the call so the server can count the session's work by use. With `only` the server refuses
    a call that does not fit the session instead of serving it from a conversation's slot."""
    if side_context(client, use) <= 0:
        return {}
    return {KEY: "side_only" if only else "side", **({USE_KEY: use} if use else {})}


def side_extra_for(client: Any, use: str, chars: int, answer_tokens: int) -> Dict[str, Any]:
    """`side_extra(client, use)` only when a text of `chars` characters and an answer of `answer_tokens` fit the session; else {}. For a
    call that must not fall back to an ordinary slot by itself (the server would serve one that does not fit from a conversation's slot)."""
    from harness import digest
    stays = getattr(client, "stays_local", None)
    if callable(stays) and not stays() and not side_uses.external_on():       # a text goes to another provider only when the person allowed it
        return {}
    window = side_context(client, use)
    if window <= 0 or chars > digest.part_chars(window, answer_tokens):
        return {}
    return side_extra(client, use, only=True)
