"""ask_user v2: the repairs of a model's call, the check of the person's answer, the text the model reads, the unattended picks, the ask guard."""
import json

import pytest

from harness import questions as qs
from harness.questions import AskGuard, QuestionError, auto_answers, check_answer, format_answer, normalise, parse_answer


def one(**kw):
    return normalise({"questions": [{"text": "Which database?", "kind": "single", "options": [{"label": "PostgreSQL"}, {"label": "SQLite"}], **kw}]})["questions"][0]


def test_a_good_call_gets_ids_and_defaults():
    q = one(options=[{"label": "PostgreSQL", "description": "used already", "recommended": True}, {"label": "SQLite"}])
    assert q["id"] == "q1" and q["kind"] == "single" and q["allow_other"] and q["required"]
    assert [o["id"] for o in q["options"]] == ["o1", "o2"] and q["options"][0]["recommended"] and q["options"][0]["description"] == "used already"


def test_repairs_of_options():
    q = one(options=["Alpha", {"label": "alpha"}, {"label": "Beta (Recommended)"}, {"label": "Other"}, {"label": "Altro"}, {"label": ""}, 7, {"label": "x" * 90}])
    labels = [o["label"] for o in q["options"]]
    assert labels[:2] == ["Alpha", "Beta"] and q["options"][1]["recommended"] and "Other" not in labels and "Altro" not in labels
    assert len(labels[2]) == qs.LABEL_CHARS and labels[2].endswith("…")                    # cut with an ellipsis, not refused
    many = one(options=[{"label": f"o{i}"} for i in range(9)] + [{"label": "best", "recommended": True}])
    assert len(many["options"]) == qs.MAX_OPTIONS and any(o["label"] == "best" for o in many["options"])
    two = one(options=[{"label": "a", "recommended": True}, {"label": "b", "recommended": True}])
    assert [bool(o.get("recommended")) for o in two["options"]] == [True, False]                # a single keeps the first
    multi = one(kind="multi", options=[{"label": "a", "recommended": True}, {"label": "b", "recommended": True}])
    assert all(o.get("recommended") for o in multi["options"])


def test_other_stays_when_the_screen_adds_none():
    q = one(allow_other=False, options=[{"label": "Yes please"}, {"label": "Other"}])
    assert [o["label"] for o in q["options"]] == ["Yes please", "Other"] and not q["allow_other"]


def test_one_option_or_none_makes_the_question_free_and_a_missing_kind_follows_the_options():
    assert one(options=[{"label": "only"}])["kind"] == "free"
    assert normalise({"questions": [{"text": "Why?"}]})["questions"][0]["kind"] == "free"
    assert normalise({"questions": [{"text": "Which?", "options": ["a", "b"]}]})["questions"][0]["kind"] == "single"
    assert normalise({"questions": [{"text": "Which?", "kind": "choose", "options": ["a", "b"]}]})["questions"][0]["kind"] == "single"


def test_options_written_in_the_question_text_are_parsed():
    q = normalise({"questions": [{"text": "Which one?\nA) the first\nB) the second\nC. the third"}]})["questions"][0]
    assert q["text"] == "Which one?" and [o["label"] for o in q["options"]] == ["the first", "the second", "the third"] and q["kind"] == "single"


def test_questions_are_capped_ids_made_unique_and_strings_accepted():
    out = normalise({"intro": "Because.", "questions": ["One?", {"text": "Two?", "id": "q1"}, {"text": "Three?", "id": "mine"}, "Four?", "Five?"]})
    assert [q["id"] for q in out["questions"]] == ["q1", "q2", "mine", "q4"] and out["notes"] and out["intro"] == "Because."
    assert len(normalise({"questions": [{"text": "w" * 190}] * 4})["questions"]) <= qs.MAX_QUESTIONS


def test_control_characters_and_overlong_text_are_cleaned():
    q = normalise({"questions": [{"text": "A\x00B‮​C " + "z" * 400}]})["questions"][0]
    assert "\x00" not in q["text"] and "‮" not in q["text"] and len(q["text"]) == qs.TEXT_CHARS


