"""E7: how much of the server's prefix cache survives each kind of change to a request.

Question: for each request shape (identical repeat, one new user turn, a changed tool list, a different system prompt with the same tools, a changed
block in the middle, a short unrelated side call between two turns), what share of the prompt does the server report as cached, and how long is the
time to the first token?

This is a measurement, not a choice between settings. The arms are the shapes. `A` (identical repeat) is the best case and `A~aa` is the same shape
again, so their gap is the noise; `G` (a request never seen before) is the worst case. For each other shape the report prints the cached share, the
time to the first token and the survival, (share - cold) / (identical - cold): 1 means the change cost the cache nothing, 0 that it cost all of it.
The rule below reads a shape's verdict against the noise. Run it once for each server setup that matters (`--server-setup "parallel=1"`, then
with `--parallel 2`, then with `--conversation-cache-mib 8192`) and compare the reports (docs/compaction.md, "The summary call and the engine's cache").

The tools are the same in every cycle, so the cold request still shares the tool block with the others: that is the floor, not zero.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from .core import Arm, CaseTrial, Context, Experiment, Rule, by_arm, mean_of, with_control

COLUMNS = ("cached_share", "ttft_s", "prompt_tokens", "cached_tokens", "seconds")
PREFIX_WORDS = (2000, 6000)
WORDS = ("granite gneiss obsidian pumice tuff scoria dolerite gabbro rhyolite andesite diorite peridotite komatiite phonolite trachyte "
         "kimberlite lamprophyre carbonatite ignimbrite").split()

ARMS = with_control([
    Arm("A", "identical repeat (best case)", {"shape": "identical"}, "the same request twice"),
    Arm("B", "one new user turn", {"shape": "new_turn"}, "the first request, its answer, a new user message"),
    Arm("C", "a changed tool list", {"shape": "tools_changed"}, "the same messages, one tool added"),
    Arm("D", "a different system prompt, same tools", {"shape": "system_changed"}, "the system text differs from its first words on"),
    Arm("E", "a block in the middle changed", {"shape": "middle_changed"}, "a long block after the first one differs"),
    Arm("F", "a short side call between two turns", {"shape": "side_call"}, "turn 1, an unrelated short request, turn 2"),
    Arm("G", "a request never seen (worst case)", {"shape": "cold"}, "a new conversation of the same size"),
], "A")

RULE = Rule(
    primary="cached_share", better="higher", min_effect=0.10, baseline="A", challengers=("B", "C", "D", "E", "F", "G"),
    text="Read each shape against the identical repeat. 'No difference beyond the noise' means the change leaves the prefix cache alone; 'A is better' means "
         "it costs part or all of the cache, and the survival column says how much. A shape keeps its cache when the cached share is within the A/A gap "
         "(and 10 points) of the identical repeat; it loses it when it is within the gap and 10 points of the never-seen request G. Anything between is a "
         "partial loss and is reported as the survival figure. G must differ from A: if it does not, the server reports no cache and the run says nothing.")

TOOLS = [{"type": "function", "function": {"name": f"tool_{i}", "description": f"Tool number {i}, which does nothing in this test.",
                                           "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}} for i in range(1, 7)]
EXTRA_TOOL = {"type": "function", "function": {"name": "tool_extra", "description": "One more tool, added to change the list.",
                                               "parameters": {"type": "object", "properties": {"y": {"type": "string"}}, "required": ["y"]}}}


@dataclass
class Seen:
    ttft_s: float
    total_s: float
    prompt: int
    cached: int
    text: str


def filler(salt: str, words: int, label: str) -> str:
    """About `words` words that differ for every salt and label, so that nothing is cached from an earlier cycle."""
    offset = int(hashlib.sha1(f"{salt}/{label}".encode()).hexdigest()[:6], 16)
    return " ".join(WORDS[(offset + i * 7 + i // 19) % len(WORDS)] + (str((offset + i) % 997) if i % 11 == 0 else "") for i in range(words))


def one(client: Any, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], max_tokens: int = 16) -> Seen:
    first: List[float] = []
    t0 = time.monotonic()

    def seen(kind: str, text: str) -> None:
        if not first:
            first.append(time.monotonic() - t0)
    reply = client.chat_completion(messages=messages, tools=tools, temperature=0.0, max_tokens=max_tokens, timeout=600.0, on_delta=seen,
                                   extra={"reasoning_effort": "none"})
    usage = reply.get("usage") or {}
    text = (reply["choices"][0]["message"].get("content") or "").strip()
    return Seen(first[0] if first else time.monotonic() - t0, time.monotonic() - t0, int(usage.get("prompt_tokens") or 0),
                int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0), text or "ok")


Send = Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], Seen]


def run_shape(shape: str, send: Send, salt: str, words: int) -> Seen:
    """Plays the requests of one shape and returns what the server said about the last one (the one measured). Every cycle's text is its own."""
    system = f"Session {salt}. You answer in one short word. Background notes, not to be repeated: " + filler(salt, words, "system")
    ask = {"role": "user", "content": "Name one volcanic rock."}
    base = [{"role": "system", "content": system}, ask]
    if shape == "cold":
        return send([{"role": "system", "content": f"Session {uuid.uuid4().hex[:10]}. Notes: " + filler(uuid.uuid4().hex, words, "cold")}, ask], TOOLS)
    if shape == "identical":
        send(base, TOOLS)
        return send(base, TOOLS)
    if shape in ("new_turn", "side_call"):
        first = send(base, TOOLS)
        if shape == "side_call":
            send([{"role": "system", "content": "You answer in one word."}, {"role": "user", "content": "Name one colour."}], None)
        return send([*base, {"role": "assistant", "content": first.text}, {"role": "user", "content": "And another one."}], TOOLS)
    if shape == "tools_changed":
        send(base, TOOLS)
        return send(base, [*TOOLS, EXTRA_TOOL])
    if shape == "system_changed":
        send(base, TOOLS)
        other = f"Session {salt}-b. Answer in one short word. Different notes: " + filler(salt, words, "system-b")
        return send([{"role": "system", "content": other}, ask], TOOLS)
    if shape == "middle_changed":
        head = {"role": "user", "content": "First document, fixed: " + filler(salt, words // 2, "head")}
        ack = {"role": "assistant", "content": "Read."}

        def convo(version: str) -> List[Dict[str, Any]]:
            return [{"role": "system", "content": f"Session {salt}. You answer in one short word."}, head, ack,
                    {"role": "user", "content": "Second document, changes: " + filler(salt, words // 2, "mid-" + version)}, ack, ask]
        send(convo("v1"), TOOLS)
        return send(convo("v2"), TOOLS)
    raise ValueError(f"unknown shape {shape!r}")


def tasks() -> List[Dict[str, Any]]:
    return [{"id": f"prefix{w}", "words": w} for w in PREFIX_WORDS]


def run_cycle(arm: Arm, task: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    trial = CaseTrial(arm.name, task["id"], rep)
    client = ctx.client
    seen = run_shape(arm.settings["shape"], lambda m, t: one(client, m, t), uuid.uuid4().hex[:10], task["words"])
    share = (seen.cached / seen.prompt) if seen.prompt else None
    if share is None:
        trial.invalid, trial.error = True, "the server reported no prompt tokens"
    trial.success = True
    trial.metrics = {"ttft_s": round(seen.ttft_s, 3), "prompt_tokens": seen.prompt, "cached_tokens": seen.cached, "seconds": round(time.monotonic() - t0, 2)}
    if share is not None:
        trial.metrics["cached_share"] = share
    return trial


def survival(grouped: Dict[str, Sequence[CaseTrial]]) -> Dict[str, Optional[float]]:
    """(share - cold) / (identical - cold) for every shape; None where the identical repeat and the cold request do not differ."""
    ident = [t for a in ("A", "A~aa") for t in grouped.get(a, [])]
    hi, lo = mean_of(ident, "cached_share"), mean_of(grouped.get("G", []), "cached_share")
    out: Dict[str, Optional[float]] = {}
    for name, ts in grouped.items():
        v = mean_of(ts, "cached_share")
        out[name] = None if v is None or hi is None or lo is None or hi - lo < 0.05 else max(0.0, min(1.0, (v - lo) / (hi - lo)))
    return out


def extra_report(trials: Sequence[CaseTrial]) -> List[str]:
    grouped = by_arm(trials)
    surv = survival(grouped)
    cold = mean_of(grouped.get("G", []), "ttft_s")
    lines = ["", "## Survival of the prefix cache", "", "| Shape | Cached share | Survival | Time to first token (s) | Against the never-seen request |", "| :--- | ---: | ---: | ---: | ---: |"]
    for arm in ARMS:
        ts = grouped.get(arm.name)
        if not ts:
            continue
        share, ttft = mean_of(ts, "cached_share"), mean_of(ts, "ttft_s")
        s = surv.get(arm.name)
        lines.append(f"| `{arm.name}` {arm.label} | {'n/a' if share is None else f'{share * 100:.0f}%'} | {'n/a' if s is None else f'{s * 100:.0f}%'} | "
                     f"{'n/a' if ttft is None else f'{ttft:.2f}'} | {'n/a' if ttft is None or not cold else f'{ttft / cold * 100:.0f}%'} |")
    if all(v is None for v in surv.values()):
        lines += ["", "The identical repeat and the never-seen request show the same cached share: the server reports no prefix cache, or the prefix is too short to show one."]
    return lines


def experiment() -> Experiment:
    return Experiment(
        id="e7", title="Prefix cache behaviour by request shape", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=tasks, run_trial=run_cycle, task_id=lambda t: t["id"],
        default_k=5, extra_report=extra_report,
        question="For each request shape, how much of the prompt does the server's prefix cache still hold, and how long is the time to the first token?")
