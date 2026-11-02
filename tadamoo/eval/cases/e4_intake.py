"""E4: the look at a request (intake), off, rules or full.

Question: on prompts that are ambiguous, dated, risky, long, injection-looking or plain, does looking at the request first make the agent ask
when it should and not when it should not, resolve dates right, and finish the task more often, and what does it cost in time and tokens?

Arms: A `--intake off`, B `--intake rules`, C `--intake full` (docs/intake.md); `A~aa` is A again. Fixture: `fixtures/intake_prompts.py`, twenty
prompts on a fixed clock (Friday 2026-10-30), attended at the Default permission level (at Auto the harness asks nothing but named owners), each in its own conversation. When the agent ends its turn on a question, the fixture's
`reply` is sent as the person's answer. The scorer is deterministic: a question is asked or not (the turn ends as `asked`), the rows' own checks
decide the task, and the date check is part of those. No model judges anything.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from tadamoo import intake as it
from tadamoo import questions as qs
from tadamoo.eval.tasks import Task, make_workspace, run_checks

from .agentrun import CountingClient, clear_bytecode, close_quietly, open_session
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import intake_prompts

COLUMNS = ("good", "success", "ask_correct", "over_ask", "missed_ask", "date_ok", "seconds", "prompt_tokens", "cached_tokens", "model_calls")
TURN_TIMEOUT_S = 900.0

ARMS = with_control([
    Arm("A", "intake off", {"intake": "off"}, "--intake off"),
    Arm("B", "intake rules (no model call)", {"intake": "rules"}, "--intake rules"),
    Arm("C", "intake full (one triage call when the gate says so)", {"intake": "full"}, "--intake full"),
], "A")

RULE = Rule(
    primary="good", better="higher", min_effect=0.10, baseline="A", challengers=("B", "C"),
    guards=(Guard("over_ask", "lower", 0.10), Guard("seconds", "lower", 0.25, relative=True)),
    text="`good` is the share of prompts where the agent asked exactly when it should (and only then) and the task ended right (its checks, dates "
         "included, pass). A mode beats `off` when its `good` share is higher than the pooled off and A/A share by more than the A/A gap and by at "
         "least 10 points, it does not ask unneeded questions more often than `off` by more than 10 points (or the A/A gap), and it does not take "
         "more than 25% more seconds. If both B and C qualify, the cheaper one (fewer seconds) is the one to recommend.")


def prompts() -> List[Dict[str, Any]]:
    return intake_prompts.prompts()


def score_row(row: Dict[str, Any], asked: bool, checks_failed: List[str]) -> Dict[str, float]:
    """The deterministic score of one prompt: was asking right, did the task end right (the date included when the prompt has one)."""
    ask_correct = asked == bool(row["should_ask"])
    success = not checks_failed
    out = {"ask_correct": 1.0 if ask_correct else 0.0, "over_ask": 1.0 if asked and not row["should_ask"] else 0.0,
           "missed_ask": 1.0 if row["should_ask"] and not asked else 0.0, "good": 1.0 if ask_correct and success else 0.0}
    if row["expected_date"]:
        out["date_ok"] = 1.0 if success else 0.0
    return out


def answer_text(reply: str) -> str:
    """The person's answer to a card, in the words the server sends the model."""
    return f"{qs.ANSWER_HEADER}\n1. -> {reply}\n{qs.GO_ON}"


def run_prompt(arm: Arm, row: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    timeout = float(ctx.extra.get("turn_timeout_s", TURN_TIMEOUT_S))
    root = Path(tempfile.mkdtemp(prefix=f"basalt-e4-{row['id']}-"))
    trial = CaseTrial(arm.name, row["id"], rep)
    client = CountingClient(ctx.client)
    session = built = None
    try:
        task = Task(id=row["id"], category=row["category"], prompt=row["text"], files=dict(row["files"]), checks=list(row["checks"]))
        workspace = make_workspace(task, root / "ws")
        session, events, built = open_session(workspace, client, root / "data", permission_mode="default", allow=["shell_command:python *"], max_turns=20)
        built.agent.sampling = {"seed": ctx.seed_base + rep, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})}
        session.intake = it.Intake(arm.settings["intake"], client)
        session.send("m1", row["text"])
        done = events.wait("done", timeout)
        asked = False
        if done is not None and done.get("stop_reason") == "asked":
            asked = True
            n = len(events.items)
            session.send("m2", answer_text(row["reply"]))
            done = events.wait("done", timeout, after=n)
        if done is None:
            session.cancel()
            trial.invalid, trial.error = True, "no result before the time limit"
        elif done.get("stop_reason") == "error":
            trial.invalid, trial.error = True, str(done.get("error"))
        clear_bytecode(workspace)
        failed = run_checks(task, workspace, str((done or {}).get("final") or ""), [])
        trial.notes = failed + ([] if asked == row["should_ask"] else [f"{'asked' if asked else 'did not ask'}; should {'' if row['should_ask'] else 'not '}have"])
        scored = score_row(row, asked, failed)
        trial.success = not failed
        trial.metrics = {**scored, "prompt_tokens": client.prompt_tokens, "cached_tokens": client.cached_tokens, "completion_tokens": client.completion_tokens,
                         "model_calls": client.calls}
    finally:
        trial.metrics["seconds"] = round(time.monotonic() - t0, 2)
        close_quietly(session)
        if built is not None:
            close_quietly(built)
        if ctx.keep:
            shutil.copytree(root, ctx.keep / f"e4-{arm.name}-{row['id']}-{rep}", dirs_exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e4", title="Intake: off, rules or full", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=prompts, run_trial=run_prompt, task_id=lambda r: r["id"], default_k=5,
        question="Does looking at a request first make the agent ask when it should, not when it should not, get dates right and finish more tasks, and at what cost?")
