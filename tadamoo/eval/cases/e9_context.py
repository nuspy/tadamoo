"""E9: what the agent can still answer about the part of a long conversation that was folded, with Basalt's compaction on or off.

Question: after a compaction written from the originals (docs/context-layers.md), does the agent still know what it saw in the folded part (the name of a
function, a value, a count, the standing rules), where masking alone does not, and what does it cost in tokens read again?

Arm `N` is compaction off (masking only), arm `A` is compaction on (`--compaction-threshold 0.85`); `N~aa` is `N` again. The context window is small on purpose,
as in E5, so the job outgrows it. The fixture is E5's generated repository (`fixtures/long_turn_repo.py`) plus fourteen probes (`fixtures/context_probes.py`) put to
the same conversation after the job, with the tools taken away, about the first modules of the job: eight the job needed later (a ledger count, whether a module had the
misspelling, the standing rules) and six it did not (a name, a value). The rule decides on the first group. The proxy arms of the design (the proxy folding
old turns itself, alone or stacked with Basalt's compaction) need the proxy in the loop and are not built here; the Chat's short record is not measured here either.

`--only long200` runs the large job instead: 200 modules, one at a time with read_file and edit_file and no scripts, about 2.6 times the prompt budget of a 65,536
window (`--window 65536`). It runs only when named. Extra columns: `summary_cut` (summary calls that ended cut off), `carry` (share of the first twelve modules whose
ledger line is still readable in the conversation when the job ends), `artifact_reads` (calls that went to the saved copies of earlier results).
"""
from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from tadamoo import agent_loop as al
from tadamoo import compacted
from tadamoo.compaction import text_of
from tadamoo.tools.registry import ToolRegistry

from .agentrun import CountingClient, close_quietly, open_agent, snapshot, transcript_measures, write_files
from tadamoo.eval.traces import observe
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .e5_compaction import WINDOW, notes_of, score
from .fixtures import context_probes as cp
from .fixtures import long_turn_repo as lt

COLUMNS = ("success", "recall_task", "recall_incidental", "recall", "probes_right", "compactions", "generation", "summary_cut", "carry", "artifact_reads", "reread_tokens", "prompt_tokens", "cached_tokens", "tool_calls", "seconds")

ARMS = with_control([
    Arm("N", "compaction off (masking only)", {"compaction": False, "keep_code": False}, "--no-compaction"),
    Arm("A", "compaction on, written from the originals", {"compaction": True, "compaction_threshold": 0.85, "keep_code": False}, "--compaction-threshold 0.85 (the default)"),
], "N")

RULE = Rule(
    primary="recall_task", better="higher", min_effect=0.10, baseline="N", challengers=("A",),
    guards=(Guard("success", "higher", 0.10), Guard("seconds", "lower", 1.0, relative=True)),
    text="A beats N when the share of task probes it answers right (counts it wrote, which modules had the misspelling, the standing rules) is higher than the pooled N and N/N rate by more than the N/N gap and by at least 10 points, its job "
         "succeeds no less often than N (within 10 points or the gap), and it takes no more than twice N's seconds. Tokens read again are reported and not decided on.")


# The large job, named with --only: about 2.6 times the prompt budget of a 65,536-token window, handled one module at a time with the file tools.
LARGE = {"id": "long200", "modules": 200, "seed": 23, "per_module": True}
OPT_IN = (LARGE["id"],)
CARRY_MODULES = 12                    # the first modules, which the probes ask about


def variants() -> List[Dict[str, Any]]:
    return [*lt.VARIANTS, LARGE]


def carry_of(repo: lt.Repo, messages: List[Dict[str, Any]]) -> float:
    """The share of the first modules whose ledger line (`mod_003: 4 functions`, with the right count) can still be read in the conversation."""
    text = "\n".join(text_of(m) for m in messages if isinstance(m, dict) and m.get("role") != "system")
    names = [f"mod_{i:03d}" for i in range(min(CARRY_MODULES, len(repo.functions)))]
    return sum(1 for n in names if re.search(rf"(?<!\w){n}: {repo.functions[n]} functions", text)) / max(1, len(names))


def artifact_reads(turns: List[Any]) -> int:
    """Calls that went to the saved copies of earlier results (`.tadamoo/artifacts`) instead of the files themselves."""
    found = 0
    for turn in turns:
        for call in turn.tool_calls:
            args = call.arguments if isinstance(call.arguments, dict) else {}
            found += any(".tadamoo/artifacts" in str(v).replace("\\", "/") for v in args.values())
    return found


