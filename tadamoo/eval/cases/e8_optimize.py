"""E8: Optimize prompt, off or on.

Question: on requests, short, vague or long, that carry something a rewrite must keep (names, numbers, quotations, code, paths, links), does rewriting in
the technical session before the model reads it make the task end right more often, does it keep what the person wrote, and what does it cost in time?

Arms: A `--optimize-prompt off`, B `--optimize-prompt on` (docs/intake.md); `A~aa` is A again. Fixture: `fixtures/optimize_prompts.py`, twenty-nine requests (short, vague, long, mixed Italian and English),
each in its own workspace, attended, with the look at a request off so only the rewrite differs. B needs a server started with `--side-slot`; without
one its trials are invalid, not failures. The scorer is deterministic: the rows' own checks decide the task, and what the model was given is compared
with what the person wrote by the same protected-content check the harness applies (`tadamoo.optimize.lost`). No model judges anything.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from tadamoo import intake as it
from tadamoo import optimize as opt
from tadamoo.eval.tasks import Task, make_workspace, run_checks
from tadamoo.side import side_context

from .agentrun import CountingClient, clear_bytecode, close_quietly, open_session
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import optimize_prompts

COLUMNS = ("good", "success", "protected_kept", "optimized", "rejected", "lost", "optimize_seconds", "seconds", "prompt_tokens", "cached_tokens", "model_calls")
TURN_TIMEOUT_S = 900.0

ARMS = with_control([
    Arm("A", "optimize prompt off", {"optimize": False}, "--optimize-prompt off"),
    Arm("B", "optimize prompt on (one short call in the technical session)", {"optimize": True}, "--optimize-prompt on"),
], "A")

RULE = Rule(
    primary="good", better="higher", min_effect=0.10, baseline="A", challengers=("B",),
    guards=(Guard("protected_kept", "higher", 0.0), Guard("seconds", "lower", 0.5, relative=True)),
    text="`good` is the share of requests where the task ended right (its checks pass) and the message the model read still held everything the person "
         "wrote that must survive. B beats `off` when its `good` share is higher than the pooled off and A/A share by more than the A/A gap and by at least "
         "10 points, it never gives the model a message that lost protected content, and it does not take more than 50% more seconds.")


def prompts() -> List[Dict[str, Any]]:
    return optimize_prompts.prompts()


def score_row(row: Dict[str, Any], seen: str, state: str, reason: str, checks_failed: List[str]) -> Dict[str, float]:
    """The deterministic score of one request: did the task end right, did the message the model read keep what the person wrote, and what became of the rewrite."""
    success = not checks_failed
    kept = opt.lost(row["text"], seen) is None
    return {"success": 1.0 if success else 0.0, "protected_kept": 1.0 if kept else 0.0, "good": 1.0 if success and kept else 0.0,
            "optimized": 1.0 if state == "sent" else 0.0, "rejected": 1.0 if state == "rejected" else 0.0, "lost": 1.0 if state == "rejected" and reason == "lost" else 0.0}


class TimedOptimizer(opt.Optimizer):
    """The harness's optimizer that remembers how long its call took and what came of it."""

    def __init__(self, client: Any, enabled: bool) -> None:
        super().__init__(client, enabled)
        self.spent, self.last = 0.0, opt.Result()

    def run(self, text: str, **kw: Any) -> opt.Result:
        t0 = time.monotonic()
        self.last = super().run(text, **kw)
        self.spent += time.monotonic() - t0
        return self.last


def message_seen(built: Any) -> str:
    """The person's message as the model read it: the first user message of the conversation that is not a note of the harness."""
    return next((str(m.get("content")) for m in built.agent.messages if m.get("role") == "user" and not m.get("note")), "")


def run_prompt(arm: Arm, row: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    timeout = float(ctx.extra.get("turn_timeout_s", TURN_TIMEOUT_S))
    root = Path(tempfile.mkdtemp(prefix=f"basalt-e8-{row['id']}-"))
    trial = CaseTrial(arm.name, row["id"], rep)
    client = CountingClient(ctx.client)
    session = built = None
    try:
        task = Task(id=row["id"], category=row["category"], prompt=row["text"], files=dict(row["files"]), checks=list(row["checks"]))
        workspace = make_workspace(task, root / "ws")
        session, events, built = open_session(workspace, client, root / "data", permission_mode="default", allow=["shell_command:python *"], max_turns=20)
        built.agent.sampling = {"seed": ctx.seed_base + rep, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})}
        session.intake = it.Intake("off", client)
        session.optimizer = optimizer = TimedOptimizer(client, bool(arm.settings["optimize"]))
        if optimizer.enabled and side_context(client) <= 0:
            trial.invalid, trial.error = True, "the server has no technical session (start it with --side-slot)"
            return trial
        session.send("m1", row["text"])
        done = events.wait("done", timeout)
        if done is None:
            session.cancel()
            trial.invalid, trial.error = True, "no result before the time limit"
        elif done.get("stop_reason") == "error":
            trial.invalid, trial.error = True, str(done.get("error"))
        clear_bytecode(workspace)
        failed = run_checks(task, workspace, str((done or {}).get("final") or ""), [])
        seen = message_seen(built)
        last = optimizer.last
        scored = score_row(row, seen, last.state, last.reason, failed)
        trial.notes = failed + ([f"rewrite not used: {last.reason} {last.detail}".strip()] if last.state == "rejected" else [])
        trial.success = not failed
        trial.metrics = {**scored, "optimize_seconds": round(optimizer.spent, 2), "prompt_tokens": client.prompt_tokens, "cached_tokens": client.cached_tokens,
                         "completion_tokens": client.completion_tokens, "model_calls": client.calls}
    finally:
        trial.metrics["seconds"] = round(time.monotonic() - t0, 2)
        close_quietly(session)
        if built is not None:
            close_quietly(built)
        if ctx.keep:
            shutil.copytree(root, ctx.keep / f"e8-{arm.name}-{row['id']}-{rep}", dirs_exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e8", title="Optimize prompt: off or on", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=prompts, run_trial=run_prompt, task_id=lambda r: r["id"], default_k=5,
        question="Does rewriting a request in the technical session before the model reads it make the task end right more often without losing what the person wrote, and at what cost in time?")
