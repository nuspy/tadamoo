"""E13 (old chats) runs end to end with no model: the fixture is sound, the arms reach the cards they name, the scorer reads the order of the conversations, and the
pre-registered rule picks the cards written by the session and not the cards made by code."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.eval import cases
from harness.eval.cases import e13_old_chats as e13
from harness.eval.cases.core import Context, decide, by_arm, render_report, results_json, run_experiment
from harness.eval.cases.fixtures import old_chats as oc
from harness.past import PastIndex


def arm(name):
    return next(a for a in e13.ARMS if a.name == name)


def run_all(name):
    return [e13.run_trial(arm(name), q, 1, Context()) for q in e13.tasks()]


def test_the_fixture_has_twelve_conversations_and_sixteen_questions_that_name_a_real_one():
    ids = [c["id"] for c in oc.CONVERSATIONS]
    assert len(ids) == len(set(ids)) == 12 and len(oc.QUESTIONS) == 16 and len({q["id"] for q in oc.QUESTIONS}) == 16
    assert all(q["answer"] in ids for q in oc.QUESTIONS)
    assert sorted(k for k in {q["kind"] for q in oc.QUESTIONS}) == ["abbreviation", "language", "paraphrase"]
    assert [sum(1 for q in oc.QUESTIONS if q["kind"] == k) for k in ("abbreviation", "language", "paraphrase")] == [4, 6, 6]
    assert all(len(c["turns"]) >= 4 and c["card"].also_called and c["card"].title for c in oc.CONVERSATIONS)


def test_the_experiment_is_registered_with_its_arms_rule_and_a_pre_registered_win_rule():
    exp = cases.load("e13")
    assert [a.name for a in exp.arms] == ["N", "C", "F", "N~aa"] and exp.rule.baseline == "N" and exp.rule.challengers == ("C", "F") and exp.rule.primary == "hit3"
    assert not exp.needs_model and exp.question.endswith("?") and exp.default_k >= 5
    assert "Win rule, written before the run" in e13.__doc__ and "25 points" in e13.__doc__
    assert (Path(__file__).resolve().parent.parent / "docs" / "experiments.md").read_text(encoding="utf-8").count("## E13. Old chats") == 1


def test_arm_n_has_no_cards_arm_c_the_fixture_cards_and_arm_f_cards_made_by_code(tmp_path):
    for mode, expected in (("none", 0), ("fixture", 12), ("code", 12)):
        index = e13.build_index(tmp_path / f"{mode}.db", mode)
        assert index.card_counts() == {"total": 12, "current": expected, "model": 12 if mode == "fixture" else 0, "fallback": 12 if mode == "code" else 0}
        index.close()


def test_the_questions_use_other_words_so_the_text_alone_finds_almost_none_and_the_cards_find_all():
    n, c, f = run_all("N"), run_all("C"), run_all("F")
    assert sum(t.metrics["hit3"] for t in n) <= 4 and sum(t.metrics["nothing"] for t in n) >= 10
    assert all(t.success and t.metrics["hit3"] == 1.0 for t in c) and sum(t.metrics["hit1"] for t in c) >= 14
    assert sum(t.metrics["hit3"] for t in f) <= sum(t.metrics["hit3"] for t in c) - 8                 # a card by code cannot translate or expand an abbreviation
    miss = next(t for t in n if not t.success)
    assert miss.notes and "returned" in miss.notes[0] and miss.metrics["rank"] == e13.HITS + 1


def test_the_order_of_the_conversations_decides_the_rank_and_a_conversation_counts_once():
    hits = [{"conversation": "a"}, {"conversation": "a"}, {"conversation": "b"}]
    assert e13.conversations_of(hits) == ["a", "b"]


def test_the_rule_gives_the_win_to_the_cards_of_the_session_and_not_to_the_cards_by_code():
    exp = cases.load("e13")
    trials = run_experiment(exp, Context(plumbing=True), 5)
    assert len(trials) == 16 * 5 * 4 and not any(t.invalid for t in trials)
    verdicts = {v.challenger: v for v in decide(by_arm(trials), exp.rule)}
    assert verdicts["C"].outcome == "C is better than N" and verdicts["F"].outcome.startswith("no difference")
    assert verdicts["C"].effect >= 0.25 and verdicts["F"].effect < 0.25 and verdicts["C"].noise == 0.0
    text = render_report(exp, Context(plumbing=True), trials, 5)
    assert "hit3" in text and "hit1" in text and "`C`" in text
    assert '"hit3"' in results_json(exp, Context(plumbing=True), trials, 5)
