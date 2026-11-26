"""Live test, part A: how the local Basalt provider handles sessions and context, measured on the running server.

  python tests/live/a_context_probe.py status          the engine's windows and what the harness reads from them
  python tests/live/a_context_probe.py isolation       the technical session leaves the conversation's cache alone
  python tests/live/a_context_probe.py summary_ab [N]  the compaction summary at 1500 and at 3000 answer tokens, N runs each (default 4)
  python tests/live/a_context_probe.py prompt_weight   the fixed prompt of a session, by part, as the server counts it

Results go to <LIVE_OUT>/a_*.jsonl. Answer tokens are counted on the answer text alone, never the thinking."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
from pathlib import Path

from common import ENDPOINT, ROOT, chat, gpu, record, status, tokens


def build():
    """The harness as a session builds it, on this server's real clients (the registry is read, nothing in it is changed)."""
    from tadamoo.client import TadamooClient
    from tadamoo.llm import client_for
    from tadamoo.session import parse_args
    from tadamoo.stack import AgentConfig, agent_settings, build_agent
    tmp = Path(tempfile.mkdtemp(prefix="live_a_"))
    ws = tmp / "ws"
    ws.mkdir()
    args = parse_args(["--workspace", str(ws), "--endpoint", ENDPOINT, "--permission-mode", "auto", "--capabilities", "--lsp", "--report-changes",
                       "--page-files", "--router", "rules", "--pipeline", "off"])
    st = agent_settings(args)
    st.update(use_memory=False, use_checkpoints=False, data_dir=str(tmp / "d"))
    here = TadamooClient(base_url=ENDPOINT, session_id="live-a")
    built = build_agent(AgentConfig(**st), client=client_for("executor", here), summarizer=client_for("compaction", here, level="none"),
                        technical=client_for("technical", here))
    return built


