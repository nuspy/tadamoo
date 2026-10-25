"""What it costs when two conversations take turns on one model: the time to the first token and the share of the prompt read again.

Two conversations, each with its own long fixed start and a short new turn every round, are run twice against the same server: taking turns
(A, B, A, B...) and one after the other (A, A, A... then B, B, B...). When the engine keeps one prompt cache, taking turns throws the other
conversation's start away at every switch, and the second run shows how much that costs; when it keeps several, the two runs look alike. The
numbers decide whether a queue that groups requests by conversation is worth building; they are a development comparison, labelled with the GPU
power limit of the run, not a benchmark.

    python -m harness.eval.alternate --rounds 6 --prefix-words 2500 [--gpu-power-limit-w 450] [--out report.md]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from harness.client import BasaltClient

WORDS = ("granite basalt obsidian pumice tuff scoria dolerite gabbro rhyolite andesite diorite peridotite komatiite phonolite trachyte "
         "kimberlite lamprophyre carbonatite ignimbrite").split()


def long_start(label: str, words: int) -> str:
    """A fixed start of about `words` words, different for every conversation and every run so that nothing is cached from before."""
    salt = uuid.uuid4().hex[:8]
    body = " ".join(WORDS[(i * 7 + len(label) + i // 19) % len(WORDS)] for i in range(words))
    return f"Conversation {label}-{salt}. Notes to keep in mind (do not repeat them): {body}."


def one_request(client: Any, system: str, turn: int, max_tokens: int) -> Dict[str, Any]:
    """{"ttft_s", "total_s", "prompt", "cached"} of one turn."""
    first: List[float] = []
    t0 = time.monotonic()

    def seen(kind: str, text: str) -> None:
        if not first:
            first.append(time.monotonic() - t0)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": f"Round {turn}: name one volcanic rock, in one word."}]
    reply = client.chat_completion(messages=messages, temperature=0.0, max_tokens=max_tokens, timeout=600.0, on_delta=seen,
                                   extra={"reasoning_effort": "none"})
    usage = reply.get("usage") or {}
    return {"ttft_s": first[0] if first else time.monotonic() - t0, "total_s": time.monotonic() - t0, "prompt": int(usage.get("prompt_tokens") or 0),
            "cached": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)}


def run_order(client: Any, order: List[str], starts: Dict[str, str], max_tokens: int) -> List[Dict[str, Any]]:
    seen: Dict[str, int] = {}
    out = []
    for who in order:
        seen[who] = seen.get(who, 0) + 1
        out.append({"who": who, **one_request(client, starts[who], seen[who], max_tokens)})
    return out


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """Over the requests after each conversation's first (the first always reads its whole start)."""
    seen, later = set(), []
    for r in rows:
        if r["who"] in seen:
            later.append(r)
        seen.add(r["who"])
    if not later:
        return {"requests": 0}
    prompt = sum(r["prompt"] for r in later)
    return {"requests": len(later), "ttft_median_s": statistics.median(r["ttft_s"] for r in later), "ttft_mean_s": statistics.fmean(r["ttft_s"] for r in later),
            "total_mean_s": statistics.fmean(r["total_s"] for r in later), "cached_share": (sum(r["cached"] for r in later) / prompt) if prompt else 0.0}


def compare(client: Any, rounds: int, prefix_words: int, max_tokens: int = 8, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    alternating = ["A", "B"] * rounds
    apart = ["A"] * rounds + ["B"] * rounds
    result = {}
    for name, order in (("taking turns", alternating), ("one after the other", apart)):
        starts = {"A": long_start("A", prefix_words), "B": long_start("B", prefix_words)}
        rows = run_order(client, order, starts, max_tokens)
        result[name] = {"summary": summarize(rows), "rows": rows}
        sleep(1.0)
    return result


def report(result: Dict[str, Any], rounds: int, prefix_words: int, power_limit_w: Optional[float], model: str = "") -> str:
    lines = ["# Two conversations on one model", "",
             f"Rounds: {rounds} each. Fixed start: about {prefix_words} words. Model: {model or 'unknown'}. GPU power limit: "
             f"{f'{power_limit_w:g} W' if power_limit_w else 'not given'}.", "",
             "| Order | Requests counted | Time to first token, median (s) | mean (s) | Whole answer, mean (s) | Prompt read from the cache |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, data in result.items():
        s = data["summary"]
        if not s.get("requests"):
            lines.append(f"| {name} | 0 | - | - | - | - |")
            continue
        lines.append(f"| {name} | {s['requests']} | {s['ttft_median_s']:.2f} | {s['ttft_mean_s']:.2f} | {s['total_mean_s']:.2f} | {s['cached_share'] * 100:.0f}% |")
    a, b = result["taking turns"]["summary"], result["one after the other"]["summary"]
    if a.get("requests") and b.get("requests") and b["ttft_median_s"] > 0:
        lines += ["", f"Taking turns made the time to the first token {a['ttft_median_s'] / b['ttft_median_s']:.1f} times that of working one after the other "
                  f"({a['ttft_median_s']:.2f} s against {b['ttft_median_s']:.2f} s) and read {a['cached_share'] * 100:.0f}% of the prompt from the cache against "
                  f"{b['cached_share'] * 100:.0f}%. A difference under about 20% is noise until a second run repeats it.", ""]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m harness.eval.alternate", description="The cost of two conversations taking turns on one model")
    p.add_argument("--endpoint", default="http://127.0.0.1:7232")
    p.add_argument("--api-key", default=None)
    p.add_argument("--rounds", type=int, default=6)
    p.add_argument("--prefix-words", type=int, default=2500)
    p.add_argument("--gpu-power-limit-w", type=float, default=None)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    if not 2 <= args.rounds <= 50 or not 100 <= args.prefix_words <= 60000:
        p.error("--rounds is 2 to 50 and --prefix-words is 100 to 60000")
    client = BasaltClient(base_url=args.endpoint, api_key=args.api_key)
    from harness.eval.runner import collect_environment
    model = str(collect_environment(client, args.endpoint, gpu_power_limit_w=args.gpu_power_limit_w).get("model", ""))
    result = compare(client, args.rounds, args.prefix_words)
    text = report(result, args.rounds, args.prefix_words, args.gpu_power_limit_w, model)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
