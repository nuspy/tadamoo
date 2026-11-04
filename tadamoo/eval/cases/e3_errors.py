"""E3: the error classifier, off or on.

Question: against a server that answers with transient errors, errors no waiting can cure, and context overflow, does the classifier turn the
turns that never end into visible results without ending a turn that waiting would have saved?

Arm A is the classifier off (`--no-error-classifier`: every model error is retried), arm B is the default, classifier on (docs/harness-gates.md); `A~aa` is A again. The
server is scripted (`scripted_server.py`, scenarios in `fixtures/error_scenarios.py`), so the experiment needs no model and no GPU and its numbers
are the same on any machine: they measure the harness's retry logic, with the waits shrunk to tens of milliseconds and a stall made short.
`real_wait_s` is what the default waits (2, 5, 15, 30, 60, 120, 180, 300 s, the last repeated) would have added for the retries seen.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from tadamoo.client import TadamooClient
from tadamoo.session import BACKOFF_S

from .agentrun import close_quietly, open_session
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import error_scenarios
from .scripted_server import ScriptedServer

COLUMNS = ("success", "never_ends", "wrong_end", "retries", "visible_s", "real_wait_s", "seconds")
DEADLINE_S = 1.5                                       # a turn with no result by then is counted as never ending
WAITS = (0.03, 0.06, 0.12, 0.2)
CLIENT_TIMEOUT_S = 0.3

ARMS = with_control([
    Arm("A", "classifier off", {"classify_errors": False}, "--no-error-classifier"),
    Arm("B", "classifier on (default)", {"classify_errors": True}, "no flag"),
], "A")

RULE = Rule(
    primary="never_ends", better="lower", min_effect=0.10, baseline="A", challengers=("B",),
    guards=(Guard("wrong_end", "lower", 0.0),),
    text="B beats A when the share of turns that never end is lower than the pooled A and A/A share by more than the A/A gap and by at least 10 "
         "points, and B ends no turn on an error that waiting would have cured (wrong_end not above A's, beyond the A/A gap). Whether to turn the "
         "classifier on remains the owner's decision; the numbers say what it buys and what it risks.")


class ShortTimeoutClient(TadamooClient):
    """The real client with a short time limit, so a stalled request ends as a timeout in milliseconds."""

    def chat_completion(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kwargs["timeout"] = min(float(kwargs.get("timeout") or 60.0), CLIENT_TIMEOUT_S)
        return super().chat_completion(*args, **kwargs)


def tasks() -> List[Dict[str, Any]]:
    return error_scenarios.scenarios()


def real_wait(retries: int) -> float:
    return float(sum(BACKOFF_S[min(i, len(BACKOFF_S) - 1)] for i in range(retries)))


def run_scenario(arm: Arm, scenario: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    deadline = float(ctx.extra.get("deadline_s", DEADLINE_S))
    root = Path(tempfile.mkdtemp(prefix=f"tadamoo-e3-{scenario['id']}-"))
    trial = CaseTrial(arm.name, scenario["id"], rep)
    t0 = time.monotonic()
    session = built = None
    try:
        with ScriptedServer(scenario["steps"], forever=scenario["forever"]) as server:
            ws = root / "ws"
            ws.mkdir()
            client = ShortTimeoutClient(server.url)
            session, events, built = open_session(ws, client, root / "data", waits=WAITS, classify_errors=bool(arm.settings["classify_errors"]), max_turns=4)
            sent = time.monotonic()
            session.send("m1", "Say that you are done.")
            done = events.wait("done", deadline)
            visible = time.monotonic() - sent
            retries = events.count("retrying")
            ended = done is not None
            if not ended:
                session.cancel()
                events.wait("done", 3.0)
            reason = (done or {}).get("stop_reason", "")
            ideal = scenario["ideal"]
            ok = ended and (reason == "final" if ideal == "answer" else reason == "error")
            trial.success = ok
            trial.metrics = {"never_ends": 0.0 if ended else 1.0, "wrong_end": 1.0 if ended and reason == "error" and ideal == "answer" else 0.0,
                             "retries": retries, "visible_s": round(visible if ended else deadline, 3), "real_wait_s": real_wait(retries), "requests": server.count()}
            if not ok:
                trial.notes.append("no result before the deadline" if not ended else f"ended as {reason or 'nothing'}; wanted {'an answer' if ideal == 'answer' else 'a visible error'}")
    finally:
        close_quietly(session)
        trial.metrics["seconds"] = round(time.monotonic() - t0, 3)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e3", title="Error classifier: off or on", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=tasks, run_trial=run_scenario, task_id=lambda s: s["id"],
        default_k=5, needs_model=False,
        question="Does the classifier turn the turns that never end into visible results without ending a turn that waiting would have saved?")
