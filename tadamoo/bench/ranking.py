"""Where a result would stand among published entries. Nothing here reaches the network: the caller passes the text of a downloaded results file."""
from __future__ import annotations

import csv
import io
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from tadamoo.eval.report import wilson

NAME_COLUMNS = ("name", "model", "entry", "agent", "submission", "harness")


@dataclass
class Entry:
    name: str
    metrics: Dict[str, float] = field(default_factory=dict)
    raw: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "metrics": self.metrics}


def _column(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def _number(text: str) -> Optional[float]:
    t = (text or "").strip().rstrip("%").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def load_csv(source: Union[str, Path]) -> List[Entry]:
    """Entries from a results CSV (a path or the text itself). The name is the first of the usual name columns (else the first column); every column
    that holds a number becomes a metric, with its header lower-cased and joined by underscores. A percent sign is dropped and the number kept."""
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source and "," not in source and Path(source).is_file()):
        text = Path(source).read_text(encoding="utf-8-sig")
    else:
        text = str(source).lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return []
    columns = {_column(h): h for h in rows[0] if h}
    name_col = next((columns[c] for c in NAME_COLUMNS if c in columns), next(iter(rows[0])))
    out = []
    for row in rows:
        metrics = {}
        for h, v in row.items():
            if h and h != name_col and (n := _number(v if isinstance(v, str) else "")) is not None:
                metrics[_column(h)] = n
        out.append(Entry((row.get(name_col) or "").strip(), metrics, {str(k): str(v) for k, v in row.items() if k}))
    return out


def interval(successes: int, n: int) -> Dict[str, float]:
    """The 95% Wilson interval of a resolution rate, as fractions."""
    low, high = wilson(successes, n)
    return {"low": low, "high": high, "n": n}


def _scale(values: List[float], ours: float, scale: str) -> float:
    """Our value in the units of the entries ('fraction' 0..1 or 'percent' 0..100)."""
    if scale == "auto":
        scale = "percent" if values and max(values) > 1.0 else "fraction"
    if scale == "percent" and ours <= 1.0:
        return ours * 100.0
    if scale == "fraction" and ours > 1.0:
        return ours / 100.0
    return ours


def rank(our_metrics: Mapping[str, float], entries: List[Entry], metric: str, scale: str = "auto") -> Dict[str, Any]:
    """Our place among the entries on one metric: `position` (1 + entries that score strictly higher), `of` (entries plus us), `percentile` (share of
    entries we beat, ties counting half) and the nearest entry above and below. `scale` says whether the entries hold fractions or percents ('auto' guesses)."""
    scored = [e for e in entries if metric in e.metrics]
    if metric not in our_metrics:
        raise KeyError(f"our results have no metric {metric!r}; they hold {', '.join(sorted(our_metrics)) or 'none'}")
    values = [e.metrics[metric] for e in scored]
    ours = _scale(values, float(our_metrics[metric]), scale)
    above = sorted((e for e in scored if e.metrics[metric] > ours), key=lambda e: e.metrics[metric])
    below = sorted((e for e in scored if e.metrics[metric] < ours), key=lambda e: -e.metrics[metric])
    equal = sum(1 for v in values if v == ours)
    n = len(scored)
    pick = lambda e: None if e is None else {"name": e.name, "value": e.metrics[metric]}
    return {"metric": metric, "our_value": ours, "position": 1 + len(above), "of": n + 1,
            "percentile": None if not n else round(100.0 * (len(below) + 0.5 * equal) / n, 1),
            "nearest_above": pick(above[0] if above else None), "nearest_below": pick(below[0] if below else None),
            "entries_compared": n, "entries_without_metric": len(entries) - n}


def simulate(our_metrics: Mapping[str, float], entries: List[Entry], metric: str, *, official_judge: bool, successes: Optional[int] = None,
             trials: Optional[int] = None, scale: str = "auto") -> Dict[str, Any]:
    """The preview shown before publishing. `basis` says how much it can be trusted: 'entries' (compared with published entries), 'local_only' (no entries
    could be downloaded: only our own score and its interval). A score made without the official judge is marked provisional."""
    out: Dict[str, Any] = {"provisional": not official_judge, "metric": metric, "our_value": our_metrics.get(metric)}
    if successes is not None and trials:
        out["interval"] = interval(successes, trials)
    if entries:
        out.update(rank(our_metrics, entries, metric, scale))
        out["basis"] = "entries"
        if "interval" in out:
            lo = rank({metric: out["interval"]["low"]}, entries, metric, scale)
            hi = rank({metric: out["interval"]["high"]}, entries, metric, scale)
            out["position_range"] = {"best": hi["position"], "worst": lo["position"]}
        out["note"] = "Compared with the entries in the downloaded results file."
    else:
        out.update({"position": None, "of": None, "percentile": None, "nearest_above": None, "nearest_below": None, "basis": "local_only"})
        out["note"] = "No published entries could be downloaded, so the place in the ranking cannot be shown. This is the benchmark's own score computed locally."
    if out["provisional"]:
        out["note"] += " Unofficial judge: not comparable with the board; the place is provisional."
    return out


