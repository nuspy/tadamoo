"""E13: finding an old conversation by other words, with the conversation's card written or not.

Question: when a person asks for a past conversation in words it does not contain (an abbreviation, the other language, a paraphrase), does the card kept with each
conversation (harness/cards.py) put the right conversation among the first hits of `search_past`, where the text alone does not?

Arm `N` has no cards: the index holds the text only. Arm `C` has the cards a good technical session would write (written by hand in the fixture, so no model is
needed). Arm `F` has the cards made by code from the text (`fallback_card`), which is what a conversation gets when there is no technical session. `N~aa` is `N` again;
the search is deterministic, so it is identical to `N` and its gap is zero. Twelve conversations on different subjects and sixteen questions (`fixtures/old_chats.py`):
four use an abbreviation, six the other language, six a paraphrase. A trial puts the twelve conversations in a fresh index, asks one question through the `search_past`
tool and reads the conversation of each hit in order. Nothing is judged by a model and the experiment does not call one.

Win rule, written before the run (k of at least 5, the same result each time): a challenger (`C`, and `F` on its own) beats `N` when its hit@3 (the right conversation
is among the first three conversations returned) is higher than the pooled `N` and `N~aa` rate by more than their gap and by at least 25 points (0.25), and its hit@1 is no lower
than N's (within 10 points or the gap), so that the cards do not bring the right conversation in and push it down. hit@1, the mean rank and the share of questions that
return nothing are reported and not decided on.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from harness import cards
from harness.past import PastIndex, register_past_tools
from harness.tools.registry import ToolRegistry

from .core import Arm, CaseTrial, Context, Experiment, Guard, Rule, with_control
from .fixtures import old_chats as oc

COLUMNS = ("hit3", "hit1", "rank", "nothing", "cards", "seconds")
HITS = 5

ARMS = with_control([
    Arm("N", "no cards: the text only", {"cards": "none"}, "(no card written; the use conversation_card off and the back-fill stopped)"),
    Arm("C", "cards written by the technical session (fixture)", {"cards": "fixture"}, "(the use conversation_card on)"),
    Arm("F", "cards made by code from the text", {"cards": "code"}, "(no technical session: the cards by code)"),
], "N")

RULE = Rule(
    primary="hit3", better="higher", min_effect=0.25, baseline="N", challengers=("C", "F"),
    guards=(Guard("hit1", "higher", 0.10),),
    text="A challenger beats N when its share of questions whose right conversation is among the first three returned by search_past is higher than the pooled N and N/N rate by "
         "more than the N/N gap and by at least 25 points, and its hit@1 is no lower than N's (within 10 points or the gap). hit@1, the mean rank and the share of questions that "
         "return nothing are reported and not decided on.")


def tasks() -> List[Dict[str, str]]:
    return list(oc.QUESTIONS)


def build_index(path: Path, mode: str) -> PastIndex:
    """The twelve conversations in a fresh index, with their cards as `mode` says: "none", "fixture" (written by hand) or "code" (made from the text)."""
    index = PastIndex(path)
    for n, c in enumerate(oc.CONVERSATIONS):
        index.add_session(c["id"], oc.messages_of(c), title=c["title"], kind=c["kind"], at=1_800_000_000.0 + n)
        if mode == "fixture":
            index.set_card(c["id"], c["card"].text(), "model")
        elif mode == "code":
            cards.write_card(index, c["id"])
    return index


def conversations_of(hits: List[Dict[str, Any]]) -> List[str]:
    """The conversations in the order the tool returned them, each once."""
    seen: List[str] = []
    for h in hits:
        if h["conversation"] not in seen:
            seen.append(h["conversation"])
    return seen


def run_trial(arm: Arm, question: Dict[str, str], rep: int, ctx: Context) -> CaseTrial:
    t0 = time.monotonic()
    trial = CaseTrial(arm.name, question["id"], rep)
    with tempfile.TemporaryDirectory(prefix="basalt-e13-") as root:
        index = build_index(Path(root) / "past.db", str(arm.settings["cards"]))
        try:
            registry = ToolRegistry()
            register_past_tools(registry, index)
            got = registry._tools["search_past"].handler(question["text"], limit=HITS)
            order = conversations_of(got["hits"])
            rank = order.index(question["answer"]) + 1 if question["answer"] in order else 0
            trial.success = bool(rank and rank <= 3)
            trial.metrics = {"hit3": 1.0 if rank and rank <= 3 else 0.0, "hit1": 1.0 if rank == 1 else 0.0, "rank": float(rank or HITS + 1),
                             "nothing": 1.0 if not order else 0.0, "cards": float(index.card_counts()["current"])}
            if not trial.success:
                trial.notes.append(f"{question['kind']}: '{question['text']}' returned {', '.join(order[:3]) or 'nothing'}, not {question['answer']}")
        finally:
            index.close()
    trial.metrics["seconds"] = round(time.monotonic() - t0, 3)
    return trial


def experiment() -> Experiment:
    return Experiment(
        id="e13", title="Old chats: finding a conversation by other words, with cards or without", arms=ARMS, rule=RULE, columns=COLUMNS, tasks=tasks, run_trial=run_trial,
        task_id=lambda q: q["id"], default_k=5, needs_model=False,
        question="When a person asks for an old conversation in other words (an abbreviation, the other language, a paraphrase), do the conversation cards put the right one among the first hits?")
