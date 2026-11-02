"""E10: lessons of earlier work, off or on (docs/harness-study/awareness.md, section 3 and 6).

Question: when the agent starts a task it has met before, do the lessons kept from that work (tadamoo/lessons.py, shown at the start of a request as advice)
make it avoid the mistake it made then, so it fails fewer calls and takes fewer steps, without finishing less often?

Arm N has no lessons (`lessons_dir` None); `N~aa` is N again. Arm L has the project's lessons store (`lessons_dir` ""), filled before the run with the lessons
the Recorder keeps for these tasks. They are written in the fixture (`fixtures/lesson_tasks.py`), not captured from a first run: this measures what a good
lesson is worth once it is there, with no model needed to make it, and the same eight lessons sit in the store for every task, so the retrieval by the
request's words has to find the right one. How good the Recorder's captured lessons are is a separate question, tested without a model in tests/test_lessons.py.
Eight tasks, each with a typical recoverable mistake (a wrong file name, flags the script wants, a date in another form, a missing folder, a quoted path);
a script scores the final files, never a judge.

Win rule, written before the run (k of at least 5): L beats N when its mean failed calls per task (a call that errored, was refused, or ended with an exit
code other than 0) is lower than the pooled N and N/N mean by more than the N/N gap and by at least 0.5 calls, its success rate is no lower than N's
(within 10 points or the gap), and it takes no more than 25% more steps than N.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from tadamoo import agent_loop as al
from tadamoo.eval.traces import observe
from tadamoo.lessons import LessonStore

from .agentrun import CountingClient, close_quietly, failed_calls, open_agent, snapshot, transcript_measures, write_files
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import lesson_tasks as lt

COLUMNS = ("success", "failed_calls", "clean", "turns", "tool_calls", "wasted_turns", "lessons_shown", "prompt_tokens", "cached_tokens", "seconds")
MAX_TURNS = 14

ARMS = with_control([
    Arm("N", "no lessons", {"lessons_dir": None, "prefill": False}, "(no --lessons-dir)"),
    Arm("L", "the project's lessons, filled before the run", {"lessons_dir": "", "prefill": True}, "--lessons-dir \"\""),
], "N")

RULE = Rule(
    primary="failed_calls", better="lower", min_effect=0.5, baseline="N", challengers=("L",),
    guards=(Guard("success", "higher", 0.10), Guard("turns", "lower", 0.25, relative=True)),
    text="L beats N when its mean failed calls per task is lower than the pooled N and N/N mean by more than the N/N gap and by at least 0.5 calls, its success "
         "rate is no lower than N's (within 10 points or the gap), and it takes no more than 25% more steps than N.")


def tasks() -> List[Dict[str, Any]]:
    return lt.tasks()


def fill(store: LessonStore) -> None:
    """The eight lessons of the fixture, kept the way the Recorder's candidates are."""
    for task in lt.TASKS:
        situation, went_wrong, fix = task["lesson"]
        store.add(situation, went_wrong, fix, signature=f"fixture:{task['id']}")


def run_trial(arm: Arm, task: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix=f"basalt-e10-{task['id']}-"))
    trial = CaseTrial(arm.name, task["id"], rep)
    client = CountingClient(ctx.client)
    built = None
    try:
        workspace = write_files(root / "ws", task["files"])
        settings = dict(arm.settings)
        if settings.pop("prefill", False):
            fill(LessonStore(workspace))
        built = open_agent(workspace, client, root / "data", max_turns=MAX_TURNS, **settings)
        built.agent.sampling = {"seed": ctx.seed_base + rep, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})}
        turns = built.agent.run(task["prompt"])
        reason = built.agent.stop_reason
        files = snapshot(workspace)
        trial.notes = task["check"](files, al.answer_text(turns))
        trial.success = not trial.notes and reason == al.FINAL
        measures = transcript_measures(turns)
        failed = failed_calls(turns)
        trial.trace, trial.components = observe(turns, built.agent, built.audit, client)
        trial.metrics = {"failed_calls": failed, "clean": 1.0 if failed == 0 else 0.0, "turns": measures["turns"], "tool_calls": measures["tool_calls"],
                         "wasted_turns": measures["wasted_turns"], "lessons_shown": built.agent.mission.get("lessons", 0), "prompt_tokens": client.prompt_tokens,
                         "cached_tokens": client.cached_tokens, "completion_tokens": client.completion_tokens}
        if reason == al.ERROR:
            trial.invalid, trial.error = True, built.agent.error
        elif reason != al.FINAL:
            trial.notes.insert(0, f"the run ended as {reason}, not with an answer")
    finally:
        trial.metrics["seconds"] = round(time.monotonic() - t0, 2)
        if built is not None:
            close_quietly(built)
        if ctx.keep:
            shutil.copytree(root, ctx.keep / f"e10-{arm.name}-{task['id']}-{rep}", dirs_exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e10", title="Lessons of earlier work: off or on", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=tasks, run_trial=run_trial, task_id=lambda t: t["id"], default_k=5,
        question="When a task has been met before, do the lessons kept from it make the agent avoid its old mistake: fewer failed calls and steps, with no loss in success?")
