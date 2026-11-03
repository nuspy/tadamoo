"""E2: the verify gate, off or on.

Question: on coding tasks that start with a failing test or a trap, does the gate (the task's test command run before the turn may end) get
more tasks truly finished, and fewer finished with a test still failing?

Arm A is `--verify-gate off`, arm B is `--verify-gate always --verify-command "python -m unittest discover -q"` (docs/harness-gates.md);
`A~aa` is A again. Fixture: `fixtures/gate_tasks.py`, eight tasks, four with a trap. The scorer runs the suite on the final workspace and
checks that no test file was edited; it does not trust the model's word that it is done.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from tadamoo import agent_loop as al
from tadamoo.eval.tasks import Task, make_workspace, run_checks

from .agentrun import CountingClient, clear_bytecode, open_agent, transcript_measures
from tadamoo.eval.traces import observe
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import gate_tasks

COLUMNS = ("success", "finished_failing", "passes", "wasted_turns", "prompt_tokens", "cached_tokens", "seconds", "turns")
MAX_TURNS = 30

ARMS = with_control([
    Arm("A", "gate off (the default before it was set to on whenever checks are accepted)", {"verify_gate": "off"}, "--verify-gate off"),
    Arm("B", "gate on with the task's test command", {"verify_gate": "always", "use_task_command": True},
        '--verify-gate always --verify-command "python -m unittest discover -q"'),
], "A")

RULE = Rule(
    primary="success", better="higher", min_effect=0.10, baseline="A", challengers=("B",),
    guards=(Guard("finished_failing", "lower", 0.0), Guard("seconds", "lower", 1.0, relative=True)),
    text="B beats A when its success rate (suite passes, no test edited) is higher than the pooled A and A/A rate by more than the A/A gap and "
         "by at least 10 points, the share of tasks that ended with a test still failing is not higher than A's (beyond the A/A gap), and B takes "
         "no more than twice A's seconds. The gate is then worth turning on by default for attended runs; the default for unattended runs stays the owner's decision.")


def tasks() -> List[Dict[str, Any]]:
    return gate_tasks.tasks()


def as_task(raw: Dict[str, Any]) -> Task:
    return Task(id=raw["id"], category="e2", prompt=raw["prompt"], files=dict(raw["files"]), checks=list(raw["checks"]), max_turns=MAX_TURNS)


def run_one(arm: Arm, raw: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix=f"tadamoo-e2-{raw['id']}-"))
    trial = CaseTrial(arm.name, raw["id"], rep)
    client = CountingClient(ctx.client)
    built = None
    try:
        task = as_task(raw)
        workspace = make_workspace(task, root / "ws")
        settings = {k: v for k, v in arm.settings.items() if k != "use_task_command"}
        if arm.settings.get("use_task_command"):
            settings["verify_commands"] = list(raw["verify"])
        built = open_agent(workspace, client, root / "data", max_turns=MAX_TURNS, **settings)
        gate_events: List[Dict[str, Any]] = []
        built.agent.on_event = lambda e: gate_events.append(e) if e.get("type") == "gate" else None
        built.agent.sampling = {"seed": ctx.seed_base + rep, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})}
        turns = built.agent.run(task.prompt)
        reason = built.agent.stop_reason
        clear_bytecode(workspace)
        failures = run_checks(task, workspace, al.answer_text(turns), [])
        measures = transcript_measures(turns)
        trial.trace, trial.components = observe(turns, built.agent, built.audit, client)
        finished = reason == al.FINAL
        trial.success, trial.notes = not failures, failures
        trial.metrics = {"finished_failing": 1.0 if finished and failures else 0.0, "passes": sum(1 for e in gate_events if e.get("decision") == "retry"),
                         "wasted_turns": measures["wasted_turns"], "turns": measures["turns"], "prompt_tokens": client.prompt_tokens,
                         "cached_tokens": client.cached_tokens, "completion_tokens": client.completion_tokens}
        if reason == al.ERROR:
            trial.invalid, trial.error = True, built.agent.error
        elif not finished:
            trial.notes.insert(0, f"the run ended as {reason}, not with an answer")
    finally:
        trial.metrics["seconds"] = round(time.monotonic() - t0, 2)
        if built is not None:
            try:
                built.close()
            except Exception:
                pass
        if ctx.keep:
            shutil.copytree(root, ctx.keep / f"e2-{arm.name}-{raw['id']}-{rep}", dirs_exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e2", title="Verify gate: off or on", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=tasks, run_trial=run_one, task_id=lambda t: t["id"], default_k=5,
        question="On tasks that start with a failing test or a trap, does the verify gate get more of them truly finished and fewer closed with a test still failing?")
