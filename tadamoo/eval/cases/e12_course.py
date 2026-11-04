"""E12: the course watch, off or on (docs/harness-study/awareness.md, sections 4 and 6).

Question: on tasks built to derail an agent (a flaky command it may repeat, a refused command, a file that is not where the request says or not there at all),
does the watch on the course of a request (tadamoo/course.py: a note with the facts after the same failure twice or three failed calls in a row) cut the wasted
calls, without making the agent give up on what could be finished?

Arm N has `course_watch` False; `N~aa` is N again. Arm W has it on (the default). Nothing else differs. Six tasks (`fixtures/derail_tasks.py`): three can be
finished after a detour, three cannot. A run ends well when it completed the task or stopped honestly: said what blocks, left no result that claims success and
left the protected files alone. A script reads the final files and the answer; nothing is judged by a model. The cards for the person need `ask_user`, which
these agents do not have, so a card reaches the agent as the same note.

Win rule, written before the run (k of at least 5): W beats N when its mean failed calls per task (a call that errored, was refused, or ended with an exit code
other than 0) are fewer than the pooled N and N/N mean by more than the N/N gap and by at least 1.0, the share of runs that completed or stopped cleanly is no
lower than N's (within 10 points or the gap), and the share that completed the task is no lower than N's (within 10 points or the gap), so that fewer wasted
calls are not bought by giving up. Wasted turns (E2's measure, which does not count a command that ran and failed) and the notes the watch gave are reported and
not decided on.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from tadamoo import agent_loop as al
from tadamoo.eval.traces import observe

from .agentrun import CountingClient, close_quietly, failed_calls, open_agent, snapshot, transcript_measures, write_files
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import derail_tasks as dt

COLUMNS = ("resolved", "completed", "abandoned", "failed_calls", "wasted_turns", "tool_calls", "turns", "course_checks", "prompt_tokens", "cached_tokens", "seconds")
MAX_TURNS = 16

ARMS = with_control([
    Arm("N", "course watch off", {"course_watch": False}, "(course_watch off)"),
    Arm("W", "course watch on", {"course_watch": True}, "(the default)"),
], "N")

RULE = Rule(
    primary="failed_calls", better="lower", min_effect=1.0, baseline="N", challengers=("W",),
    guards=(Guard("resolved", "higher", 0.10), Guard("completed", "higher", 0.10)),
    text="W beats N when its mean failed calls per task are fewer than the pooled N and N/N mean by more than the N/N gap and by at least 1.0, the share of runs that "
         "completed or stopped cleanly is no lower than N's (within 10 points or the gap), and the share that completed the task is no lower than N's (within 10 "
         "points or the gap). Wasted turns and the watch's notes are reported and not decided on.")


def tasks() -> List[Dict[str, Any]]:
    return dt.tasks()


def run_trial(arm: Arm, task: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix=f"tadamoo-e12-{task['id']}-"))
    trial = CaseTrial(arm.name, task["id"], rep)
    client = CountingClient(ctx.client)
    built = None
    try:
        workspace = write_files(root / "ws", task["files"])
        built = open_agent(workspace, client, root / "data", max_turns=MAX_TURNS, **arm.settings)
        built.agent.sampling = {"seed": ctx.seed_base + rep, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})}
        turns = built.agent.run(task["prompt"])
        reason = built.agent.stop_reason
        scored = dt.score(task, snapshot(workspace), al.answer_text(turns), reason)
        measures = transcript_measures(turns)
        trial.trace, trial.components = observe(turns, built.agent, built.audit, client)
        resolved = scored["completed"] or scored["abandoned"]
        trial.success, trial.notes = resolved, scored["notes"]
        trial.metrics = {"resolved": 1.0 if resolved else 0.0, "completed": 1.0 if scored["completed"] else 0.0, "abandoned": 1.0 if scored["abandoned"] else 0.0,
                         "wasted_turns": measures["wasted_turns"], "failed_calls": failed_calls(turns), "tool_calls": measures["tool_calls"], "turns": measures["turns"],
                         "course_checks": trial.components.get("course_checks", 0), "prompt_tokens": client.prompt_tokens, "cached_tokens": client.cached_tokens,
                         "completion_tokens": client.completion_tokens}
        if reason == al.ERROR:
            trial.invalid, trial.error = True, built.agent.error
    finally:
        trial.metrics["seconds"] = round(time.monotonic() - t0, 2)
        if built is not None:
            close_quietly(built)
        if ctx.keep:
            shutil.copytree(root, ctx.keep / f"e12-{arm.name}-{task['id']}-{rep}", dirs_exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e12", title="Course watch: off or on", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=tasks, run_trial=run_trial, task_id=lambda t: t["id"], default_k=5,
        question="On tasks built to derail an agent, does the watch on the course of a request cut wasted calls without making the agent give up on what could be finished?")