# -- comparing our own runs ---------------------------------------------------------------------------------------------
# Which of several runs of one benchmark is better (two arms of the harness, before and after a change). Each benchmark is judged by its own metric and with the
# interval that fits how its score is made; a winner is named only when its interval lies wholly above every other run's, otherwise the runs are tied.

@dataclass(frozen=True)
class Policy:
    metric: str                  # the key of a run's metrics that is compared
    interval: str                # wilson_trials | task_mean | bootstrap_tasks
    unit: str                    # what one sample of the interval is, in words


POLICIES: Dict[str, Policy] = {
    "terminal-bench-2": Policy("resolution_rate", "task_mean", "task (the mean of each task's trials)"),
    "swe-bench-verified": Policy("resolution_rate", "wilson_trials", "instance"),
    "clawbench": Policy("reward_rate", "wilson_trials", "task"),
    "bfcl-v4": Policy("accuracy", "wilson_trials", "case"),
    "tau3-bench": Policy("pass_hat_k", "bootstrap_tasks", "task"),
}
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 20261101


def _task_rates(per_task: Mapping[str, Sequence[Any]]) -> List[float]:
    return [sum(1 for x in v if x) / len(v) for v in per_task.values() if len(v)]


def _pass_hat_k(trials: Sequence[Any], k: int) -> float:
    n, c = len(trials), sum(1 for x in trials if x)
    return math.comb(c, k) / math.comb(n, k) if 0 < k <= n else 0.0