def generation_of(messages: List[Dict[str, Any]]) -> int:
    """The highest generation among the compacted records the conversation holds."""
    gens = [(compacted.info_of(m) or {}).get("generation", 0) for m in messages if isinstance(m, dict)]
    return max([0, *gens])


def ask_probes(built: Any, probes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The probes put one after the other to the agent that did the job, with no tool left; `right` is how many it answered."""
    built.agent.tools = ToolRegistry()
    answers: Dict[str, str] = {}
    for p in probes:
        turns = built.agent.run(p["question"])
        answers[p["id"]] = al.answer_text(turns)
    right = [p["id"] for p in probes if cp.right(p, answers.get(p["id"], ""))]
    share = lambda group: (sum(1 for p in probes if p["group"] == group and p["id"] in right) / max(1, sum(1 for p in probes if p["group"] == group)))   # noqa: E731
    return {"right": len(right), "of": len(probes), "wrong": [p["id"] for p in probes if p["id"] not in right], "answers": answers,
            "task": share("task"), "incidental": share("incidental")}


def run_trial(arm: Arm, variant: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix=f"basalt-e9-{variant['id']}-"))
    trial = CaseTrial(arm.name, variant["id"], rep)
    client = CountingClient(ctx.client)
    built = None
    try:
        repo = lt.build(variant["modules"], variant["seed"])
        workspace = write_files(root / "ws", repo.files)
        built = open_agent(workspace, client, root / "data", context_window=int(ctx.extra.get("window", WINDOW)), max_turns=int(ctx.extra.get("max_turns", 500)),
                           max_seconds=float(ctx.extra.get("max_seconds", 3 * 3600)), **arm.settings)
        built.agent.sampling = {"seed": ctx.seed_base + rep, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})}
        turns = built.agent.run(lt.request(variant["modules"], bool(variant.get("per_module"))))
        reason = built.agent.stop_reason
        final = al.answer_text(turns)
        order = sorted(repo.functions, key=lambda n: (workspace / "pkg" / f"{n}.py").stat().st_mtime_ns)
        scored = score(repo, snapshot(workspace), final, order)
        measures = transcript_measures(turns)
        trial.trace, trial.components = observe(turns, built.agent, built.audit, client)
        trial.success, trial.notes = bool(scored["success"]), notes_of(scored)
        before = client.prompt_tokens - client.cached_tokens
        carried = carry_of(repo, getattr(built.agent, "messages", []))
        probes = cp.build(repo, variant["modules"])
        asked = ask_probes(built, probes) if reason == al.FINAL else {"right": 0, "of": len(probes), "wrong": [p["id"] for p in probes], "answers": {}, "task": 0.0, "incidental": 0.0}
        comp = getattr(getattr(built.agent.context, "compaction", None), "count", 0)
        trial.metrics = {"rule_obeyed": scored["rule_obeyed"], "ledger_ok": scored["ledger_ok"], "course_checks": trial.components.get("course_checks", 0),
                         "recall": asked["right"] / max(1, asked["of"]), "recall_task": asked["task"], "recall_incidental": asked["incidental"], "probes_right": asked["right"], "compactions": comp,
                         "generation": generation_of(getattr(built.agent, "messages", [])), "tool_calls": measures["tool_calls"],
                         "summary_cut": getattr(getattr(built.agent.context, "compaction", None), "cuts", 0), "carry": carried, "artifact_reads": artifact_reads(turns),
                         "prompt_tokens": client.prompt_tokens, "cached_tokens": client.cached_tokens, "reread_tokens": client.prompt_tokens - client.cached_tokens,
                         "reread_tokens_in_probes": (client.prompt_tokens - client.cached_tokens) - before, "completion_tokens": client.completion_tokens}
        if asked["wrong"]:
            trial.notes.append("probes answered wrong: " + ", ".join(asked["wrong"]))
        if reason == al.ERROR:
            trial.invalid, trial.error = True, built.agent.error
        elif reason != al.FINAL:
            trial.notes.insert(0, f"the run ended as {reason}, not with an answer: the probes were not asked")
    finally:
        trial.metrics["seconds"] = round(time.monotonic() - t0, 2)
        if built is not None:
            close_quietly(built)
        if ctx.keep:
            shutil.copytree(root, ctx.keep / f"e9-{arm.name}-{variant['id']}-{rep}", dirs_exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e9", title="What the agent still knows after a fold: compaction off or on", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=variants, run_trial=run_trial,
        task_id=lambda v: v["id"], default_k=5, opt_in=OPT_IN,
        question="After a long job outgrew the context, does the agent still answer about the folded part, with Basalt's compaction on where masking alone loses it?")
