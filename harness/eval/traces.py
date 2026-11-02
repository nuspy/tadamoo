"""What a trial did, read from the run it already made: the tool calls in order (the trace), the components it used as counts, and where trials
of one task part ways. Nothing here calls a model or changes how the agent runs."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ARGS_CHARS = 100                 # the arguments in a trace line, cut

SKILL_TOOLS = ("load_skill", "load_capability")
SUBAGENT_TOOLS = ("spawn_subagent", "delegate_to_acp_agent", "ask_a2a_agent", "ask_council")
MEMORY_TOOLS = ("recall", "recall_about_user", "search_past")
COMPONENTS = ("skills", "subagents", "memory_recalls", "hook_blocks", "hook_feedback", "stop_denials", "course_checks", "compactions", "side_uses")


def _args_text(arguments: Any) -> str:
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


def call_trace(turns: Iterable[Any]) -> List[Dict[str, str]]:
    """The tool calls of a run in order: name, a short text of the arguments and a digest of all of them (two calls are the same when name and digest are)."""
    out = []
    for turn in turns:
        for c in turn.tool_calls:
            text = _args_text(c.written if c.written is not None else c.arguments)
            out.append({"name": c.tool_name, "args": text[:ARGS_CHARS] + ("..." if len(text) > ARGS_CHARS else ""),
                        "key": hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]})
    return out


def component_usage(turns: Sequence[Any], audit_records: Optional[Sequence[Mapping[str, Any]]] = None, compactions: int = 0,
                    side_uses: Optional[Mapping[str, int]] = None) -> Dict[str, int]:
    """Counts of the components a run used. Tool names and decisions come from the turns, stop-hook denials from the audit ledger, `compactions` from the
    context's own count and `side_uses` (technical-session calls by use) from the client that counted them. Skills, sub-agents and memory recalls are
    calls that ran; hooks are counted where they acted (a call one blocked, feedback one gave, a stop one denied); `course_checks` are the notes and cards of the course watch."""
    calls = [c for t in turns for c in t.tool_calls]
    ran = [c.tool_name for c in calls if c.decision == "executed"]
    used = {
        "skills": sum(n in SKILL_TOOLS for n in ran),
        "subagents": sum(n in SUBAGENT_TOOLS for n in ran),
        "memory_recalls": sum(n in MEMORY_TOOLS for n in ran),
        "hook_blocks": sum(c.decision == "blocked" for c in calls),
        "hook_feedback": sum(bool(c.feedback) for c in calls),
        "stop_denials": sum(1 for r in audit_records or () if r.get("action") == "stop_denied"),
        "course_checks": sum(1 for r in audit_records or () if r.get("action") == "course_check"),
        "compactions": int(compactions),
        "side_uses": sum(int(v) for v in (side_uses or {}).values()),
    }
    for use, n in sorted((side_uses or {}).items()):
        used[f"side:{use}"] = int(n)
    return used


def observe(turns: Sequence[Any], agent: Any = None, audit: Any = None, client: Any = None) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """(trace, components) of a finished run, with the counters of the agent, its audit ledger and the counting client when it has them."""
    compactions = getattr(getattr(getattr(agent, "context", None), "compaction", None), "count", 0)
    return call_trace(turns), component_usage(turns, getattr(audit, "records", None), compactions or 0, getattr(client, "side_uses", None))


# -- where trials of one task part ways ---------------------------------------------------------------------------------

def first_difference(a: Sequence[Mapping[str, str]], b: Sequence[Mapping[str, str]]) -> Optional[int]:
    """The index of the first call where two traces differ (one that ends sooner differs where it ends); None when they are the same."""
    for i, (x, y) in enumerate(zip(a, b)):
        if (x.get("name"), x.get("key")) != (y.get("name"), y.get("key")):
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def divergence(trials: Sequence[Tuple[int, bool, Sequence[Mapping[str, str]]]]) -> Optional[Dict[str, Any]]:
    """For one task, trials given as (repetition, passed, trace). None unless some passed and some did not. Otherwise the earliest call at which a
    passing and a failing trial differ: `index` (0-based), the repetitions and the two calls (None where a trace had ended). With no trace in
    any trial `index` is None."""
    passed = [t for t in trials if t[1]]
    failed = [t for t in trials if not t[1]]
    if not passed or not failed:
        return None
    best: Optional[Dict[str, Any]] = None
    for p_rep, _, p_trace in passed:
        for f_rep, _, f_trace in failed:
            if not p_trace and not f_trace:
                continue
            i = first_difference(p_trace, f_trace)
            if i is not None and (best is None or i < best["index"]):
                best = {"index": i, "passed_rep": p_rep, "failed_rep": f_rep, "passed_call": dict(p_trace[i]) if i < len(p_trace) else None,
                        "failed_call": dict(f_trace[i]) if i < len(f_trace) else None}
    return best or {"index": None, "passed_rep": passed[0][0], "failed_rep": failed[0][0], "passed_call": None, "failed_call": None}


def divergent_tasks(by_task: Mapping[str, Sequence[Tuple[int, bool, Sequence[Mapping[str, str]]]]]) -> List[Dict[str, Any]]:
    """The tasks that passed in some trials and not in all, each with `task`, `passed`, `of` and its `divergence`."""
    rows = []
    for task in sorted(by_task):
        d = divergence(by_task[task])
        if d is not None:
            rows.append({"task": task, "passed": sum(1 for t in by_task[task] if t[1]), "of": len(by_task[task]), **d})
    return rows


def _call(c: Optional[Mapping[str, str]]) -> str:
    return "no further call" if c is None else f"`{c['name']}` {c['args']}".replace("|", "/")


def divergent_section(rows: Sequence[Mapping[str, Any]], heading: str = "## Divergent tasks") -> List[str]:
    """Markdown lines for the report; empty when no task diverges."""
    if not rows:
        return []
    arm = any("arm" in r for r in rows)
    lines = [heading, "", "Tasks that passed in some trials and not in all, and the first tool call (0-based index) at which a passing and a failing trial differ.", "",
             ("| Arm " if arm else "") + "| Task | Passed | First differing call | Passing trial | Failing trial |",
             ("| :--- " if arm else "") + "| :--- | :---: | :---: | :--- | :--- |"]
    for r in rows:
        head = f"| `{r['arm']}` " if arm else ""
        if r["index"] is None:
            lines.append(f"{head}| `{r['task']}` | {r['passed']}/{r['of']} | no trace recorded | | |")
        else:
            lines.append(f"{head}| `{r['task']}` | {r['passed']}/{r['of']} | {r['index']} | #{r['passed_rep']}: {_call(r['passed_call'])} | #{r['failed_rep']}: {_call(r['failed_call'])} |")
    return lines + [""]


def components_section(rows: Sequence[Tuple[str, Sequence[Mapping[str, int]]]], heading: str = "## Components used") -> List[str]:
    """Markdown lines: for each group (an arm, or the whole run) the components used, as a total and per trial. `rows` is (name, per-trial component dicts).
    Empty when no trial recorded any."""
    names = [n for n in COMPONENTS if any(c.get(n) for _, cs in rows for c in cs)]
    names += sorted({n for _, cs in rows for c in cs for n in c if n.startswith("side:") and c[n]})
    if not names:
        return []
    lines = [heading, "", "Counts from what the run already recorded (tool calls that ran, hooks where they acted, compactions, technical-session calls by use); total over the trials, and per trial in brackets.", "",
             "| Group | Trials | " + " | ".join(names) + " |", "| :--- | ---: | " + " | ".join("---:" for _ in names) + " |"]
    for name, cs in rows:
        n = max(len(cs), 1)
        cells = [f"{sum(c.get(x, 0) for c in cs)} ({sum(c.get(x, 0) for c in cs) / n:.2f})" for x in names]
        lines.append(f"| `{name}` | {len(cs)} | " + " | ".join(cells) + " |")
    return lines + [""]