def test_a_free_and_a_confirm_question_keep_their_extras():
    free = normalise({"questions": [{"text": "Name?", "kind": "free", "placeholder": "a name", "default": "Rex", "involves": ["people: the vet"]}]})["questions"][0]
    assert free["placeholder"] == "a name" and free["default"] == "Rex" and free["involves"] == ["people: the vet"] and not free["allow_other"]
    ok = normalise({"questions": [{"text": "Delete it?", "kind": "confirm", "recommended": False}]})["questions"][0]
    assert [o["id"] for o in ok["options"]] == ["yes", "no"] and ok["recommended"] is False


def test_no_usable_question_is_one_error_line_with_an_example():
    for bad in ({"questions": []}, {"questions": [{"text": "  "}, 5]}, {"questions": "x"}, {}):
        with pytest.raises(QuestionError, match="Example"):
            normalise(bad)


def answer(payload, **per):
    return parse_answer("ask:" + json.dumps({"v": 1, "status": per.pop("status", "submitted"), "answers": per}))


def card():
    return normalise({"questions": [
        {"text": "Which database?", "kind": "single", "options": [{"label": "PostgreSQL", "recommended": True}, {"label": "SQLite"}]},
        {"text": "Which sections?", "kind": "multi", "options": [{"label": "Introduction"}, {"label": "Conclusion"}, {"label": "Glossary"}], "required": False},
        {"text": "Anything out of scope?", "kind": "free", "required": False}, {"text": "Publish it?", "kind": "confirm"}]})


def test_the_answer_is_checked_against_the_stored_questions():
    p = card()
    ok = check_answer(p, answer(p, q1={"picked": ["o1"]}, q2={"picked": ["o1", "o2"], "text": "a glossary"}, q4={"picked": ["no"]}))
    assert ok["answers"]["q1"] == {"picked": ["o1"], "text": ""} and ok["answers"]["q3"] == {"picked": [], "text": ""}
    bad = [
        dict(q1={"picked": ["o9"]}, q4={"picked": ["yes"]}),                                  # not offered
        dict(q1={"picked": ["o1", "o2"]}, q4={"picked": ["yes"]}),                            # two on a single
        dict(q1={"picked": []}, q4={"picked": ["yes"]}),                                      # required and empty
        dict(q1={"picked": ["o1"], "text": "mine"}, q4={"picked": ["yes"]}),                  # a pick and Other together
        dict(q1={"picked": ["o1"]}, q3={"picked": ["o1"]}, q4={"picked": ["yes"]}),           # a pick on a free question
        dict(q1={"picked": ["o1"]}, q4={"picked": []}),                                       # confirm needs yes or no
        dict(q1={"picked": ["o1"]}, q4={"picked": ["yes"]}, q7={"picked": []}),               # a question that was not asked
        dict(q1={"picked": ["o1"]}, q3={"text": "x" * 501}, q4={"picked": ["yes"]}),          # too long
        dict(q1={"picked": ["o1", "o1"]}, q4={"picked": ["yes"]}),                            # duplicates
    ]
    for case in bad:
        with pytest.raises(QuestionError):
            check_answer(p, answer(p, **case))
    clean = check_answer(p, answer(p, q1={"picked": ["o1"]}, q3={"text": "no\x00 pdf"}, q4={"picked": ["yes"]}))
    assert clean["answers"]["q3"]["text"] == "no pdf"


def test_an_unfitting_answer_string_is_refused():
    for value in ("hello", "ask:{", "ask:[]", 'ask:{"status":"maybe"}', "ask:" + "x" * 5000, None):
        with pytest.raises(QuestionError):
            parse_answer(value)


def test_the_text_the_model_reads_has_labels_not_ids_and_the_four_outcomes():
    p = card()
    ok = check_answer(p, answer(p, q1={"picked": ["o1"]}, q2={"picked": ["o1", "o2"], "text": "a glossary"}, q4={"picked": ["no"]}))
    text = format_answer(p, ok)
    assert text.splitlines()[0] == qs.ANSWER_HEADER and text.splitlines()[-1] == qs.GO_ON
    assert "1. Which database? -> PostgreSQL (your recommended option)" in text
    assert '2. Which sections? -> Introduction; Conclusion; Other: "a glossary"' in text
    assert "3. Anything out of scope? -> (no answer)" in text and "4. Publish it? -> No" in text and "o1" not in text
    assert format_answer(p, check_answer(p, answer(p, status="skipped"))) == qs.SKIP_TEXT
    assert format_answer(p, check_answer(p, answer(p, status="chat"))) == qs.CHAT_TEXT
    assert "-> Rex" in format_answer(normalise({"questions": [{"text": "Name?", "kind": "free"}]}),
                                     {"status": "submitted", "answers": {"q1": {"picked": [], "text": "Rex"}}})


