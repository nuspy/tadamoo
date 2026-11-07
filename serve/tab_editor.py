"""Changing a plan or a cron job, or making a new cron job, by telling the harness what to do, from the tab of that plan or job.

One conversation with the harness (named `tab-editor`) does it with its own plan and cron tools, in Automatic: the person asked for the change
by typing it, so there is nothing more to confirm, and a change the plans keep for the person's yes stays a proposal in the plan's own list.
The conversation is made the first time and used again after."""
from __future__ import annotations

from typing import Any, Dict

from serve.agent_runs import RunError

ALIAS = "tab-editor"
KINDS = ("plan", "cron")
TARGET_MAX, INSTRUCTION_MAX = 120, 4000
RULES = (
    "You change plans and cron jobs, and make new cron jobs, on the person's instruction, given from the tab of that plan or job.\n"
    "- Use only the plan_* and cron_* tools (load them with load_capability if they are not listed). Read the plan or job first, change only what was asked, and "
    "do not touch any other plan or job.\n"
    "- A new cron job is made with cron_create from what the person said: name it by what it does, take its time and its task from their words, ask nothing "
    "they did not leave open, and say its name and when it will first run.\n"
    "- A step you add to a plan takes the person's wording in its title and description; do not invent details they did not give.\n"
    "- Answer in one or two sentences in the person's language: what changed, or why it could not be done and what is needed.\n"
    "- Do nothing outside plans and cron jobs: no files, no commands, no web."
)


def instruct(sessions: Any, kind: Any, target: Any, instruction: Any) -> Dict[str, Any]:
    """Sends the instruction to the editing conversation (made if there is none) and says which one to follow."""
    if kind not in KINDS:
        raise RunError("Say whether it is a plan or a cron job.", "bad_kind")
    target = target if isinstance(target, str) else ""
    if len(target) > TARGET_MAX or (not target.strip() and kind != "cron"):         # a cron job with no target is a new one
        raise RunError("Say which plan or job to change.", "bad_target")
    if not isinstance(instruction, str) or not instruction.strip():
        raise RunError("Write what to change.", "text_missing")
    if len(instruction) > INSTRUCTION_MAX:
        raise RunError(f"Keep the instruction under {INSTRUCTION_MAX} characters.", "too_long")
    sid = sessions.resolve(ALIAS)
    if sid is None:
        sid = sessions.create("", mode="auto", alias=ALIAS, title="Plans and cron jobs", operating_rules=RULES)["id"]
    what = "plan" if kind == "plan" else "cron job"
    sessions.send(sid, f"Change the {what} `{target.strip()}`: {instruction.strip()}" if target.strip() else f"Make a new cron job: {instruction.strip()}")
    return {"session": sid}
