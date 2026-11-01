"""python -m harness.eval.cases <list | e1 ... e8> [options]: runs one case study and writes its report (docs/experiments.md)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from harness.client import BasaltClient
from harness.eval.report import k_warning
from harness.eval.runner import collect_environment

from . import MODULES, load
from harness.eval.resume import Journal, ResumeMismatch

from .core import CONTROL_SUFFIX, Context, render_report, results_json, run_experiment, run_fingerprint


def _temperature(text: str) -> Optional[float]:
    if text.strip().lower() == "model":
        return None
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("a number, or 'model'") from None


def show_list() -> int:
    for key in MODULES:
        exp = load(key)
        print(f"{key}  {exp.title}\n    {exp.question}")
        for arm in exp.arms:
            print(f"    arm {arm.name:<6} {arm.label}" + (f"  [{arm.flags}]" if arm.flags else ""))
        print(f"    rule: {exp.rule.text}\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m harness.eval.cases", description="Runs one case study of the harness (E1 to E8) and reports it with its A/A control and decision rule")
    p.add_argument("experiment", choices=["list", *MODULES], help="list shows every experiment, its arms and its rule")
    p.add_argument("--endpoint", default="http://127.0.0.1:7232")
    p.add_argument("--api-key", default=None)
    p.add_argument("--k", type=int, default=None, help="repetitions of every task under every arm (5 or more for a rule worth reading)")
    p.add_argument("--arms", default="", help="only these arms, comma separated (names as in `list`); the A/A control of the baseline is always run")
    p.add_argument("--only", action="append", default=[], metavar="TASK_ID")
    p.add_argument("--temperature", type=_temperature, default=0.6, help="above 0 so that repetitions differ; 'model' sends none")
    p.add_argument("--gpu-power-limit-w", type=float, default=None, help="the GPU power limit of this run: it labels every number, so a run that uses the model needs it")
    p.add_argument("--out", default=None, help="write the Markdown report here")
    p.add_argument("--json", default=None, help="write the full results here")
    p.add_argument("--keep", default=None, help="keep every trial's workspace in this folder, for reading failures")
    p.add_argument("--window", type=int, default=None, help="E5: the context window the agent plans for, in tokens (default 16384)")
    p.add_argument("--server-setup", default="", help="E7, E8: how the server was started (for example 'parallel=1', or 'side-slot=30000' for E8); printed in the report")
    p.add_argument("--deadline-s", type=float, default=None, help="E3: seconds after which a turn counts as never ending (default 1.5)")
    p.add_argument("--resume", default=None, metavar="DIR", help="the run's folder: every finished trial is written there as it ends, and a run started again with this folder "
                   "reuses the valid ones if nothing it depends on changed (code, model file, settings, arms, k); invalid trials are redone")
    p.add_argument("--model-file", default=None, help="the model file the server loaded: its name and size are part of what --resume checks")
    args = p.parse_args(argv)
    if args.experiment == "list":
        return show_list()
    exp = load(args.experiment)
    k = args.k or exp.default_k
    if k < 1:
        p.error("--k must be at least 1")
    warning = k_warning(k)
    if warning:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    if exp.needs_model and not args.gpu_power_limit_w:
        p.error(f"{exp.id} runs the model: give --gpu-power-limit-w, since every number is labelled with the GPU power limit")
    wanted = [a for a in args.arms.split(",") if a]
    unknown = [a for a in wanted if a not in {x.name for x in exp.arms}]
    if unknown:
        p.error(f"no arm {', '.join(unknown)}; the arms are {', '.join(x.name for x in exp.arms)}")
    if wanted and exp.rule.baseline not in wanted:
        wanted.append(exp.rule.baseline)
    if wanted and exp.rule.baseline in wanted and exp.rule.baseline + CONTROL_SUFFIX not in wanted:
        wanted.append(exp.rule.baseline + CONTROL_SUFFIX)
    extra = {key: value for key, value in (("window", args.window), ("deadline_s", args.deadline_s)) if value is not None}
    environment = {}
    if exp.needs_model:
        client = BasaltClient(base_url=args.endpoint, api_key=args.api_key)
        env = collect_environment(client, args.endpoint, gpu_power_limit_w=args.gpu_power_limit_w)
        environment = {"Model": env.get("model"), "Hardware": ", ".join(g.get("name", "GPU") for g in env.get("gpus") or []) or "not recorded",
                       "Harness": f"{env.get('harness_version')}, commit {env.get('harness_commit')}" + (" (uncommitted changes)" if env.get("harness_dirty") else ""),
                       "Endpoint": args.endpoint, **({"Server setup": args.server_setup} if args.server_setup else {})}
    else:
        client = None
    ctx = Context(client=client, endpoint=args.endpoint, api_key=args.api_key, power_limit_w=args.gpu_power_limit_w, temperature=args.temperature,
                  keep=Path(args.keep) if args.keep else None, extra=extra, plumbing=not exp.needs_model)

    def show(t) -> None:
        print(f"{'PASS' if t.success else 'FAIL' if not t.invalid else 'INVALID':<8} {t.arm:<8} {t.task:<28} #{t.rep}", file=sys.stderr, flush=True)

    journal = None
    if args.resume:
        try:
            journal = Journal(Path(args.resume), run_fingerprint(exp, ctx, k, wanted, args.only, environment, args.model_file))
        except ResumeMismatch as exc:
            p.error(str(exc))
    trials = run_experiment(exp, ctx, k, wanted, args.only, on_trial=show, journal=journal)
    if journal and journal.reused:
        print(f"{journal.reused} finished trial(s) reused from {args.resume}", file=sys.stderr, flush=True)
    report = render_report(exp, ctx, trials, k, environment)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(results_json(exp, ctx, trials, k, environment), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
