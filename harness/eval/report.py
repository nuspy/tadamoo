"""Reproducible evaluation reports: what was run, on what, and what it cost, with the caveats attached."""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .runner import EvalResult


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval of a success rate, honest about small samples."""
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def inconclusive(result: EvalResult) -> bool:
    """A task nothing was learned from: no trial passed and every failure was the server's (RAM guard, restart), not the model's."""
    failed = [t for t in result.trials if not t.passed]
    return bool(failed) and len(failed) == len(result.trials) and all(t.stop_reason == "error" for t in failed)


def summarize(results: List[EvalResult]) -> Dict[str, Any]:
    trials = [t for r in results for t in r.trials]
    decided = [r for r in results if not inconclusive(r)]
    answered = [t for t in trials if t.stop_reason != "error"]
    n = len(trials)
    ok = sum(t.passed for t in trials)
    calls = sum(t.tool_calls for t in trials)
    invalid = sum(t.invalid_calls for t in trials)
    prompt = sum(t.prompt_tokens for t in trials)
    lo, hi = wilson(ok, n)
    return {
        "tasks": len(results), "trials": n, "trial_success": ok / n if n else 0.0, "trial_success_ci95": [round(lo, 3), round(hi, 3)],
        "pass_at_k": sum(1 for r in results if r.pass_at_k > 0), "pass_pow_k": sum(1 for r in results if r.pass_pow_k),
        "tool_calls": calls, "invalid_calls": invalid, "tool_call_validity": (calls - invalid) / calls if calls else None,
        "denied_calls": sum(t.denied_calls for t in trials),
        "mean_turns": statistics.fmean(t.turns for t in trials) if n else 0.0,
        "mean_seconds": statistics.fmean(t.seconds for t in trials) if n else 0.0,
        "median_seconds": statistics.median(t.seconds for t in trials) if n else 0.0,
        "prompt_tokens": prompt, "completion_tokens": sum(t.completion_tokens for t in trials),
        "cache_hit_rate": (sum(t.cached_tokens for t in trials) / prompt) if prompt else None,
        "crashed": sum(1 for t in trials if t.error and t.stop_reason != "error" and not t.turns),
        "server_errors": sum(1 for t in trials if t.stop_reason == "error"),
        "inconclusive_tasks": [r.task_id for r in results if inconclusive(r)],
        "decided_tasks": len(decided), "decided_pass_pow_k": sum(1 for r in decided if r.pass_pow_k),
        "answered_trial_success": (sum(t.passed for t in answered) / len(answered)) if answered else 0.0,
    }


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def generate_eval_report(
    results: List[EvalResult],
    model_name: Optional[str] = None,
    hardware: Optional[str] = None,
    environment: Optional[Dict[str, Any]] = None,
    task_set: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    env = environment or {}
    model = model_name or env.get("model") or "not recorded"
    gpus = env.get("gpus") or []
    hw = hardware or (", ".join(g.get("name", "GPU") for g in gpus) if gpus else "not recorded")
    limit = env.get("gpu_power_limit_w")
    s = summarize(results)
    lines = [
        "# Basalt harness evaluation",
        "",
        "> Development A/B only. A number from an agent's run is not a headline result; those come from the maintainer's own runs.",
        "> A difference smaller than about 3 points between two runs is noise until it is repeated.",
        "",
        "## What was run",
        "",
        f"- **Task set**: {(task_set or {}).get('name', 'unnamed')} {(task_set or {}).get('version', '')}".rstrip(),
        f"- **Model**: {model}",
        f"- **Hardware**: {hw}",
        f"- **GPU power limit**: {f'{limit} W' if limit else 'not recorded (pass --gpu-power-limit-w); numbers are not comparable without it'}",
        f"- **Harness**: {env.get('harness_version', 'unknown')}, commit {env.get('harness_commit', 'unknown')}{' (uncommitted changes)' if env.get('harness_dirty') else ''}",
        f"- **Endpoint**: {env.get('endpoint', 'unknown')}",
        f"- **Python / platform**: {env.get('python', '?')} / {env.get('platform', '?')}",
    ]
    for k, v in (config or {}).items():
        lines.append(f"- **{k}**: {v}")
    lines += [
        "",
        "## Result",
        "",
        f"- **Tasks**: {s['tasks']}, **trials**: {s['trials']}",
        f"- **Trial success**: {_pct(s['trial_success'])} (95% interval {s['trial_success_ci95'][0] * 100:.0f}-{s['trial_success_ci95'][1] * 100:.0f}%)",
        f"- **pass@k** (at least one trial passes): {s['pass_at_k']}/{s['tasks']}",
        f"- **pass^k** (every trial passes, the reliability that matters): {s['pass_pow_k']}/{s['tasks']}",
        f"- **Tool calls**: {s['tool_calls']}, valid {_pct(s['tool_call_validity'])}, refused by permissions {s['denied_calls']}",
        f"- **Cost per trial**: {s['mean_turns']:.1f} turns, {s['mean_seconds']:.1f} s on average ({s['median_seconds']:.1f} s median)",
        f"- **Tokens**: {s['prompt_tokens']} in, {s['completion_tokens']} out, prefix cache hit rate {_pct(s['cache_hit_rate'])}",
        f"- **Not model faults**: {s['server_errors']} server error(s), {s['crashed']} crashed trial(s)",
    ]
    if s["inconclusive_tasks"]:
        lines += [
            f"- **Inconclusive** (every failure was the server's, so nothing was learned about the model): "
            f"{', '.join(f'`{t}`' for t in s['inconclusive_tasks'])}. Without them: pass^k {s['decided_pass_pow_k']}/{s['decided_tasks']}, "
            f"success {_pct(s['answered_trial_success'])} of the trials the server answered. Run them again with `--only`.",
        ]
    lines += [
        "",
        "## By category",
        "",
        "| Category | Tasks | Trial success | pass^k |",
        "| :--- | :---: | :---: | :---: |",
    ]
    for cat in sorted({r.category for r in results}):
        rs = [r for r in results if r.category == cat]
        ts = [t for r in rs for t in r.trials]
        lines.append(f"| {cat} | {len(rs)} | {_pct(sum(t.passed for t in ts) / len(ts) if ts else 0)} | {sum(r.pass_pow_k for r in rs)}/{len(rs)} |")
    lines += ["", "## By task", "", "| Task | Category | k | Passed | pass^k | Turns | Calls (invalid) | Tokens in | Seconds | First failure |",
              "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |"]
    for r in results:
        n = max(len(r.trials), 1)
        first = next((t.failures[0] for t in r.trials if t.failures), "")
        lines.append(
            f"| `{r.task_id}` | {r.category} | {r.k_trials} | {sum(t.passed for t in r.trials)} | {'inconclusive' if inconclusive(r) else 'yes' if r.pass_pow_k else 'no'} | "
            f"{sum(t.turns for t in r.trials) / n:.1f} | {sum(t.tool_calls for t in r.trials)} ({sum(t.invalid_calls for t in r.trials)}) | "
            f"{sum(t.prompt_tokens for t in r.trials) // n} | {sum(t.seconds for t in r.trials) / n:.1f} | {first[:90].replace('|', '/')} |")
    lines += ["", "Read the transcripts of failed trials before trusting a low score: a task that fails every time is usually a broken task, not a weak model.", ""]
    return "\n".join(lines)


def results_to_json(results: List[EvalResult], environment: Optional[Dict[str, Any]] = None, task_set: Optional[Dict[str, Any]] = None,
                    config: Optional[Dict[str, Any]] = None) -> str:
    return json.dumps({"environment": environment or {}, "task_set": task_set or {}, "config": config or {}, "summary": summarize(results),
                       "results": [asdict(r) for r in results]}, indent=2, ensure_ascii=False)
