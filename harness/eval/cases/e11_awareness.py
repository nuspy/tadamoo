"""E11: awareness under pressure, off or on (docs/harness-study/awareness.md, sections 1, 4 and 6).

Question: in a job that outgrows a small context window and is folded more than once, does what the harness tells the agent about itself (the fixed block "what
you are", the `self_status` tool, and the course watch with its note that a fold is near) help it keep the early standing rules and facts, without finishing
the job less often?

Arm N has neither the block nor the tool (`self_block` False) and no course watch (`course_watch` False); `N~aa` is N again. Arm S has the block, the tool and
the watch (the defaults). Both have compaction on (`--compaction-threshold 0.85`), so the folds happen in both; only what the agent is told differs. The job,
the small window and the scoring are E5's and E9's (`fixtures/long_turn_repo.py`, `fixtures/context_probes.py`): the final workspace and answer are scored
for the three standing rules (the trailer in the last edits, the closing line, the frozen folder), and fourteen probes are put to the same conversation after
the job, with the tools taken away, eight about the early part that the job needed later and six it did not. Nothing is judged by a model.

Win rule, written before the run (k of at least 5): S beats N when the share of task probes it answers right is higher than the pooled N and N/N share by more
than the N/N gap and by at least 10 points, the standing rules hold at the end no less often than in N (within 10 points or the gap), its job succeeds no less
often than N (within 10 points or the gap), and it takes no more than twice N's seconds.
"""
from __future__ import annotations

from .core import Arm, Experiment, Guard, Rule, with_control
from .e9_context import run_trial, variants

COLUMNS = ("success", "rule_obeyed", "recall_task", "recall_incidental", "probes_right", "compactions", "course_checks", "reread_tokens", "prompt_tokens", "cached_tokens", "tool_calls", "seconds")

COMPACTION = {"compaction": True, "compaction_threshold": 0.85}
ARMS = with_control([
    Arm("N", "no self block, no self_status, no course watch", {**COMPACTION, "self_block": False, "course_watch": False}, "(self_block off, course_watch off)"),
    Arm("S", "self block, self_status and course watch", {**COMPACTION, "self_block": True, "course_watch": True}, "(the defaults)"),
], "N")

RULE = Rule(
    primary="recall_task", better="higher", min_effect=0.10, baseline="N", challengers=("S",),
    guards=(Guard("rule_obeyed", "higher", 0.10), Guard("success", "higher", 0.10), Guard("seconds", "lower", 1.0, relative=True)),
    text="S beats N when the share of task probes it answers right is higher than the pooled N and N/N share by more than the N/N gap and by at least 10 points, the "
         "standing rules hold at the end no less often than in N (within 10 points or the gap), its job succeeds no less often than N (within 10 points or the gap), "
         "and it takes no more than twice N's seconds.")


def experiment() -> Experiment:
    return Experiment(
        id="e11", title="Awareness under pressure: self block and course watch off or on", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=variants, run_trial=run_trial,
        task_id=lambda v: v["id"], default_k=5,
        question="When a long job is folded in a small window, does telling the agent about its limits (block, status tool, fold-near note) keep the early rules and facts?")