def run_interval(policy: Policy, run: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The 95% interval of a run's metric by its benchmark's method, or None when the run does not carry what the method needs."""
    per_task = run.get("per_task") or {}
    value = (run.get("metrics") or {}).get(policy.metric)
    if policy.interval == "task_mean" and per_task:
        rates = _task_rates(per_task)
        t = len(rates)
        mean = sum(rates) / t
        if t >= 2 and any(abs(r - mean) > 1e-12 for r in rates):
            se = math.sqrt(sum((r - mean) ** 2 for r in rates) / (t - 1) / t)
            return {"low": max(0.0, mean - 1.96 * se), "high": min(1.0, mean + 1.96 * se), "n": t, "basis": "tasks"}
        low, high = wilson(round(mean * t), t)               # every task alike: the spread says nothing, so the small-sample interval of the tasks
        return {"low": low, "high": high, "n": t, "basis": "tasks"}
    if policy.interval == "bootstrap_tasks" and per_task:
        k = int(run.get("k") or max(len(v) for v in per_task.values()))
        scores = [_pass_hat_k(v, k) for v in per_task.values() if len(v) >= k]
        if len(scores) >= 2:
            rng = random.Random(BOOTSTRAP_SEED)
            means = sorted(sum(rng.choice(scores) for _ in scores) / len(scores) for _ in range(BOOTSTRAP_DRAWS))
            return {"low": means[int(0.025 * BOOTSTRAP_DRAWS)], "high": means[int(0.975 * BOOTSTRAP_DRAWS) - 1], "n": len(scores), "basis": "tasks"}
        return None
    if policy.interval in ("wilson_trials", "task_mean", "bootstrap_tasks"):
        n = run.get("trials")
        if n and value is not None:
            s = run.get("successes")
            s = round(float(value) * n) if s is None else s
            low, high = wilson(int(s), int(n))
            return {"low": low, "high": high, "n": int(n), "basis": "trials"}
    return None


def mcnemar_p(a: Mapping[str, Sequence[Any]], b: Mapping[str, Sequence[Any]]) -> Optional[float]:
    """The exact two-sided p of the difference between two runs on the same tasks (each task counted solved when most of its trials passed). Shown beside the
    verdict, never used for it."""
    tasks = sorted(set(a) & set(b))
    if not tasks or set(a) != set(b):
        return None
    solved = lambda v: sum(1 for x in v if x) * 2 > len(v)
    only_a = sum(1 for t in tasks if solved(a[t]) and not solved(b[t]))
    only_b = sum(1 for t in tasks if solved(b[t]) and not solved(a[t]))
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(only_a, only_b) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def compare_runs(spec: Union[str, Any], runs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Orders runs of one benchmark by its own metric and names a winner only when it is clear.

    A run is a dict: `name`, `metrics` (the adapter's score), optionally `trials` and `successes` (counts for a Wilson interval), `per_task` (task -> list of
    pass/fail per trial; needed for the Terminal-Bench task-mean interval and the tau3 bootstrap), `k` (tau3), `subset`, `provisional`.
    `rank` of a run is 1 plus the number of runs whose interval lies wholly above its own; runs with no run clearly above them share rank 1 and are tied unless
    one lies wholly above all the others, which is then the `winner`. Runs on different subsets or tasks, or with no interval, never produce a winner."""
    spec_id = spec if isinstance(spec, str) else getattr(spec, "id")
    policy = POLICIES.get(spec_id)
    if policy is None:
        metric = getattr(spec, "primary_metric", None)
        if not metric:
            raise KeyError(f"no comparison rule for {spec_id!r}")
        policy = Policy(metric, "wilson_trials", "trial")
    rows, unranked, warnings = [], [], []
    for i, run in enumerate(runs):
        name = str(run.get("name") or f"run {i + 1}")
        value = (run.get("metrics") or {}).get(policy.metric)
        if value is None:
            unranked.append({"name": name, "why": f"the run has no '{policy.metric}' metric"})
            continue
        rows.append({"name": name, "value": float(value), "interval": run_interval(policy, run), "provisional": bool(run.get("provisional")), "_run": run})
    rows.sort(key=lambda r: (-r["value"], r["name"]))
    subsets = {str(r["_run"].get("subset")) for r in rows if r["_run"].get("subset")}
    task_sets = {frozenset((r["_run"].get("per_task") or {}).keys()) for r in rows if r["_run"].get("per_task")}
    comparable = True
    if len(subsets) > 1:
        comparable = False
        warnings.append(f"the runs used different subsets ({', '.join(sorted(subsets))}); they are not like for like")
    if len(task_sets) > 1:
        comparable = False
        warnings.append("the runs did not cover the same tasks; they are not like for like")
    if any(r["interval"] is None for r in rows):
        warnings.append("a run carries no interval (no counts or no per-task results), so no winner can be named")
    for r in rows:
        mine = r["interval"]
        r["beaten_by"] = [o["name"] for o in rows if o is not r and mine and o["interval"] and o["interval"]["low"] > mine["high"]]
        r["tied_with"] = [o["name"] for o in rows if o is not r and mine and o["interval"] and not (o["interval"]["low"] > mine["high"] or mine["low"] > o["interval"]["high"])]
        r["rank"] = 1 + len(r["beaten_by"])
    top = [r for r in rows if r["rank"] == 1]
    winner = None
    if comparable and len(rows) >= 2 and all(r["interval"] for r in rows) and len(top) == 1 and len(top[0]["beaten_by"]) == 0:
        lead = top[0]
        if all(lead["interval"]["low"] > o["interval"]["high"] for o in rows if o is not lead):
            winner = lead["name"]
    tie = [] if winner or len(rows) < 2 else [r["name"] for r in top]
    paired = None
    if len(rows) == 2 and all(r["_run"].get("per_task") for r in rows):
        paired = {"mcnemar_p": mcnemar_p(rows[0]["_run"]["per_task"], rows[1]["_run"]["per_task"]), "note": "shown for information; the verdict uses the intervals"}
    for r in rows:
        r.pop("_run")
    if any(r["provisional"] for r in rows):
        warnings.append("a run was scored without the official judge: its score is not comparable with the board")
    verdict = (f"{winner} is clearly ahead: its interval lies above all the others." if winner else
               "No run is clearly ahead: " + ("these are tied: " + ", ".join(tie) + "." if tie else "there is nothing to compare.") if rows else "There are no runs to compare.")
    return {"spec": spec_id, "metric": policy.metric, "interval": policy.interval, "sample": policy.unit, "ordered": rows, "unranked": unranked, "winner": winner,
            "tie": tie, "paired": paired, "warnings": warnings, "verdict": verdict}


def run_summary(run_dir: Union[str, Path]) -> Dict[str, Any]:
    """A finished run folder as the dict `compare_runs` takes: the adapter's metrics, the counts of valid trials and the pass/fail list of each task."""
    from .runner import SETTINGS, read_json, read_trials
    run_dir = Path(run_dir)
    record = read_json(run_dir / SETTINGS) or {}
    results = read_json(run_dir / "results.json") or {}
    per_task: Dict[str, List[bool]] = {}
    for key, (valid, res) in sorted(read_trials(run_dir).items(), key=lambda kv: [str(x) for x in kv[0]]):
        if valid:
            per_task.setdefault(str(key[0]), []).append(bool(res.passed))
    settings = record.get("settings") or {}
    subset = str(settings.get("subset") or "")
    arm = str(settings.get("arm") or ("baseline" if subset.endswith("-baseline") else ""))
    flat = [x for v in per_task.values() for x in v]
    return {"name": run_dir.name + (f" ({arm})" if arm else ""), "metrics": results.get("metrics") or {}, "trials": len(flat), "successes": sum(flat), "per_task": per_task,
            "subset": subset[:-len("-baseline")] if subset.endswith("-baseline") else subset, "arm": arm, "provisional": bool(record.get("provisional")),
            "benchmark": record.get("benchmark")}
