"""E5: compaction inside one long autonomous turn, off or on.

Question: in one request that takes well over a hundred tool calls, does summarising the older steps keep the work and the earliest standing
rules intact where masking alone does not, and what does it cost in tokens read again and in time?

Arm A is `--no-compaction` (masking of old results only), arm B is compaction on (`--compaction-threshold 0.85`, the default) (docs/compaction.md);
`A~aa` is A again. The context window the agent plans for is set the same for every arm (`--window`, default 16384 tokens, small on purpose so the
job outgrows it). Fixture: `fixtures/long_turn_repo.py`, two generated repositories of 60 and 45 modules and a request with three standing rules.
The scorer reads the final workspace and answer: typos left, the ledger lines against the modules' real function counts, the frozen folder, and
whether the rules still held in the last ten edits and the final line.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

from harness import agent_loop as al

from .agentrun import CountingClient, close_quietly, open_agent, snapshot, transcript_measures, write_files
from harness.eval.traces import observe
from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import long_turn_repo as lt

COLUMNS = ("success", "rule_obeyed", "ledger_ok", "typos_left", "compactions", "tool_calls", "reread_tokens", "prompt_tokens", "cached_tokens", "seconds")
WINDOW = 16384
LAST_EDITS = 10
LEDGER_LINE = re.compile(r"^\s*(mod_\d+)\s*:\s*(\d+)\s+functions?\s*$")

ARMS = with_control([
    Arm("A", "compaction off (masking only)", {"compaction": False}, "--no-compaction"),
    Arm("B", "compaction on", {"compaction": True, "compaction_threshold": 0.85}, "--compaction-threshold 0.85 (the default)"),
], "A")

RULE = Rule(
    primary="success", better="higher", min_effect=0.20, baseline="A", challengers=("B",),
    guards=(Guard("rule_obeyed", "higher", 0.10), Guard("seconds", "lower", 1.0, relative=True)),
    text="B beats A when its success rate (every typo fixed, the ledger right in at least 95% of its lines, frozen/ untouched) is higher than the pooled "
         "A and A/A rate by more than the A/A gap and by at least 20 points, the standing rules still hold at the end no less often than in A (within 10 "
         "points or the A/A gap), and B takes no more than twice A's seconds. Tokens read again (prompt minus cached) are reported and not decided on.")


def variants() -> List[Dict[str, Any]]:
    return lt.VARIANTS


def _last_line(text: str) -> str:
    body = [ln for ln in text.splitlines() if ln.strip()]
    return body[-1].strip() if body else ""


def score(repo: lt.Repo, files: Dict[str, str], answer: str, edit_order: Sequence[str]) -> Dict[str, float]:
    """Scores the final workspace of one long turn. `files` is the workspace as it ended, `edit_order` the pkg modules in the order they were last written."""
    pkg = {n: files.get(f"pkg/{n}.py", "") for n in repo.functions}
    typos_left = sum(1 for text in pkg.values() if lt.TYPO in text)
    frozen_intact = all(files.get(p) == repo.files[p] for p in repo.frozen)
    ledger: Dict[str, int] = {}
    for line in files.get("LEDGER.md", "").splitlines():
        m = LEDGER_LINE.match(line)
        if m:
            ledger[m.group(1)] = int(m.group(2))
    ledger_ok = sum(1 for n, f in repo.functions.items() if ledger.get(n) == f) / max(1, len(repo.functions))
    last = [n for n in edit_order if n in repo.typo_modules][-LAST_EDITS:]
    trailers = (sum(_last_line(pkg[n]) == lt.TRAILER for n in last) / len(last)) if last else 0.0
    final_ok = _last_line(answer) == lt.FINAL_LINE
    obeyed = trailers == 1.0 and final_ok and frozen_intact
    ok = typos_left == 0 and ledger_ok >= 0.95 and frozen_intact
    return {"success": 1.0 if ok else 0.0, "rule_obeyed": 1.0 if obeyed else 0.0, "ledger_ok": ledger_ok, "typos_left": typos_left,
            "trailers_late": trailers, "final_line": 1.0 if final_ok else 0.0, "frozen_intact": 1.0 if frozen_intact else 0.0}


def notes_of(s: Dict[str, float]) -> List[str]:
    out = []
    if s["typos_left"]:
        out.append(f"{int(s['typos_left'])} module(s) still have the misspelling")
    if s["ledger_ok"] < 0.95:
        out.append(f"the ledger is right in {s['ledger_ok'] * 100:.0f}% of its lines")
    if not s["frozen_intact"]:
        out.append("something under frozen/ was changed")
    if not s["rule_obeyed"]:
        out.append(f"a standing rule did not hold at the end (trailer in the last edits {s['trailers_late'] * 100:.0f}%, final line {'yes' if s['final_line'] else 'no'})")
    return out


def run_long(arm: Arm, variant: Dict[str, Any], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix=f"basalt-e5-{variant['id']}-"))
    trial = CaseTrial(arm.name, variant["id"], rep)
    client = CountingClient(ctx.client)
    built = None
    try:
        repo = lt.build(variant["modules"], variant["seed"])
        workspace = write_files(root / "ws", repo.files)
        built = open_agent(workspace, client, root / "data", context_window=int(ctx.extra.get("window", WINDOW)), max_turns=int(ctx.extra.get("max_turns", 500)),
                           max_seconds=float(ctx.extra.get("max_seconds", 3 * 3600)), **arm.settings)
        built.agent.sampling = {"seed": ctx.seed_base + rep, **({"temperature": ctx.temperature} if ctx.temperature is not None else {})}
        turns = built.agent.run(lt.request(variant["modules"]))
        reason = built.agent.stop_reason
        final = al.answer_text(turns)
        order = sorted(repo.functions, key=lambda n: (workspace / "pkg" / f"{n}.py").stat().st_mtime_ns)
        scored = score(repo, snapshot(workspace), final, order)
        measures = transcript_measures(turns)
        trial.trace, trial.components = observe(turns, built.agent, built.audit, client)
        trial.success, trial.notes = bool(scored["success"]), notes_of(scored)
        comp = getattr(getattr(built.agent.context, "compaction", None), "count", 0)
        trial.metrics = {**{k: v for k, v in scored.items() if k != "success"}, "compactions": comp, "tool_calls": measures["tool_calls"], "turns": measures["turns"],
                         "wasted_turns": measures["wasted_turns"], "prompt_tokens": client.prompt_tokens, "cached_tokens": client.cached_tokens,
                         "reread_tokens": client.prompt_tokens - client.cached_tokens, "completion_tokens": client.completion_tokens}
        if reason == al.ERROR:
            trial.invalid, trial.error = True, built.agent.error
        elif reason != al.FINAL:
            trial.notes.insert(0, f"the run ended as {reason}, not with an answer")
    finally:
        trial.metrics["seconds"] = round(time.monotonic() - t0, 2)
        if built is not None:
            close_quietly(built)
        if ctx.keep:
            shutil.copytree(root, ctx.keep / f"e5-{arm.name}-{variant['id']}-{rep}", dirs_exist_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e5", title="Compaction inside one long turn: off or on", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=variants, run_trial=run_long,
        task_id=lambda v: v["id"], default_k=5,
        question="In one request of a hundred tool calls or more, does summarising the older steps keep the work and the earliest rules intact, and at what cost?")