def test_unattended_picks_recommended_default_and_recommended_side_and_never_answers_a_confirm_without_one():
    p = normalise({"questions": [
        {"text": "Db?", "kind": "single", "options": [{"label": "A"}, {"label": "B", "recommended": True}]},
        {"text": "Parts?", "kind": "multi", "options": [{"label": "a", "recommended": True}, {"label": "b", "recommended": True}, {"label": "c"}]},
        {"text": "Name?", "kind": "free", "default": "Rex"}, {"text": "Safe?", "kind": "confirm", "recommended": True}]})
    p2 = normalise({"questions": [{"text": "Sure?", "kind": "confirm"}, {"text": "Pick?", "kind": "single", "options": [{"label": "A"}, {"label": "B"}]},
                                  {"text": "Call the vet?", "kind": "single", "options": [{"label": "A", "recommended": True}, {"label": "B"}], "involves": ["people: the vet"]}]})
    checked, tags = auto_answers(p)
    assert tags == {"q1": "recommended", "q2": "recommended", "q3": "default", "q4": "recommended"}
    assert checked["answers"]["q1"]["picked"] == ["o2"] and checked["answers"]["q2"]["picked"] == ["o1", "o2"] and checked["answers"]["q4"]["picked"] == ["yes"]
    text = format_answer(p, checked, tags)
    assert "Db? -> B (chosen automatically: your recommended option)" in text and "Name? -> Rex (default)" in text
    checked2, tags2 = auto_answers(p2)
    assert tags2 == {"q1": "none", "q2": "none", "q3": "parked"} and checked2["answers"]["q1"]["picked"] == []
    text2 = format_answer(p2, checked2, tags2)
    assert "decide yourself, conservatively" in text2 and "the person will answer later" in text2
    lines = qs.assumption_lines(p, checked, tags)
    assert len(lines) == 4 and "recommended" in lines[0] and not qs.assumption_lines(p2, checked2, {"q3": "parked"})


def test_the_parked_form_of_a_question():
    q = normalise({"questions": [{"text": "Call the vet?", "kind": "single", "options": [{"label": "Yes"}, {"label": "No"}], "involves": ["people: the vet"]}]})["questions"][0]
    parked = qs.parked_payload(q)
    assert parked["kind"] == "parked_question" and parked["options"] == [{"label": "Yes", "value": "o1"}, {"label": "No", "value": "o2"}] and parked["free_prefix"] == "other:"


def test_the_guard_limits_asks_per_request_refuses_repeats_and_shows_a_plain_question_after_two_failures():
    g = AskGuard()
    call = lambda text: {"questions": [{"text": text, "kind": "free"}]}
    g.check(call("What is the name?"), "do it")
    with pytest.raises(QuestionError, match="already asked"):
        g.check(call("what is the NAME"), "do it")
    g.check(call("And the colour?"), "do it")
    with pytest.raises(QuestionError, match="Do not ask again"):
        g.check(call("A third thing?"), "do it")
    answered = qs.ANSWER_HEADER + "\n1. x -> y"
    with pytest.raises(QuestionError, match="Do not ask again"):
        g.check(call("A third thing?"), answered)                         # an answer to a card is not a new request
    g.check(call("A third thing?"), "now another request")                # a new request starts the count again
    h = AskGuard()
    unusable = {"questions": [{"options": ["Where should it go, then?"]}]}
    with pytest.raises(QuestionError):
        h.check(unusable, "go")
    shown = h.check(unusable, "go")                                       # the second unusable call shows the model's words as one plain question
    assert shown["questions"][0]["kind"] == "free" and shown["questions"][0]["text"] == "Where should it go, then?"
    with pytest.raises(QuestionError):
        AskGuard().check({"questions": ["", {"text": ""}]}, "go")        # no words at all: still an error, the model is told
