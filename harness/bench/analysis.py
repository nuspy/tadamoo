"""What failed and what to change. Every failed trial is put in one class by fixed rules over its error text, its trajectory events and its verifier output;
each class points to the part of the harness that handles it and says in a plain sentence what to change.

A trajectory file is JSON lines (or one JSON list) of events: `{"type": "tool_call", "name": ..., "args": ..., "decision": "executed|blocked|refused|invalid|failed"}`,
`{"type": "tool_result", "name": ..., "error": text}`, `{"type": "plan", "revision": n}`, `{"type": "stop", "reason": ...}`, `{"type": "compaction"}`. Other events are
ignored. Candidate lessons are made from the classes and the tool names only, never from a task's text."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from harness.eval.traces import divergent_tasks

from .runner import TrialResult, key_text, read_json, read_trials

READ_LIMIT = 5_000_000
TAIL_CHARS = 4000
REFUSED_LAST_N = 3
LOOP_CONSECUTIVE = 4
LOOP_TOTAL = 8
INVALID_CALLS = 3
PLAN_REVISIONS = 3
TOP = 5
LESSON_MIN_TRIALS = 2
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")

# class id -> (title, part of the harness, what to change, can the harness fix it)
CLASSES: Dict[str, Tuple[str, str, str, bool]] = {
    "environment_problem": ("Environment or infrastructure problem", "the benchmark setup (Docker, WSL2, the model server)",
                            "Not the agent's fault: repair the environment named in the error and run these trials again.", False),
    "context_overflow": ("Context overflow", "context management (harness/compaction.py, harness/context.py)",
                         "Compact earlier, cut long tool outputs before they enter the context, and keep working notes outside the window.", True),
    "timeout": ("Timeout", "pacing in the agent loop (harness/agent_loop.py) and the tool timeouts (harness/tools/shell.py)",
                "Run long commands in the background, look at partial output, and start the slowest step first.", True),
    "missing_executable": ("Missing executable", "environment discovery (harness/environment.py, harness/project_profile.py)",
                           "Before relying on a program, check that it exists, then install it or use another one that is present.", True),
    "blocked_by_site": ("Blocked by a site or CAPTCHA", "the browser tools (harness/browser.py)",
                        "Mostly outside the harness: record the block, and have the agent stop early and say so instead of retrying the same page.", False),
    "permission_refused": ("Refused by our permission layer", "the permission layer (harness/permissions.py, harness/exec_level.py)",
                           "Decide whether the refused action is legitimate in the benchmark's sandbox: allow it there, or give the agent a permitted way to the same result.", True),
    "tool_format_error": ("Tool-format error", "tool-call parsing and the chat template (harness/tool_extract.py, harness/tools/repair.py)",
                          "Make the tool schemas simpler, repair the malformed call and show the exact error to the model so it can correct it.", True),
    "loop": ("Loop", "the loop and repeat guards (harness/loop_guard.py, harness/repeat_guard.py)",
             "Stop the repeated call earlier and tell the model what it already tried, so it changes approach.", True),
    "gave_up": ("Too many failed attempts", "the limit of failed attempts (harness/agent_loop.py, set in the Benchmarks tab)",
                "Read what the agent kept trying: if the task is within reach, raise the limit; if not, change what it tries first.", True),
    "judge_disagreement": ("Judge disagreement", "the judge (the local model standing in for the official judge)",
                           "Read the judge's reason for these trials: if the judge is wrong, the score is not comparable; if it is right, treat them as wrong final states.", False),
    "bad_plan": ("Bad plan", "the planner (harness/planner/, harness/todo.py)",
                 "Plan the steps with the checks the task implies, and keep a plan that is changing over and over under review instead of replanning blindly.", True),
    "wrong_final_state": ("Wrong final state", "the completion check (harness/completion.py) and the verification step",
                          "Have the agent run the task's own check and compare the final state with what the task asked before it says it is done.", True),
    "unknown": ("Unknown", "trajectory recording (the adapter)", "Record more of the trial (events, tool errors, verifier output) so this kind of failure can be classed.", False),
}
CLASS_ORDER = tuple(CLASSES)

_ENV = re.compile(r"docker (?:daemon|engine)|cannot connect to the docker|no space left|pull access denied|image .{0,60}not found|connection (?:refused|reset)|"
                  r"name resolution|server (?:unavailable|crashed|restarted)|adapter crashed|out of memory|oom-?kill|wsl.{0,20}(?:error|not running)", re.I)
_CONTEXT = re.compile(r"context (?:length|window|overflow|limit)|maximum context|exceeds? the context|too many tokens|prompt is too long", re.I)
_TIMEOUT = re.compile(r"time[- ]?out|timed out|deadline exceeded|wall[- ]clock", re.I)
_MISSING = re.compile(r"command not found|is not recognized as an internal|executable file not found|(?:^|\s)\S+: not found\b|no such file or directory: '?[\w./-]*bin", re.I | re.M)
_SITE = re.compile(r"captcha|are you a (?:robot|human)|verify you are human|access denied|403 forbidden|cloudflare|sign in to continue|log ?in required|paywall", re.I)
_PERMISSION = re.compile(r"refused by (?:the |our )?permission|permission layer|not allowed by (?:the )?(?:policy|permission)", re.I)
_FORMAT = re.compile(r"invalid (?:tool )?(?:call|arguments)|malformed (?:tool|json)|could not parse (?:the )?tool|unknown tool|unterminated string|schema validation", re.I)
_LOOP = re.compile(r"loop detected|repeat guard|stuck in a loop|repeated the same", re.I)
_JUDGE = re.compile(r"judge (?:said|rejected|disagree)", re.I)


def _read_events(path: Path) -> List[Dict[str, Any]]:
    try:
        with open(path, "rb") as f:
            raw = f.read(READ_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return [e for e in data if isinstance(e, dict)]
        except ValueError:
            return []
    out = []
    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _read_tail(path: Path) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-TAIL_CHARS:]


def _resolve(run_dir: Path, rel: str) -> Optional[Path]:
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else Path(run_dir) / p


def _calls(events: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return [e for e in events if e.get("type") == "tool_call"]


def _call_key(e: Mapping[str, Any]) -> str:
    return str(e.get("name")) + hashlib.sha1(json.dumps(e.get("args"), sort_keys=True, default=str).encode()).hexdigest()[:8]


def _longest_repeat(calls: Sequence[Mapping[str, Any]]) -> Tuple[int, Optional[str]]:
    best, name, run, prev = 0, None, 0, None
    for c in calls:
        k = _call_key(c)
        run = run + 1 if k == prev else 1
        prev = k
        if run > best:
            best, name = run, c.get("name")
    return best, name


def classify(result: TrialResult, events: Sequence[Mapping[str, Any]], verifier: str) -> Tuple[str, str, List[str]]:
    """(class, the rule that fired in words, the tool names involved) for a trial that did not pass."""
    facts = result.facts or {}
    hinted = facts.get("class")
    if hinted in CLASSES:
        return hinted, "the adapter named the class", []
    err = result.error or ""
    calls = _calls(events)
    stops = [str(e.get("reason", "")) for e in events if e.get("type") == "stop"]
    errors = " ".join(str(e.get("error", "")) for e in events if e.get("error")) + " " + " ".join(str(e.get("text", "")) for e in events if e.get("type") == "error")
    evidence = " ".join((err, errors, verifier))
    if _ENV.search(err) or (result.passed is None and err):
        return "environment_problem", "the error names a failing environment or the trial could not be judged", []
    if _CONTEXT.search(evidence) or "context_overflow" in stops:
        return "context_overflow", "the context window ran out", []
    if _TIMEOUT.search(evidence) or "timeout" in stops:
        return "timeout", "the time limit was reached", []
    if _MISSING.search(errors) or _MISSING.search(verifier) or _MISSING.search(err):
        names = [str(e.get("name")) for e in events if e.get("type") == "tool_result" and e.get("error") and _MISSING.search(str(e["error"]))]
        return "missing_executable", "a command was not found", names
    if _SITE.search(evidence):
        return "blocked_by_site", "the page asked for a login, a CAPTCHA or refused access", []
    refused = [c for c in calls if c.get("decision") in ("refused", "blocked")]
    if (calls and calls[-1].get("decision") in ("refused", "blocked")) or len(refused) >= REFUSED_LAST_N or _PERMISSION.search(evidence):
        return "permission_refused", "calls were refused by the permission layer", [str(c.get("name")) for c in refused]
    invalid = [c for c in calls if c.get("decision") == "invalid"]
    if len(invalid) >= INVALID_CALLS or _FORMAT.search(evidence):
        return "tool_format_error", "tool calls were malformed or invalid", [str(c.get("name")) for c in invalid]
    longest, looped = _longest_repeat(calls)
    total_same = Counter(_call_key(c) for c in calls).most_common(1)
    if longest >= LOOP_CONSECUTIVE or (total_same and total_same[0][1] >= LOOP_TOTAL) or _LOOP.search(evidence):
        return "loop", "the same call was repeated", [looped] if looped else []
    if facts.get("judge_disagrees") or _JUDGE.search(evidence):
        return "judge_disagreement", "the judge did not accept what the check accepted", []
    if "gave_up" in stops:
        return "gave_up", "the agent reached the limit of failed attempts and was stopped", []
    revisions = sum(1 for e in events if e.get("type") == "plan")
    if revisions >= PLAN_REVISIONS or (events and not calls):
        return "bad_plan", "the agent gave up, replanned over and over, or never acted", []
    if result.passed is False and (verifier.strip() or (not err.strip() and events)):
        return "wrong_final_state", "the run ended normally and the check did not accept the final state", []
    return "unknown", "nothing recorded says why", []


def _usable(name: str) -> bool:
    return bool(TOOL_NAME.match(name))


def candidate_lessons(counts: Mapping[str, int], tools: Mapping[str, Counter], failed: int, min_trials: int = LESSON_MIN_TRIALS) -> List[Dict[str, Any]]:
    """Lessons for harness.lessons made from the class and the tool names only. Classes the harness cannot act on, and ones seen once, make none."""
    out = []
    for cls in CLASS_ORDER:
        title, _component, change, fixable = CLASSES[cls]
        n = counts.get(cls, 0)
        if not fixable or n < min_trials or cls in ("wrong_final_state", "unknown"):
            continue
        names = [t for t, _ in tools.get(cls, Counter()).most_common(3) if _usable(t)]
        suffix = f" involving the tools {', '.join(names)}" if names else ""
        out.append({"situation": f"Benchmark trials ended in the class '{title}'{suffix}", "went_wrong": f"{n} of {failed} failed trials had this cause",
                    "fix": change, "evidence": [f"benchmark run: {n} trials"], "scope": "project", "signature": f"bench:{cls}:{'+'.join(sorted(names))}"})
    return out


def run_dir_of(run: Any) -> Path:
    return Path(getattr(run, "dir", run))


def report(run: Any, adapter: Any = None) -> Dict[str, Any]:
    """The analysis of a run folder (or a BenchRun): counts per class, tasks that pass only sometimes, cost and the top changes. Nothing is written."""
    run_dir = run_dir_of(run)
    record = read_json(run_dir / "settings.json") or {}
    results_file = read_json(run_dir / "results.json") or {}
    trials = read_trials(run_dir)
    classes: Counter = Counter()
    tools: Dict[str, Counter] = {}
    failures = []
    by_task: Dict[str, List[Tuple[int, bool, List[Dict[str, str]]]]] = {}
    passed = invalid = tokens = 0
    seconds = 0.0
    for key, (_valid, res) in sorted(trials.items(), key=lambda kv: key_text(kv[0])):
        tokens += res.tokens
        seconds += res.seconds
        events = _read_events(p) if (p := _resolve(run_dir, res.trajectory_path)) else []
        trace = [{"name": str(e.get("name")), "args": str(e.get("args", ""))[:100], "key": _call_key(e)} for e in _calls(events)]
        if res.valid:                              # a trial that could not be judged says nothing about whether the task is flaky
            by_task.setdefault(str(key[0]), []).append((int(key[1]) if len(key) > 1 and isinstance(key[1], int) else 0, bool(res.passed), trace))
        if res.passed:
            passed += 1
            continue
        if not res.valid:
            invalid += 1
        verifier = _read_tail(p) if (p := _resolve(run_dir, res.verifier_output_path)) else ""
        if adapter is not None:
            res = TrialResult.from_dict({**res.to_dict(), "facts": {**res.facts, **adapter.failure_facts(res)}})
        cls, why, names = classify(res, events, verifier)
        classes[cls] += 1
        tools.setdefault(cls, Counter()).update(names)
        failures.append({"key": list(key), "class": cls, "why": why, "tools": sorted(set(names)), "error": res.error[:300]})
    failed = len(failures)
    ranked = sorted(classes.items(), key=lambda kv: (-kv[1], CLASS_ORDER.index(kv[0])))
    recommendations = [{"class": c, "title": CLASSES[c][0], "count": n, "share": round(n / failed, 3), "component": CLASSES[c][1], "change": CLASSES[c][2],
                        "harness_fixable": CLASSES[c][3]} for c, n in ranked[:TOP]]
    n_trials = len(trials)
    return {"run_id": record.get("run_id", run_dir.name), "benchmark": record.get("benchmark"), "benchmark_commit": record.get("benchmark_commit"),
            "power_limit_w": record.get("power_limit_w"), "model": record.get("model"), "harness": record.get("harness"),
            "metrics": results_file.get("metrics", {}), "provisional": record.get("provisional", False), "provisional_label": record.get("provisional_label", ""),
            "trials": n_trials, "passed": passed, "failed": failed, "invalid": invalid,
            "classes": {c: classes.get(c, 0) for c in CLASS_ORDER if classes.get(c)}, "failures": failures,
            "sometimes_pass": [{"task": r["task"], "passed": r["passed"], "of": r["of"]} for r in divergent_tasks(by_task)],
            "cost": {"tokens": tokens, "seconds": round(seconds, 2), "tokens_per_trial": round(tokens / n_trials, 1) if n_trials else 0,
                     "seconds_per_trial": round(seconds / n_trials, 2) if n_trials else 0},
            "recommendations": recommendations, "candidate_lessons": candidate_lessons(classes, tools, failed)}


def markdown(rep: Mapping[str, Any]) -> str:
    L = [f"# Benchmark report: {rep['benchmark']} ({rep['run_id']})", ""]
    if rep.get("provisional"):
        L += [f"**Provisional score: {rep['provisional_label']}.**", ""]
    L += [f"Benchmark commit `{rep['benchmark_commit']}`; GPU power limit {rep['power_limit_w']} W; model {(rep.get('model') or {}).get('name', '?')}.", "",
          "## Result", "", f"Trials {rep['trials']}: {rep['passed']} passed, {rep['failed']} failed ({rep['invalid']} could not be judged).", ""]
    L += [f"- {k}: {v}" for k, v in (rep.get("metrics") or {}).items()] + [""]
    L += ["## Why trials failed", ""]
    if not rep["classes"]:
        L += ["No failed trials.", ""]
    for c, n in rep["classes"].items():
        L.append(f"- {CLASSES[c][0]}: {n}")
    L += ["", "## What to change first", ""]
    for i, r in enumerate(rep["recommendations"], 1):
        L.append(f"{i}. {r['title']} ({r['count']} trials, {r['share'] * 100:.0f}%). Part: {r['component']}. {r['change']}")
    if rep["sometimes_pass"]:
        L += ["", "## Tasks that passed only sometimes", ""] + [f"- {r['task']}: {r['passed']} of {r['of']}" for r in rep["sometimes_pass"]]
    c = rep["cost"]
    L += ["", "## Cost", "", f"{c['tokens']} tokens and {c['seconds']} s in total; {c['tokens_per_trial']} tokens and {c['seconds_per_trial']} s per trial.", ""]
    if rep["candidate_lessons"]:
        L += ["## Candidate lessons", ""] + [f"- {x['situation']}: {x['fix']}" for x in rep["candidate_lessons"]] + [""]
    return "\n".join(L)


def write_report(run: Any, adapter: Any = None) -> Dict[str, Any]:
    """Analyses the run and writes report.md in its folder; returns the report with the file's path."""
    rep = report(run, adapter)
    path = run_dir_of(run) / "report.md"
    path.write_text(markdown(rep), encoding="utf-8")
    return {**rep, "report_path": str(path)}
