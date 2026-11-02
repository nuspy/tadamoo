"""E1: the Target Loop's first message, joined or separate.

Question: does sending the person's text alone as the first message, and then one message per task, reach multi-part goals more
reliably than sending the text and task 1 in one message (today)?

Arm A is `first_message: joined`, arm B is `first_message: separate` (an option of the Target Loop, see docs/target-loop.md); `A~aa` is A again.
Fixture: `fixtures/shop_repo.py`, a small repository and eight goals. The scorer runs each goal's own tests and greps on the final workspace.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from harness import target as tg
from harness.eval.tasks import Task, make_workspace, run_checks

from .agentrun import AgentConversation, CountingClient, clear_bytecode, open_agent, transcript_measures
from harness.eval.traces import observe
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import shop_repo

COLUMNS = ("success", "criteria_met", "passes", "wasted_turns", "prompt_tokens", "cached_tokens", "seconds", "turns")
MAX_PASSES = 4                                           # a cap on the loop so a bad arm cannot run for ever; the same for every arm
WALL_S = 1800.0

ARMS = with_control([
    Arm("A", "joined: the person's text and task 1 in one message (today)", {"first_message": "joined"}, "Target Loop option first_message=joined"),
    Arm("B", "separate: the person's text alone, then one message per task", {"first_message": "separate"}, "Target Loop option first_message=separate"),
], "A")

RULE = Rule(
    primary="success", better="higher", min_effect=0.10, baseline="A", challengers=("B",),
    guards=(Guard("prompt_tokens", "lower", 0.25, relative=True), Guard("seconds", "lower", 0.25, relative=True)),
    text="B replaces A when its goal success rate is higher than the pooled A and A/A rate by more than the A/A gap and by at least 10 points, "
         "and B does not use more than 25% more prompt tokens or more than 25% more seconds than A (or the A/A gap on those, if larger). "
         "Otherwise A stays: the default does not change on a difference the noise could have made.")


def goals() -> List[Dict[str, Any]]:
    return shop_repo.goals()


def goal_task(goal: Dict[str, Any]) -> Task:
    return Task(id=goal["id"], category="e1", prompt=goal["text"], files={**shop_repo.REPO, **goal["files"]}, checks=list(goal["checks"]), allow=["shell_command:python *"])


def score(goal: Dict[str, Any], workspace: Path) -> Tuple[bool, float, List[str]]:
    """(every check passed, the share of checks that passed, what failed) for the workspace as it is now."""
    task = goal_task(goal)
    clear_bytecode(workspace)
    failed: List[str] = []
    for check in goal["checks"]:
        single = Task(id=task.id, category="e1", prompt="", files=task.files, checks=[check])
        failed += run_checks(single, workspace, "", [])
    n = len(goal["checks"])
    return not failed, (n - len(failed)) / n if n else 1.0, failed


class _Registry:
    """Stands in for the server's task registry."""

    def record(self, *args: Any, **kwargs: Any) -> None:
        return None


def run_goal(arm: Arm, goal: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix=f"basalt-e1-{goal['id']}-"))
    trial = CaseTrial(arm.name, goal["id"], rep)
    client = CountingClient(ctx.client)
    built = None
    try:
        workspace = make_workspace(goal_task(goal), root / "ws")
        built = open_agent(workspace, client, root / "data")
        built.agent.sampling = {"seed": ctx.seed_base + rep, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})}
        conv = AgentConversation(built, workspace)
        options = {"verify": [shop_repo.VERIFY_CMD], "max_iterations": MAX_PASSES, "stall_passes": 3, **arm.settings}
        target = tg.new_target(goal["text"], options, session="e1")
        llm = tg.StructuredLLM(client)
        loop = tg.TargetLoop(target, conv, llm, _Registry(), lambda t: None, waits=(2.0, 5.0, 15.0))
        timer = threading.Timer(WALL_S, loop.cancel.set)
        timer.daemon = True
        timer.start()
        try:
            loop.run()
        finally:
            timer.cancel()
        ok, fraction, failed = score(goal, workspace)
        measures = transcript_measures(conv.turns)
        trial.trace, trial.components = observe(conv.turns, built.agent, built.audit, client)
        trial.success, trial.notes = ok, failed
        trial.metrics = {"criteria_met": fraction, "passes": len(target["iterations"]), "wasted_turns": measures["wasted_turns"], "turns": measures["turns"],
                         "prompt_tokens": client.prompt_tokens, "cached_tokens": client.cached_tokens, "completion_tokens": client.completion_tokens,
                         "tasks_run": sum(t["attempts"] for t in target["tasks"]), "target_state": 0.0 if target["state"] == "achieved" else 1.0}
        if target.get("error"):
            trial.invalid, trial.error = True, target["error"]
        elif target["stop_reason"] == "person" and loop.cancel.is_set():
            trial.notes.append(f"stopped after {WALL_S:.0f} s")
    finally:
        trial.metrics["seconds"] = round(time.monotonic() - t0, 2)
        if built is not None:
            try:
                built.close()
            except Exception:
                pass
        if ctx.keep:
            shutil.copytree(root, ctx.keep / f"e1-{arm.name}-{goal['id']}-{rep}", dirs_exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e1", title="Target Loop: the first message", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=goals, run_trial=run_goal, task_id=lambda g: g["id"], default_k=5,
        question="Does sending the person's text alone and then one message per task reach multi-part goals more reliably than the text and task 1 in one message?")
