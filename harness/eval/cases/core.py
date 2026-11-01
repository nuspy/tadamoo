"""The shared part of the case studies: arms, trials, the A/A control, the decision rule and the report.

A case study compares settings of the harness on the same task material. Every comparison carries an A/A control, the same arm run twice,
and the winner is decided by a rule written down before the run: the difference has to exceed the gap between the two copies of the
baseline (the noise of this setup) and a smallest effect worth acting on. Nothing here talks to a model; the experiments do.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.eval.report import MIN_K, k_warning, wilson
from harness.eval.resume import Journal, make_fingerprint
from harness.eval.traces import components_section, divergent_section, divergent_tasks

STANDARD_COLUMNS = ("success", "passes", "wasted_turns", "prompt_tokens", "cached_tokens", "seconds")
CONTROL_SUFFIX = "~aa"
RATE_NAMES = frozenset({"success", "good", "criteria_met", "finished_failing", "never_ends", "wrong_end", "ask_correct", "over_ask", "missed_ask", "date_ok",
                        "rule_obeyed", "protected_intact", "survival", "clean", "resolved", "completed", "abandoned"})


@dataclass(frozen=True)
class Arm:
    """One setting compared. `settings` is what the experiment applies; `flags` says the same choice as the flag or setting a person would use."""
    name: str
    label: str
    settings: Mapping[str, Any] = field(default_factory=dict)
    flags: str = ""
    copy_of: str = ""                                   # an A/A control: the settings of the arm it names, run as a second arm


def with_control(arms: Sequence[Arm], of: str) -> List[Arm]:
    """The arms plus the A/A control of the arm `of`: the same settings under another name."""
    base = next(a for a in arms if a.name == of)
    return [*arms, Arm(of + CONTROL_SUFFIX, base.label + " (A/A control)", dict(base.settings), base.flags, copy_of=of)]


@dataclass
class CaseTrial:
    arm: str
    task: str
    rep: int
    success: bool = False
    metrics: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)       # what failed, in plain sentences
    error: str = ""
    invalid: bool = False                               # the server or the harness failed, not the thing under test: left out of the means
    trace: List[Dict[str, str]] = field(default_factory=list)       # the agent's tool calls in order, where the experiment records them (harness/eval/traces.py)
    components: Dict[str, int] = field(default_factory=dict)        # counts of the components the run used

    def value(self, name: str) -> Optional[float]:
        if name == "success":
            return 1.0 if self.success else 0.0
        v = self.metrics.get(name)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


@dataclass(frozen=True)
class Guard:
    """A metric a challenger must not make worse by more than the larger of `tolerance` and the noise."""
    metric: str
    better: str                                          # "lower" or "higher"
    tolerance: float
    relative: bool = False


@dataclass(frozen=True)
class Rule:
    """Which arm wins, decided before the run. The challenger beats the baseline when, on `primary`, it is better by more than the A/A gap
    and by more than `min_effect` (a share of the baseline when `relative`), and no guard is broken."""
    primary: str
    better: str                                          # "higher" or "lower"
    min_effect: float
    text: str
    relative: bool = False
    baseline: str = "A"
    challengers: Tuple[str, ...] = ("B",)
    guards: Tuple[Guard, ...] = ()

    @property
    def control(self) -> str:
        return self.baseline + CONTROL_SUFFIX


@dataclass
class Verdict:
    challenger: str
    primary: str
    baseline_value: Optional[float]
    control_value: Optional[float]
    challenger_value: Optional[float]
    noise: Optional[float]
    effect: Optional[float]                              # challenger minus the pooled baseline, signed so that positive is better
    threshold: Optional[float]
    outcome: str
    guards: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)


@dataclass
class Experiment:
    id: str
    title: str
    question: str
    arms: List[Arm]
    rule: Rule
    columns: Sequence[str]
    tasks: Callable[[], List[Any]]
    run_trial: Callable[[Arm, Any, int, "Context"], CaseTrial]
    task_id: Callable[[Any], str] = lambda t: t.id
    default_k: int = 5
    needs_model: bool = True
    extra_report: Optional[Callable[[Sequence[CaseTrial]], List[str]]] = None       # lines the experiment adds after the decision
    opt_in: Sequence[str] = ()                           # task ids that run only when named with --only


@dataclass
class Context:
    """What a trial may use besides its arm and task."""
    client: Any = None                                   # the model (a BasaltClient, or a scripted one in the tests)
    endpoint: str = ""
    api_key: Optional[str] = None
    power_limit_w: Optional[float] = None
    temperature: Optional[float] = 0.6
    seed_base: int = 1000
    keep: Optional[Path] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    plumbing: bool = False                               # a run against a scripted server: the numbers say nothing about the model


# -- means ------------------------------------------------------------------------------------------------------------

def mean_of(trials: Sequence[CaseTrial], name: str) -> Optional[float]:
    values = [v for t in trials if not t.invalid for v in [t.value(name)] if v is not None]
    return statistics.fmean(values) if values else None


def by_arm(trials: Sequence[CaseTrial]) -> Dict[str, List[CaseTrial]]:
    out: Dict[str, List[CaseTrial]] = {}
    for t in trials:
        out.setdefault(t.arm, []).append(t)
    return out


def task_trials(trials: Sequence[CaseTrial]) -> Dict[str, List[Tuple[int, bool, Sequence[Mapping[str, str]]]]]:
    """The valid trials of one arm by task, as (repetition, success, trace)."""
    out: Dict[str, List[Tuple[int, bool, Sequence[Mapping[str, str]]]]] = {}
    for t in trials:
        if not t.invalid:
            out.setdefault(t.task, []).append((t.rep, t.success, t.trace))
    return out


def arm_summary(trials: Sequence[CaseTrial], columns: Sequence[str]) -> Dict[str, Any]:
    valid = [t for t in trials if not t.invalid]
    tasks: Dict[str, List[CaseTrial]] = {}
    for t in valid:
        tasks.setdefault(t.task, []).append(t)
    ok = sum(t.success for t in valid)
    lo, hi = wilson(ok, len(valid))
    return {"trials": len(trials), "valid": len(valid), "invalid": len(trials) - len(valid),
            "means": {c: mean_of(trials, c) for c in columns}, "success_ci95": [round(lo, 3), round(hi, 3)],
            "pass_pow_k": sum(1 for ts in tasks.values() if all(t.success for t in ts)), "pass_at_k": sum(1 for ts in tasks.values() if any(t.success for t in ts)),
            "tasks": len(tasks),
            "divergent": sorted(task for task, ts in tasks.items() if any(t.success for t in ts) and not all(t.success for t in ts))}


# -- the decision -------------------------------------------------------------------------------------------------------

def _signed(better: str, value: float) -> float:
    return value if better == "higher" else -value


def decide(grouped: Mapping[str, Sequence[CaseTrial]], rule: Rule) -> List[Verdict]:
    """One verdict per challenger. The noise is the gap on the primary metric between the baseline and its A/A copy."""
    base, copy = grouped.get(rule.baseline, []), grouped.get(rule.control, [])
    p_base, p_copy = mean_of(base, rule.primary), mean_of(copy, rule.primary)
    out: List[Verdict] = []
    for name in rule.challengers:
        challenger = grouped.get(name, [])
        p_chal = mean_of(challenger, rule.primary)
        v = Verdict(name, rule.primary, p_base, p_copy, p_chal, None, None, None, "")
        if p_base is None or p_chal is None:
            v.outcome = "no result: a missing arm has no valid trial"
            out.append(v)
            continue
        if p_copy is None:
            v.outcome = "no result: without the A/A control the noise is unknown"
            out.append(v)
            continue
        pooled = (p_base * len(base) + p_copy * len(copy)) / max(1, len(base) + len(copy))
        noise = abs(p_base - p_copy)
        floor = rule.min_effect * abs(pooled) if rule.relative else rule.min_effect
        v.noise, v.threshold = noise, max(noise, floor)
        v.effect = _signed(rule.better, p_chal - pooled)
        if noise > floor:
            v.caveats.append(f"the A/A gap ({noise:.3g}) is larger than the smallest effect worth acting on ({floor:.3g}); only a difference above the gap counts")
        broken = []
        for g in rule.guards:
            gb, gc, gx = mean_of(base, g.metric), mean_of(copy, g.metric), mean_of(challenger, g.metric)
            if gb is None or gx is None:
                v.guards.append(f"{g.metric}: not measured")
                continue
            gnoise = abs(gb - gc) if gc is not None else 0.0
            gpool = ((gb * len(base) + gc * len(copy)) / max(1, len(base) + len(copy))) if gc is not None else gb
            allowed = max(g.tolerance * abs(gpool) if g.relative else g.tolerance, gnoise)
            worse = -_signed(g.better, gx - gpool)
            state = "broken" if worse > allowed else "kept"
            v.guards.append(f"{g.metric}: {state} (challenger {gx:.3g}, baseline {gpool:.3g}, worse by {max(worse, 0):.3g}, allowed {allowed:.3g})")
            if state == "broken":
                broken.append(g.metric)
        if v.effect > v.threshold and not broken:
            v.outcome = f"{name} is better than {rule.baseline}"
        elif v.effect > v.threshold:
            v.outcome = f"{name} is better on {rule.primary} but breaks a guard ({', '.join(broken)}): not adopted"
        elif v.effect < -v.threshold:
            v.outcome = f"{rule.baseline} is better than {name}"
        else:
            v.outcome = f"no difference beyond the noise between {rule.baseline} and {name}"
        out.append(v)
    return out


# -- running ------------------------------------------------------------------------------------------------------------

def rotate(items: Sequence[Any], n: int) -> List[Any]:
    """The arms in a different order each time, so no arm always goes first (A/B runs alternate within one session)."""
    if not items:
        return []
    n %= len(items)
    return list(items[n:]) + list(items[:n])


def selected_tasks(exp: Experiment, only: Sequence[str] = ()) -> List[Any]:
    """The tasks to run: those named in `only`, else all but the ones that run only when named."""
    return [t for t in exp.tasks() if (exp.task_id(t) in only if only else exp.task_id(t) not in exp.opt_in)]


def run_fingerprint(exp: Experiment, ctx: Context, k: int, arms: Optional[Sequence[str]] = None, only: Sequence[str] = (),
                    environment: Optional[Mapping[str, Any]] = None, model_file: Optional[str] = None) -> Dict[str, Any]:
    """What a resumed run must match (harness/eval/resume.py): the code, the model file, the settings the run records, the arms chosen, the tasks and k."""
    sampling = {"temperature": ctx.temperature, "seed_base": ctx.seed_base, "power_limit_w": ctx.power_limit_w, "plumbing": ctx.plumbing, **{f"extra.{n}": v for n, v in ctx.extra.items()}}
    env = {n: v for n, v in (environment or {}).items() if n not in ("Endpoint", "Harness")}           # the port changes between server starts; the code is in the git part
    chosen = [a for a in exp.arms if not arms or a.name in arms]
    tasks = [exp.task_id(t) for t in selected_tasks(exp, only)]
    return make_fingerprint(experiment=exp.id, k=k, arms=[asdict(a) for a in chosen], tasks=tasks, sampling=sampling, environment=env, model_file=model_file)


def run_experiment(exp: Experiment, ctx: Context, k: int, arms: Optional[Sequence[str]] = None, only: Sequence[str] = (),
                   on_trial: Optional[Callable[[CaseTrial], None]] = None, journal: Optional[Journal] = None) -> List[CaseTrial]:
    """Every task k times under every arm, arms alternating: for each repetition and task the arms run in a rotating order. With a journal, every
    finished trial is written to it and a valid one it already holds is reused instead of run."""
    chosen = [a for a in exp.arms if not arms or a.name in arms]
    tasks = selected_tasks(exp, only)
    trials: List[CaseTrial] = []
    for rep in range(1, k + 1):
        for i, task in enumerate(tasks):
            for arm in rotate(chosen, rep + i):
                key = (arm.name, exp.task_id(task), rep)
                kept = journal.get(key) if journal else None
                if kept is not None:
                    trials.append(CaseTrial(**kept))
                    if on_trial:
                        on_trial(trials[-1])
                    continue
                try:
                    trial = exp.run_trial(arm, task, rep, ctx)
                except Exception as exc:                  # a broken trial is an invalid trial, never the end of the run
                    trial = CaseTrial(arm.name, exp.task_id(task), rep, error=f"{type(exc).__name__}: {exc}", invalid=True)
                if journal:
                    journal.add(key, asdict(trial), not trial.invalid)
                trials.append(trial)
                if on_trial:
                    on_trial(trial)
    return trials


# -- the report ---------------------------------------------------------------------------------------------------------

def _num(v: Optional[float], name: str) -> str:
    if v is None:
        return "n/a"
    if name in RATE_NAMES or name.endswith("_rate") or name.endswith("_share"):
        return f"{v * 100:.0f}%"
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.2f}"


def power_label(ctx: Context) -> str:
    if ctx.plumbing:
        return "none: a plumbing run with a scripted server and no model (the numbers are checks of the code, not measurements of the model)"
    return f"{ctx.power_limit_w:g} W" if ctx.power_limit_w else "NOT RECORDED (pass --gpu-power-limit-w): the numbers are not comparable without it"


def render_report(exp: Experiment, ctx: Context, trials: Sequence[CaseTrial], k: int, environment: Optional[Mapping[str, Any]] = None) -> str:
    grouped = by_arm(trials)
    cols = list(exp.columns)
    lines = [f"# {exp.id.upper()}: {exp.title}", "", f"> {exp.question}", "",
             "> Development A/B only. Every difference is read against the A/A gap below; a difference under that gap is noise.", ""]
    if ctx.plumbing:
        lines += ["> **Plumbing check.** This run used a scripted server or a scripted model. It shows that the experiment runs and scores; "
                  "it says nothing about how the real model behaves.", ""]
    lines += ["## What was run", "", f"- **GPU power limit**: {power_label(ctx)}",
              f"- **Repetitions k**: {k}", f"- **Arms**: " + "; ".join(f"`{a.name}` {a.label}" + (f" ({a.flags})" if a.flags else "") for a in exp.arms),
              f"- **Trials**: {len(trials)} ({sum(t.invalid for t in trials)} invalid: a server or harness failure, left out of the means)"]
    for key, value in (environment or {}).items():
        lines.append(f"- **{key}**: {value}")
    warn = k_warning(k)
    if warn:
        lines += ["", f"> **Warning**: {warn}"]
    lines += ["", "## Per arm", "", "| Arm | Trials | " + " | ".join(cols) + " | Success 95% | pass^k | Divergent tasks |",
              "| :--- | ---: | " + " | ".join("---:" for _ in cols) + " | :---: | :---: | :--- |"]
    for arm in exp.arms:
        ts = grouped.get(arm.name, [])
        if not ts:
            continue
        s = arm_summary(ts, cols)
        lines.append(f"| `{arm.name}` | {s['valid']} | " + " | ".join(_num(s["means"][c], c) for c in cols) +
                     f" | {s['success_ci95'][0] * 100:.0f}-{s['success_ci95'][1] * 100:.0f}% | {s['pass_pow_k']}/{s['tasks']} | {', '.join(s['divergent']) or '-'} |")
    lines += [""]
    divergent = [{"arm": arm.name, **row} for arm in exp.arms for row in divergent_tasks(task_trials(grouped.get(arm.name, [])))]
    lines += divergent_section(divergent)
    lines += components_section([(arm.name, [t.components for t in grouped[arm.name] if not t.invalid]) for arm in exp.arms if arm.name in grouped])
    lines += ["## Decision", "", f"Rule, written before the run: {exp.rule.text}", ""]
    for v in decide(grouped, exp.rule):
        lines.append(f"- **{v.challenger} against {exp.rule.baseline}** on `{v.primary}`: {v.outcome}.")
        if v.noise is not None:
            lines.append(f"  A/A: {exp.rule.baseline} {_num(v.baseline_value, v.primary)}, copy {_num(v.control_value, v.primary)}, gap {v.noise:.3g}; "
                         f"{v.challenger} {_num(v.challenger_value, v.primary)}; effect {v.effect:+.3g} (better is positive) against a threshold of {v.threshold:.3g}.")
        lines += [f"  Guard {g}." for g in v.guards] + [f"  Note: {c}." for c in v.caveats]
    if exp.extra_report is not None:
        lines += exp.extra_report(trials)
    failed = [t for t in trials if t.notes and not t.success and not t.invalid][:12]
    if failed:
        lines += ["", "## First failures", ""] + [f"- `{t.arm}` {t.task} #{t.rep}: {t.notes[0]}" for t in failed]
    bad = [t for t in trials if t.invalid][:12]
    if bad:
        lines += ["", "## Invalid trials", ""] + [f"- `{t.arm}` {t.task} #{t.rep}: {t.error}" for t in bad]
    lines += ["", "Read the transcripts of failed trials before trusting a low score. These are development numbers; the headline ones are the maintainer's.", ""]
    return "\n".join(lines)


def results_json(exp: Experiment, ctx: Context, trials: Sequence[CaseTrial], k: int, environment: Optional[Mapping[str, Any]] = None) -> str:
    grouped = by_arm(trials)
    return json.dumps({"experiment": exp.id, "title": exp.title, "k": k, "gpu_power_limit_w": ctx.power_limit_w, "plumbing": ctx.plumbing,
                       "environment": dict(environment or {}), "rule": exp.rule.text,
                       "arms": {name: arm_summary(ts, exp.columns) for name, ts in grouped.items()},
                       "verdicts": [asdict(v) for v in decide(grouped, exp.rule)], "trials": [asdict(t) for t in trials]}, indent=2, ensure_ascii=False)


def check_k(k: int) -> Optional[str]:
    return k_warning(k) if k < MIN_K else None
