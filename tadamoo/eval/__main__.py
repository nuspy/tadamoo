"""python -m tadamoo.eval --set core --k 5 --endpoint http://127.0.0.1:7232 --out report.md

A public benchmark is read from a folder you downloaded yourself (nothing is fetched): --bfcl DIR, --terminal-bench DIR or --swebench DIR
(see tadamoo/eval/benchmarks.py for the layouts). --compare-strategies runs the reference-documents set with none, RAG and CAG side by side."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from tadamoo.client import BasaltClient

from . import benchmarks
from .report import generate_eval_report, k_warning, results_to_json
from .resume import Journal, ResumeMismatch
from .runner import EvalRunner, collect_environment
from .serverlog import ServerTimings
from .tasks import expand_strategies, load_task_set


def _temperature(text: str) -> Optional[float]:
    """A temperature, or `model` for none (the model file's own sampling)."""
    if text.strip().lower() == "model":
        return None
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("a number, or 'model'") from None


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tadamoo.eval", description="Runs a task set against a Basalt server and reports pass^k, cost and cache use")
    p.add_argument("--set", default="core", help="a set name (core, toolcalls) or the path of a JSON file")
    p.add_argument("--k", type=int, default=5, help="repetitions per task (5 or more for a meaningful pass^k)")
    p.add_argument("--endpoint", default="http://127.0.0.1:7232")
    p.add_argument("--api-key", default=None)
    p.add_argument("--temperature", type=_temperature, default=0.6,
                   help="above 0 so that repetitions differ, each with its own seed; 'model' sends none, so the model file's own sampling applies")
    p.add_argument("--instruction-blocks", action="store_true", help="add the working rules of tadamoo/prompt_blocks.py to the system prompt (compare a run with and without)")
    p.add_argument("--only", action="append", default=[], metavar="TASK_ID")
    p.add_argument("--strategies", default="none,rag,cag", help="for tasks that use reference documents: which ways to offer them")
    p.add_argument("--gpu-power-limit-w", type=float, default=None, help="the GPU power limit this run used (it labels every number)")
    p.add_argument("--out", default=None, help="write the Markdown report here")
    p.add_argument("--json", default=None, help="write the full results here")
    p.add_argument("--council", action="store_true", help="offer ask_council (the council must be set up on the Providers page); run the set with and without it to compare")
    p.add_argument("--keep-workspaces", default=None, help="keep every trial's workspace in this folder, for reading failures")
    p.add_argument("--compare-strategies", action="store_true", help="the 'knowledge' set with none, rag and cag (same as --set knowledge --strategies none,rag,cag)")
    p.add_argument("--bfcl", metavar="DIR", default=None, help="run BFCL from this folder (BFCL_v<n>_<category>.json and possible_answer/)")
    p.add_argument("--bfcl-categories", default="simple,multiple,parallel,parallel_multiple,irrelevance", help="which BFCL categories to load")
    p.add_argument("--terminal-bench", metavar="DIR", default=None, help="run Terminal-Bench tasks from this folder (a folder per task with task.yaml and tests/); use --only to pick a subset")
    p.add_argument("--swebench", metavar="DIR", default=None, help="run SWE-bench Pro instances from this folder (instances.jsonl and repos/<instance_id>)")
    p.add_argument("--swebench-test-command", default="{python} -m pytest -q", help="the command that runs the instance's tests; the test ids are added at its end")
    p.add_argument("--limit", type=int, default=None, help="load at most this many tasks of a public benchmark")
    p.add_argument("--no-server-timings", action="store_true", help="do not read the server's request log for time to first token and cache use")
    p.add_argument("--resume", default=None, metavar="DIR", help="the run's folder: every finished trial is written there as it ends, and a run started again with this folder "
                   "reuses the valid ones if nothing it depends on changed (code, model file, settings, tasks, k); invalid trials are redone")
    p.add_argument("--model-file", default=None, help="the model file the server loaded: its name and size are part of what --resume checks")
    args = p.parse_args(argv)
    if args.k < 1:
        p.error("--k must be at least 1")
    warning = k_warning(args.k)
    if warning:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    chosen = [name for name in ("bfcl", "terminal_bench", "swebench") if getattr(args, name)]
    if len(chosen) > 1:
        p.error("choose one of --bfcl, --terminal-bench and --swebench")
    try:
        if args.bfcl:
            loaded = benchmarks.load_bfcl(args.bfcl, [c for c in args.bfcl_categories.split(",") if c], args.limit)
        elif args.terminal_bench:
            loaded = benchmarks.load_terminal_bench(args.terminal_bench, args.limit)
        elif args.swebench:
            loaded = benchmarks.load_swebench(args.swebench, args.limit, args.swebench_test_command)
        else:
            loaded = None
    except ValueError as exc:
        p.error(str(exc))
    if loaded is not None:
        meta, tasks = loaded.meta, loaded.tasks
        for note in loaded.skipped[:20]:
            print(f"note: {note}", file=sys.stderr, flush=True)
        if len(loaded.skipped) > 20:
            print(f"note: and {len(loaded.skipped) - 20} more left out", file=sys.stderr, flush=True)
    else:
        meta, tasks = load_task_set("knowledge" if args.compare_strategies else args.set)
        tasks = expand_strategies(tasks, ["none", "rag", "cag"] if args.compare_strategies else [s for s in args.strategies.split(",") if s])
    if args.only:
        tasks = [t for t in tasks if t.id in args.only]
        if not tasks:
            p.error("no task matches --only")
    client = BasaltClient(base_url=args.endpoint, api_key=args.api_key)
    env = collect_environment(client, args.endpoint, gpu_power_limit_w=args.gpu_power_limit_w)
    timings = None if args.no_server_timings else ServerTimings(args.endpoint, args.api_key)
    runner = EvalRunner(client=client, temperature=args.temperature, keep_workspaces=Path(args.keep_workspaces) if args.keep_workspaces else None, council=args.council,
                        timings=timings, instruction_blocks=args.instruction_blocks)
    if args.resume:
        try:
            runner.journal = Journal(Path(args.resume), runner.fingerprint(tasks, args.k, env, args.model_file))
        except ResumeMismatch as exc:
            p.error(str(exc))
    config = {"k": args.k, "temperature": "the model file's own" if args.temperature is None else args.temperature,
              "instruction blocks": "on" if args.instruction_blocks else "off", "seeds": "1000 + repetition", "max turns": "per task", "council": "on" if args.council else "off",
              "server timings": "off" if timings is None else "from the request log"}

    def show(r):
        print(f"{'PASS' if r.pass_pow_k else 'FAIL'}  {r.task_id:<34} {sum(t.passed for t in r.trials)}/{r.k_trials}", file=sys.stderr, flush=True)

    results = runner.run_set(tasks, args.k, on_result=show)
    if runner.journal and runner.journal.reused:
        print(f"{runner.journal.reused} finished trial(s) reused from {args.resume}", file=sys.stderr, flush=True)
    report = generate_eval_report(results, environment=env, task_set=meta, config=config)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(results_to_json(results, env, meta, config), encoding="utf-8")
    return 0 if all(r.pass_pow_k for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
