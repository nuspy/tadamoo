"""Live test, part B: how long a reply the model writes when it is asked for a long text, measured on the running server.

  python tests/live/b_output_limits.py run  [--thinking on|off] [--lengths 1000,2000,4000,8000] [--samples 10] [--max-tokens 60000] [--tag NAME]
  python tests/live/b_output_limits.py table [--tag NAME]

Each run asks for a report of N words with an introduction, numbered sections and a conclusion, on one of ten subjects. What is counted is the
ANSWER text alone: its tokens by the server's tokenizer (the thinking is counted apart and never added), its words, the finish reason, and the
ratio of words written to words asked. A reply that ends with `stop` before the asked length is the model closing its turn, not a limit; `length`
is a limit. Results: <LIVE_OUT>/b_<tag>.jsonl. The GPU's power limit is written with every run."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

from common import OUT, chat, gpu, record, status

SUBJECTS = [
    "la storia della navigazione nel Mediterraneo", "il funzionamento dei mercati dell'energia elettrica", "le cause e le conseguenze delle carestie nella storia",
    "l'architettura delle cattedrali gotiche", "le basi dell'immunologia e dei vaccini", "l'evoluzione dei linguaggi di programmazione",
    "la gestione dell'acqua nelle grandi città antiche", "la nascita e la diffusione della stampa", "le tecniche di coltivazione sostenibile",
    "la teoria dei giochi e le sue applicazioni economiche",
]
PROMPT = ("Scrivi una relazione di circa {n} parole su {subject}. Deve avere un'introduzione, sezioni numerate con titolo e una conclusione. "
          "Scrivi il testo completo, senza chiedermi altro.")


def cmd_run(a: argparse.Namespace) -> None:
    lengths = [int(x) for x in a.lengths.split(",")]
    tag = a.tag or f"think_{a.thinking}"
    st = status()
    record(f"b_{tag}", {"test": "start", "model": st.get("model"), "context": (st.get("context") or {}).get("native"), "thinking": a.thinking, "max_tokens": a.max_tokens,
                        "samples": a.samples, "lengths": lengths, **gpu()})
    for rep in range(a.samples):
        for n in lengths:
            subject = SUBJECTS[rep % len(SUBJECTS)]
            r = chat([{"role": "user", "content": PROMPT.format(n=n, subject=subject)}], max_tokens=a.max_tokens, thinking=(a.thinking == "on"), timeout=3600)
            record(f"b_{tag}", {"asked_words": n, "rep": rep, "subject": subject, "finish": r["finish"], "answer_tokens": r["answer_tokens"], "words": r["words"],
                                "ratio": round(r["words"] / n, 3), "thinking_tokens": r["thinking_tokens"], "completion_tokens": r["completion_tokens"],
                                "seconds": r["seconds"], "power_limit": gpu().get("power_limit")})


def cmd_table(a: argparse.Namespace) -> None:
    tag = a.tag or f"think_{a.thinking}"
    rows = [json.loads(x) for x in open(OUT / f"b_{tag}.jsonl", encoding="utf-8") if '"asked_words"' in x]
    print(f"{tag}: {len(rows)} runs. Answer tokens exclude the thinking.")
    print("asked words | runs | answer tokens (min / median / max) | words written / asked (median) | finish stop / length | thinking tokens (median)")
    for n in sorted({r["asked_words"] for r in rows}):
        rs = [r for r in rows if r["asked_words"] == n]
        t = [r["answer_tokens"] for r in rs]
        print(f"{n:>11} | {len(rs):>4} | {min(t)} / {int(statistics.median(t))} / {max(t)} | {statistics.median([r['ratio'] for r in rs]):.2f} | "
              f"{sum(r['finish'] == 'stop' for r in rs)} / {sum(r['finish'] == 'length' for r in rs)} | {int(statistics.median([r['thinking_tokens'] for r in rs]))}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=("run", "table"))
    p.add_argument("--thinking", choices=("on", "off"), default="on")
    p.add_argument("--lengths", default="1000,2000,4000,8000")
    p.add_argument("--samples", type=int, default=10)
    p.add_argument("--max-tokens", type=int, default=60000)
    p.add_argument("--tag", default="")
    args = p.parse_args()
    (cmd_run if args.cmd == "run" else cmd_table)(args)
