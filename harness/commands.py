"""The slash commands that ask a conversation's own process something: what it is set to, what it holds, and the one that shortens its context.
Each returns plain data; the page words it in the person's language. Commands that only change the page, or that are a request to the
model, are the page's own (serve/web/slash.js)."""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from harness.permissions import PermissionLevel

MEMORIES_SHOWN = 10


def _mode_of(permissions: Any) -> str:
    if permissions.plan_mode:
        return "plan"
    return {PermissionLevel.READ_ONLY: "read_only", PermissionLevel.DEFAULT: "default", PermissionLevel.AUTO: "auto"}.get(permissions.level, "default")


def _context(session: Any) -> Dict[str, Any]:
    agent = session.agent
    used = agent.context.estimate_tokens(agent.messages) if agent.context is not None else 0
    window = session.window_tokens()
    return {"tokens": used, "window": window, "share": round(used / window, 3) if window else 0.0, "messages": len(agent.messages) - 1,
            "summarised": any(m.get("summary") for m in agent.messages)}


def status(session: Any, args: str) -> Dict[str, Any]:
    built, agent = session.built, session.agent
    return {"mode": _mode_of(built.permissions), "effort": agent.sampling.get("reasoning_effort", ""), "turns": session.turns_done,
            "model_tokens": agent.total_tokens, "memories": built.memory.count() if built.memory else 0,
            "mcp_servers": len(built.mcp.catalog) if built.mcp is not None else 0, "tools": len(agent.tools.names()), **_context(session)}


def tools(session: Any, args: str) -> Dict[str, Any]:
    registry = session.agent.tools
    return {"tools": [{"name": n, "effect": getattr(registry.get(n).effect, "value", str(registry.get(n).effect))} for n in registry.names()]}


def hooks(session: Any, args: str) -> Dict[str, Any]:
    h = session.agent.hooks
    if h is None:
        return {"sources": [], "pre_tool": 0, "post_tool": 0, "events": {}, "errors": []}
    return {"sources": list(h.sources), "pre_tool": len(h._pre_tool_hooks), "post_tool": len(h._post_tool_hooks),
            "events": {k: len(v) for k, v in h._event_hooks.items() if v}, "errors": list(h.errors)[-5:]}


def mcp(session: Any, args: str) -> Dict[str, Any]:
    hub = session.built.mcp
    if hub is None:
        return {"servers": []}
    names = sorted(set(hub.catalog) | set(hub.errors) | set(hub.servers))
    return {"servers": [{"name": n, "tools": len(hub.catalog.get(n, [])), "error": hub.errors.get(n, "")} for n in names]}


def memory(session: Any, args: str) -> Dict[str, Any]:
    store = session.built.memory
    if store is None:
        return {"count": 0, "items": []}
    items = store.search(args, MEMORIES_SHOWN) if args.strip() else store.recent(MEMORIES_SHOWN)
    return {"count": store.count(), "query": args.strip(), "items": [{"id": m["id"], "category": m["category"], "content": m["content"][:300]} for m in items]}


def permissions(session: Any, args: str) -> Dict[str, Any]:
    p = session.built.permissions
    return {"mode": _mode_of(p), "rules": [{"pattern": r.pattern, "action": r.action} for r in p.rules], "denied": p.denied,
            "untrusted_input": bool(p.untrusted_input)}


def context(session: Any, args: str) -> Dict[str, Any]:
    return _context(session)


def compact(session: Any, args: str) -> Dict[str, Any]:
    """The older half of the conversation becomes a summary (by the model, else a plain extract); only when nothing is under way."""
    agent = session.agent
    ctx = agent.context
    if ctx is None or not hasattr(ctx, "_summarize"):
        return {"done": False, "reason": "unsupported"}
    if not session.idle():
        return {"done": False, "reason": "busy"}
    before_messages = list(agent.messages)
    before = ctx.estimate_tokens(before_messages)
    shortened = ctx._summarize(before_messages)                         # may take a model call: nothing is held while it runs
    if len(shortened) >= len(before_messages):
        return {"done": False, "reason": "short", "tokens": before}
    with session._lock:
        if not session.idle() or agent.messages != before_messages:
            return {"done": False, "reason": "moved"}
        agent.messages = shortened
        session._save()
    return {"done": True, "before": before, "after": ctx.estimate_tokens(shortened)}


COMMANDS: Dict[str, Callable[[Any, str], Dict[str, Any]]] = {
    "status": status, "tools": tools, "hooks": hooks, "mcp": mcp, "memory": memory, "permissions": permissions, "context": context, "compact": compact,
}


def run(name: str, args: str, session: Any) -> Dict[str, Any]:
    """{"ok": True, "data": ...} or {"ok": False, "error": ...}; an unknown name is an error, never a guess."""
    fn = COMMANDS.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown command: {name}"}
    try:
        return {"ok": True, "data": fn(session, args or "")}
    except Exception as exc:                                              # a failed look is told, it does not end the conversation
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
