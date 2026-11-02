"""The slash commands that ask a conversation's own process something: what it is set to, what it holds, and the one that shortens its context.
Each returns plain data; the page words it in the person's language. Commands that only change the page, or that are a request to the
model, are the page's own (serve/web/slash.js)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List

from harness.permissions import GROUPS, PermissionLevel

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


def _section(name: str, text: str, **more: Any) -> Dict[str, Any]:
    raw = text.encode("utf-8")
    return {"name": name, "bytes": len(raw), "sha": hashlib.sha256(raw).hexdigest()[:12], **more}


def prefix_sections(session: Any) -> Dict[str, Any]:
    """The size and a short hash of each part of what comes first in every request: the blocks of the system prompt and the groups of
    tool definitions, then the two together. When the hash of a part changes between two requests, the server reads everything after it
    again."""
    agent, builder = session.agent, getattr(session.built, "builder", None)
    sections: List[Dict[str, Any]] = []
    if builder is not None:
        from harness.context import PIN_ORDER
        sections += [_section(name, builder.blocks[name]) for name in PIN_ORDER if builder.blocks.get(name)]
    registry = agent.tools
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for schema in registry.schemas():
        groups.setdefault(registry.get(schema["function"]["name"]).tier, []).append(schema)
    for tier, label in ((0, "tools:stable"), (1, "tools:session"), (2, "tools:external")):
        if tier in groups:
            sections.append(_section(label, json.dumps(groups[tier], sort_keys=True, ensure_ascii=False), count=len(groups[tier])))
    system = agent.messages[0].get("content") if agent.messages else ""
    whole = _section("prefix", system + "\n" + json.dumps(registry.schemas(), sort_keys=True, ensure_ascii=False))
    return {"sections": sections, "prefix_bytes": whole["bytes"], "prefix_sha": whole["sha"]}


def status(session: Any, args: str) -> Dict[str, Any]:
    built, agent = session.built, session.agent
    return {"mode": _mode_of(built.permissions), "role": built.role.id if built.role is not None else "general",
            "effort": agent.sampling.get("reasoning_effort", ""), "turns": session.turns_done,
            "max_turns": agent.max_turns, "end_turn_bias": agent.sampling.get("end_turn_bias", 0.0),
            "temperature": agent.sampling.get("temperature"), "max_tokens": agent.max_tokens, "notices": list(getattr(built, "notices", [])),
            "model_tokens": agent.total_tokens, "memories": built.memory.count() if built.memory else 0,
            "mcp_servers": len(built.mcp.catalog) if built.mcp is not None else 0, "tools": len(agent.tools.names()), **_context(session)}


def tools(session: Any, args: str) -> Dict[str, Any]:
    registry, role = session.agent.tools, getattr(session.built, "role", None)
    allowed = set(role.allowed()) if role is not None else set(registry.names())
    return {"role": role.id if role is not None else "general",
            "tools": [{"name": n, "effect": getattr(registry.get(n).effect, "value", str(registry.get(n).effect)), "role_allowed": n in allowed} for n in registry.names()]}


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
    removed = {name: rule.pattern for name in session.agent.tools.names() if (rule := p.rule_for(name)) is not None}
    return {"mode": _mode_of(p), "rules": [{"pattern": r.pattern, "action": r.action} for r in p.rules], "denied": p.denied,
            "untrusted_input": bool(p.untrusted_input), "refused_by": removed, "groups": {g: list(m) for g, m in GROUPS.items()}}


def audit(session: Any, args: str) -> Dict[str, Any]:
    """Whether the record of what this folder's agents did is whole: the hash chain and its head file, read from disk now."""
    ledger = session.built.audit
    valid, problem = ledger.verify_integrity()
    return {"valid": valid, "records": len(ledger.records), "problem": problem or ""}


def context(session: Any, args: str) -> Dict[str, Any]:
    comp = getattr(session.agent.context, "compaction", None)
    return {**_context(session), **prefix_sections(session), "notices": list(getattr(session.built, "notices", [])),
            "compaction": comp.status() if comp is not None else None}


def compact(session: Any, args: str) -> Dict[str, Any]:
    """The older part of the conversation becomes a summary (by the model, else a plain extract) with the newest steps kept as written; only
    when nothing is under way. The report says what the server will read again."""
    agent = session.agent
    ctx = agent.context
    if ctx is None or not hasattr(ctx, "compact_now"):
        return {"done": False, "reason": "unsupported"}
    if not session.idle():
        return {"done": False, "reason": "busy"}
    before_messages = list(agent.messages)
    before = ctx.estimate_tokens(before_messages)
    shortened = ctx.compact_now(before_messages)                        # may take a model call: nothing is held while it runs
    report = ctx.take_compaction()
    if report is None or len(shortened) >= len(before_messages):
        return {"done": False, "reason": "short", "tokens": before}
    with session._lock:
        if not session.idle() or agent.messages != before_messages:
            return {"done": False, "reason": "moved"}
        agent.messages = shortened
        session._save()
    return {"done": True, "before": before, "after": ctx.estimate_tokens(shortened), "compaction": report}


COMMANDS: Dict[str, Callable[[Any, str], Dict[str, Any]]] = {
    "status": status, "tools": tools, "hooks": hooks, "mcp": mcp, "memory": memory, "permissions": permissions, "context": context, "compact": compact, "audit": audit,
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
