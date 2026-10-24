"""python -m harness.eval --set core --k 5 --endpoint http://127.0.0.1:7232 --out report.md"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from harness.client import BasaltClient

from .report import generate_eval_report, results_to_json
from .runner import EvalRunner, collect_environment
from .tasks import expand_strategies, load_task_set


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m harness.eval", description="Runs a task set against a Basalt server and reports pass^k, cost and cache use")
    p.add_argument("--set", default="core", help="a set name (core, toolcalls) or the path of a JSON file")
    p.add_argument("--k", type=int, default=5, help="repetitions per task (5 or more for a meaningful pass^k)")
    p.add_argument("--endpoint", default="http://127.0.0.1:7232")
    p.add_argument("--api-key", default=None)
    p.add_argument("--temperature", type=float, default=0.6, help="above 0 so that repetitions differ; each uses its own seed")
    p.add_argument("--only", action="append", default=[], metavar="TASK_ID")
    p.add_argument("--strategies", default="none,rag,cag", help="for tasks that use reference documents: which ways to offer them")
    p.add_argument("--gpu-power-limit-w", type=float, default=None, help="the GPU power limit this run used (it labels every number)")
    p.add_argument("--out", default=None, help="write the Markdown report here")
    p.add_argument("--json", default=None, help="write the full results here")
    p.add_argument("--keep-workspaces", default=None, help="keep every trial's workspace in this folder, for reading failures")
    args = p.parse_args(argv)
    if args.k < 1:
        p.error("--k must be at least 1")

    meta, tasks = load_task_set(args.set)
    tasks = expand_strategies(tasks, [s for s in args.strategies.split(",") if s])
    if args.only:
        tasks = [t for t in tasks if t.id in args.only]
        if not tasks:
            p.error("no task matches --only")
    client = BasaltClient(base_url=args.endpoint, api_key=args.api_key)
    env = collect_environment(client, args.endpoint, gpu_power_limit_w=args.gpu_power_limit_w)
    runner = EvalRunner(client=client, temperature=args.temperature, keep_workspaces=Path(args.keep_workspaces) if args.keep_workspaces else None)
    config = {"k": args.k, "temperature": args.temperature, "seeds": "1000 + repetition", "max turns": "per task"}

    def show(r):
        print(f"{'PASS' if r.pass_pow_k else 'FAIL'}  {r.task_id:<34} {sum(t.passed for t in r.trials)}/{r.k_trials}", file=sys.stderr, flush=True)

    results = runner.run_set(tasks, args.k, on_result=show)
    report = generate_eval_report(results, environment=env, task_set=meta, config=config)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(results_to_json(results, env, meta, config), encoding="utf-8")
    return 0 if all(r.pass_pow_k for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
