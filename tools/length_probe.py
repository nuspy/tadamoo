"""How long an answer gets for a request of a given length, against the end-of-turn bias, with and without tools.

Chat mode sends /v1/chat/completions to a running server; agent mode runs `python -m tadamoo` (the same agent loop and tools as the Chat with
tools) in an empty throwaway folder. A run starts only when the server has been idle for a while, so a person's own request is never held up.
Every run is appended to a JSONL file as it ends, so an interrupted night keeps what it measured; a markdown summary is written at the end.

  python tools/length_probe.py --endpoint http://127.0.0.1:7232 --out E:/Modelli/basalt/night/length
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
PROMPT = "Scrivi una storia di {n} parole su Atra Hasis e il tappeto magico e mostrala nella risposta."
WORD = re.compile(r"\S+")
SENTENCE = re.compile(r"[^.!?\n]+[.!?]")


def get_json(url: str, timeout: float = 10) -> Optional[dict]:
    try:
        return json.load(urllib.request.urlopen(url, timeout=timeout))
    except (OSError, ValueError):
        return None


def sessions_busy() -> bool:
    """True while a Chat conversation with tools is working (its record says busy, starting or asking)."""
    import os
    path = Path(os.path.expandvars(r"%APPDATA%")) / "BasaltPannello" / "journal" / "sessions" / "agent_sessions.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")).get("sessions", [])
    except (OSError, ValueError):
        return False
    return any(r.get("state") in ("busy", "starting", "asking") for r in records)


def wait_idle(endpoint: str, quiet_s: float, deadline: float, watch_sessions: bool = True) -> bool:
    """True once the server holds a model, no request is in flight, none came for `quiet_s` seconds and no Chat conversation is working;
    False at the deadline."""
    while time.time() < deadline:
        st = get_json(endpoint + "/v1/status")
        if st and st.get("loaded") and not st.get("loading"):
            act = st.get("activity") or {}
            last = act.get("last_request_at") or 0
            if not act.get("in_flight") and time.time() - last >= quiet_s and not (watch_sessions and sessions_busy()):
                return True
        time.sleep(10)
    return False


def shape(text: str) -> Dict[str, Any]:
    """Words, and two signs of a text that has begun to repeat itself."""
    words = WORD.findall(text)
    sentences = [s.strip().lower() for s in SENTENCE.findall(text) if len(s.split()) >= 5]
    grams = [" ".join(words[i:i + 6]).lower() for i in range(0, max(0, len(words) - 5))]
    return {"words": len(words),
            "repeated_sentences": round(1 - len(set(sentences)) / len(sentences), 3) if sentences else 0.0,
            "repeated_6grams": round(1 - len(set(grams)) / len(grams), 3) if grams else 0.0}


def run_chat(endpoint: str, n_words: int, bias: float, seed: int, max_tokens: int, timeout: float) -> Dict[str, Any]:
    body = {"messages": [{"role": "user", "content": PROMPT.format(n=n_words)}], "temperature": 0.6, "top_p": 0.95, "top_k": 20,
            "seed": seed, "max_tokens": max_tokens, "end_turn_bias": bias, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(endpoint + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        reply = json.load(urllib.request.urlopen(req, timeout=timeout))
    except (OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "seconds": round(time.time() - t0, 1)}
    choice = reply["choices"][0]
    text = choice["message"].get("content") or ""
    usage = reply.get("usage") or {}
    return {"finish": choice.get("finish_reason"), "completion_tokens": usage.get("completion_tokens"), "seconds": round(time.time() - t0, 1),
            "tool_calls": len(choice["message"].get("tool_calls") or []), "tail": text[-160:], **shape(text)}


def run_agent(endpoint: str, n_words: int, bias: float, max_turns: int, timeout: float) -> Dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from tadamoo.headless import config_from_args, parse_args
    from tadamoo.stack import build_agent
    with tempfile.TemporaryDirectory(prefix="length-probe-") as work:
        args = parse_args(["-p", PROMPT.format(n=n_words), "--workspace", work, "--endpoint", endpoint, "--max-turns", str(max_turns),
                           "--permission-mode", "default", "--end-turn-bias", str(bias), "--no-report", "--no-memory",
                           "--timeout-seconds", str(int(timeout))])
        t0 = time.time()
        built = build_agent(config_from_args(args))
        try:
            turns = built.agent.run(PROMPT.format(n=n_words))
            agent = built.agent
            denials = built.permissions.denied
        except Exception as exc:  # noqa: BLE001 - one run failing must not end the night
            return {"error": f"{type(exc).__name__}: {exc}", "seconds": round(time.time() - t0, 1)}
        finally:
            built.close()
        per_turn = [len(t_.tool_calls) for t_ in turns]
        final = next((t_.assistant_content for t_ in reversed(turns) if not t_.tool_calls), "") or ""
        return {"stop_reason": agent.stop_reason, "turns": len(turns), "completion_tokens": sum(t_.completion_tokens for t_ in turns),
                "seconds": round(time.time() - t0, 1), "tool_calls": sum(per_turn), "max_calls_in_a_turn": max(per_turn) if per_turn else 0,
                "denials": denials, "error": agent.error or None, "tail": final[-160:], **shape(final)}


def summary(rows: List[dict]) -> str:
    out = ["# Answer length against the end-of-turn bias", "",
           "| mode | words asked | bias | runs | words (min / median / max) | finish or stop | tool calls (median) | most calls in a turn | repeated 6-grams (median) |",
           "|---|---|---|---|---|---|---|---|---|"]
    keys = sorted({(r["mode"], r["asked"], r["bias"]) for r in rows}, key=lambda k: (k[0], -k[1], -k[2]))
    for mode, asked, bias in keys:
        rs = [r for r in rows if (r["mode"], r["asked"], r["bias"]) == (mode, asked, bias) and not r.get("error")]
        if not rs:
            out.append(f"| {mode} | {asked} | {bias} | 0 (errors) | | | | | |")
            continue
        words = sorted(r["words"] for r in rs)
        calls = sorted(r.get("tool_calls") or 0 for r in rs)
        grams = sorted(r["repeated_6grams"] for r in rs)
        ends = ", ".join(sorted({str(r.get("finish") or r.get("stop_reason")) for r in rs}))
        most = max(r.get("max_calls_in_a_turn") or 0 for r in rs)
        out.append(f"| {mode} | {asked} | {bias} | {len(rs)} | {words[0]} / {words[len(words) // 2]} / {words[-1]} | {ends} | {calls[len(calls) // 2]} | {most} | {grams[len(grams) // 2]} |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--endpoint", default="http://127.0.0.1:7232")
    ap.add_argument("--out", required=True, help="folder for results.jsonl and summary.md")
    ap.add_argument("--biases", default="0,-1,-2,-3,-5,-10,-20")
    ap.add_argument("--modes", default="chat,agent")
    ap.add_argument("--words", default="10000,6000,3000", help="lengths asked; the first gets the full repetitions, the others one")
    ap.add_argument("--chat-seeds", type=int, default=3)
    ap.add_argument("--agent-reps", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=24000)
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--chat-timeout", type=float, default=900)
    ap.add_argument("--agent-timeout", type=float, default=900)
    ap.add_argument("--quiet-s", type=float, default=30, help="seconds the server must have been idle before a run starts")
    ap.add_argument("--hours", type=float, default=6, help="stop starting runs after this long")
    ap.add_argument("--ignore-sessions", action="store_true", help="do not wait for the Chat's conversations (a registry left busy by a closed app)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    end = time.time() + a.hours * 3600
    biases = [float(x) for x in a.biases.split(",")]
    lengths = [int(x) for x in a.words.split(",")]
    plan = []
    for li, n in enumerate(lengths):
        for mode in a.modes.split(","):
            reps = (a.chat_seeds if mode == "chat" else a.agent_reps) if li == 0 else 1
            for rep in range(reps):
                for bias in biases:                      # interleaved: every value meets the same drift
                    plan.append((mode, n, bias, rep))
    rows: List[dict] = []
    for i, (mode, n, bias, rep) in enumerate(plan, 1):
        if not wait_idle(a.endpoint, a.quiet_s, end, not a.ignore_sessions):
            print("stopping: the time given is over or the server holds no model", flush=True)
            break
        print(f"[{i}/{len(plan)}] {mode} words={n} bias={bias} rep={rep}", flush=True)
        r = (run_chat(a.endpoint, n, bias, 1000 + rep, a.max_tokens, a.chat_timeout) if mode == "chat"
             else run_agent(a.endpoint, n, bias, a.max_turns, a.agent_timeout))
        row = {"mode": mode, "asked": n, "bias": bias, "rep": rep, "at": time.strftime("%Y-%m-%d %H:%M:%S"), **r}
        rows.append(row)
        with results.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print("   ", {k: row.get(k) for k in ("words", "finish", "stop_reason", "completion_tokens", "tool_calls", "seconds", "error")}, flush=True)
    (out / "summary.md").write_text(summary(rows), encoding="utf-8")
    print(summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