def cmd_status() -> None:
    st = status()
    record("a_status", {"test": "status", "model": st.get("model"), "loaded": st.get("loaded"), "context": st.get("context"),
                        "side_slot": (st.get("concurrency") or {}).get("side_slot"), "serving": (st.get("concurrency") or {}).get("serving"),
                        "length": {k: (st.get("length") or {}).get(k) for k in ("known", "limit_tokens", "reliable_tokens", "first_stop_tokens", "observed_max_tokens")},
                        **gpu()})
    from tadamoo import stack
    from tadamoo.client import TadamooClient
    from tadamoo.llm import client_for
    c = client_for("executor", TadamooClient(base_url=ENDPOINT))
    window = c.context_window()
    reserve = max(stack.AgentConfig.max_output_tokens, min(stack.ANSWER_RESERVE_TOKENS, (window or 0) // 4))
    record("a_status", {"test": "harness", "window": window, "budget": stack.context_budget(window, reserve) if window else None,
                        "route": list(c.registry.resolve_route("executor", True)), "failover": c.registry.failover_chain("executor"),
                        "long_min_tokens": __import__("serve.longform", fromlist=["x"]).long_min_tokens(int((st.get("length") or {}).get("limit_tokens") or 0))})


def doc_text(chars: int) -> str:
    out, total = [], 0
    for f in sorted((ROOT / "docs").rglob("*.md")):
        t = f.read_text(encoding="utf-8", errors="replace")
        if len(t) < 2000:
            continue
        out.append(f"## {f.name}\n{t[:6000]}")
        total += len(out[-1])
        if total >= chars:
            break
    return "\n\n".join(out)[:chars]


def cmd_isolation() -> None:
    """A conversation's cache after calls to the technical session: the prefix must still be read from the cache."""
    sid = f"live-a4-{int(time.time())}"
    conv = [{"role": "system", "content": "You are a test assistant."}, {"role": "user", "content": doc_text(45000) + "\n\nReply with the single word: ok"}]
    steps = []
    for label in ("first", "again"):
        r = chat(conv, max_tokens=8, thinking=False, session=sid)
        steps.append(r)
        record("a_isolation", {"step": label, "prompt": r["prompt_tokens"], "cached": r["cached_tokens"], "seconds": r["seconds"]})
    for i in range(3):
        s = chat([{"role": "system", "content": "Summarise the text in three lines."}, {"role": "user", "content": doc_text(9000 + 1500 * i)}], max_tokens=200, thinking=False,
                 extra={"cache_slot": "side", "side_use": "compaction"})
        record("a_isolation", {"step": f"side call {i + 1}", "prompt": s["prompt_tokens"], "cached": s["cached_tokens"], "seconds": s["seconds"], "finish": s["finish"]})
    r = chat(conv, max_tokens=8, thinking=False, session=sid)
    record("a_isolation", {"step": "conversation after the side calls", "prompt": r["prompt_tokens"], "cached": r["cached_tokens"], "seconds": r["seconds"],
                           "cache_kept": bool(r["cached_tokens"] and r["prompt_tokens"] and r["cached_tokens"] >= 0.9 * r["prompt_tokens"] - 64)})


def synth(built, target_chars: int = 360_000):
    """A long agent conversation made of real files read by read_file, as the compaction sees one."""
    msgs = list(built.agent.messages)
    msgs.append({"role": "user", "content": "Read the documentation and the code of the project and explain how compaction and long answers work, then write the notes."})
    files = sorted((ROOT / "docs").rglob("*.md")) + sorted((ROOT / "serve").glob("*.py")) + sorted((ROOT / "tadamoo").glob("*.py"))
    total = i = 0
    for f in files:
        txt = f.read_text(encoding="utf-8", errors="replace")[:14000]
        if len(txt) < 3000:
            continue
        cid = f"call_{i}"
        i += 1
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": f.relative_to(ROOT).as_posix()})}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "name": "read_file", "content": txt})
        total += len(txt)
        if total >= target_chars:
            break
    msgs.append({"role": "user", "content": "Good. Now put what you found in the notes."})
    return msgs


def cmd_summary_ab(runs: int = 4, caps=(1500, 3000), slot: str = "side") -> None:
    """`slot` side: the summary goes through the technical session (32000 tokens); main: that session is off for the compaction and the call
    runs on an ordinary slot, so an answer limit larger than the technical session can be tried."""
    import tadamoo.side_uses as su
    built = build()
    ctx = built.agent.context
    comp = ctx.compaction
    base = synth(built)
    record("a_summary", {"test": "input", "messages": len(base), "tokens_estimate": ctx.estimate_tokens(base), "budget": ctx.budget_tokens, **gpu()})
    real_param, caught, real_enabled = su.param, {}, su.enabled
    if slot == "main":
        su.enabled = lambda use, *a, **k: False if use == "compaction" else real_enabled(use, *a, **k)
    orig_write = comp._write

    def spy(c, plan, ledger, anchor):
        r = orig_write(c, plan, ledger, anchor)
        caught["text"], caught["source"], caught["error"] = r
        return r
    comp._write = spy
    try:
        for cap in list(caps) * runs:
            su.param = lambda use, name, *a, _cap=cap, **k: _cap if (use, name) == ("compaction", "answer_tokens") else real_param(use, name, *a, **k)
            comp.count, comp.cuts, comp._last_after = 0, 0, 0
            comp.reports.clear()
            caught.clear()
            t0 = time.time()
            out = comp.apply(ctx, copy.deepcopy(base), "manual")
            rep = out.report if out else {}
            record("a_summary", {"slot": slot, "answer_cap": cap, "seconds": round(time.time() - t0, 1), "applied": bool(out), "summary_by": caught.get("source"), "error": caught.get("error"),
                                 "cut_offs": rep.get("summary_cuts"), "tokens_before": rep.get("tokens_before"), "tokens_after": rep.get("tokens_after"),
                                 "summary_answer_tokens": tokens(caught.get("text") or ""), "summary_chars": len(caught.get("text") or "")})
    finally:
        su.param, su.enabled = real_param, real_enabled
        comp._write = orig_write


def cmd_prompt_weight() -> None:
    built = build()
    system = next(m["content"] for m in built.agent.messages if m["role"] == "system")
    tools = built.agent.tools.schemas()
    hi = [{"role": "user", "content": "hi"}]

    def count(messages, with_tools=None):
        return chat(messages, max_tokens=1, thinking=False, extra={"tools": with_tools} if with_tools else None)["prompt_tokens"]
    base = count(hi)
    with_system = count([{"role": "system", "content": system}] + hi)
    with_tools = count([{"role": "system", "content": system}] + hi, tools)
    record("a_prompt", {"bare_request": base, "system_prompt_tokens": with_system - base, "tools_tokens": with_tools - with_system, "tool_count": len(tools),
                        "fixed_total": with_tools - base, "note": "skills, lessons, memory, plans and the record of an earlier compaction are added at run time and are not here"})
    per = sorted(((tokens(json.dumps(t, ensure_ascii=False)), t["function"]["name"]) for t in tools), reverse=True)[:10]
    record("a_prompt", {"largest_tools_tokens": per})


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "summary_ab":      # summary_ab [runs] [caps, comma separated]
        cmd_summary_ab(int(sys.argv[2]) if len(sys.argv) > 2 else 4, tuple(int(x) for x in sys.argv[3].split(",")) if len(sys.argv) > 3 else (1500, 3000),
                       sys.argv[4] if len(sys.argv) > 4 else "side")
    else:
        {"status": cmd_status, "isolation": cmd_isolation, "prompt_weight": cmd_prompt_weight}.get(cmd, lambda: None)()
