"""The check on an answer: the card of a request, what is measured, the one second try, and what costs nothing more."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.agent_loop import AgentLoop, FINAL
from harness.pipeline import Pipeline, extract_card, feedback, measure
from harness.tools.registry import ToolRegistry
from test_agent_loop import ScriptedClient, say


def kinds(text):
    return {(c.kind, c.value) for c in extract_card(text).constraints}


def test_the_card_finds_what_can_be_measured_in_italian_and_english():
    assert ("max_words", 50) in kinds("Riassumi il testo in massimo 50 parole")
    assert ("max_words", 30) in kinds("Summarize this in at most 30 words")
    assert ("min_words", 200) in kinds("scrivi almeno 200 parole sul mare")
    assert ("exact_words", 10) in kinds("una frase di esattamente 10 parole")
    assert ("max_chars", 280) in kinds("un tweet, massimo 280 caratteri")
    assert ("items", 3) in kinds("dammi un elenco puntato di 3 idee")
    assert ("language", "en") in kinds("rispondi in inglese: come si cucina il riso?")
    assert ("json", True) in kinds("restituisci solo JSON")
    assert ("forbidden", "bello") in kinds("descrivi la casa senza usare la parola bello")


def test_a_request_without_constraints_gives_an_empty_card_and_costs_nothing():
    card = extract_card("che ore sono a Tokyo?")
    assert card.constraints == [] and Pipeline("full").verify(card, "le 9").ok


def test_qualitative_constraints_are_kept_apart_and_capped():
    card = extract_card("scrivi una mail in tono formale, per un manager, senza giri di parole")
    assert {c.kind for c in card.qualitative} == {"qualitative"} and 1 <= len(card.qualitative) <= 3 and card.measurable == []


@pytest.mark.parametrize("request_text,answer,expected", [
    ("in massimo 5 parole", "uno due tre quattro cinque sei", 1),
    ("in massimo 5 parole", "uno due tre", 0),
    ("almeno 4 parole", "uno due", 1),
    ("esattamente 3 parole", "uno due tre", 0),
    ("elenco puntato di 2 voci", "- a\n- b\n- c", 1),
    ("elenco puntato di 2 voci", "- a\n- b", 0),
    ("solo JSON", '```json\n{"a": 1}\n```', 0),
    ("solo JSON", "ecco: {a}", 1),
    ("senza usare la parola mare", "il Mare è grande", 1),
    ("rispondi in italiano", "the sea is big and the water is blue for all of us", 1),
    ("rispondi in italiano", "il mare è grande e l'acqua è blu per tutti noi", 0),
])
def test_what_is_measured(request_text, answer, expected):
    assert len(measure(extract_card(request_text), answer)) == expected


class Judge:
    def __init__(self, word):
        self.word, self.asked = word, []

    def __call__(self, question, options, context):
        self.asked.append((question, dict(options), context))
        return self.word


def test_full_mode_judges_only_when_the_card_has_qualitative_constraints_and_counts_the_calls():
    card = extract_card("scrivi una mail in tono formale")
    j = Judge("no")
    v = Pipeline("full", j).verify(card, "ciao bro!")
    assert not v.ok and v.judge_calls == 1 and "tono formale" in v.violations[0] and "ciao bro" in j.asked[0][2]
    quiet = Judge("yes")
    assert Pipeline("full", quiet).verify(extract_card("in massimo 5 parole"), "uno due").judge_calls == 0 and quiet.asked == []


def test_light_and_off_never_call_the_judge_and_an_unsure_judge_does_not_block():
    card = extract_card("scrivi in tono formale")
    j = Judge("no")
    assert Pipeline("light", j).verify(card, "ciao").ok and j.asked == []
    assert Pipeline("off", j).verify(card, "ciao").ok
    assert Pipeline("full", Judge(None)).verify(card, "ciao").ok
    with pytest.raises(ValueError):
        Pipeline("sometimes")


def agent(client, pipeline):
    return AgentLoop(client, ToolRegistry(), pipeline=pipeline, on_event=EVENTS.append)


EVENTS = []


@pytest.fixture(autouse=True)
def _events():
    EVENTS.clear()


def test_a_failed_answer_gets_one_second_try_with_the_list_and_then_stops():
    client = ScriptedClient(say("uno due tre quattro cinque sei sette"), say("uno due tre quattro cinque sei"), say("never asked"))
    loop = agent(client, Pipeline("light"))
    loop.run("Rispondi in massimo 5 parole")
    assert loop.stop_reason == FINAL and len(client.sent) == 2 and len(client.steps) == 1       # a third call never happens
    retry_msg = client.sent[1][-1]
    assert retry_msg["role"] == "user" and "at most 5 were asked" in retry_msg["content"] and "7 words" in retry_msg["content"]
    verifies = [e for e in EVENTS if e["type"] == "verify"]
    assert [v["retry"] for v in verifies] == [True, False] and verifies[1]["ok"] is False and verifies[1]["violations"]    # still unmet: said so


def test_a_second_try_that_fixes_it_ends_clean_and_the_person_message_is_not_replaced():
    client = ScriptedClient(say("uno due tre quattro cinque sei"), say("uno due tre"))
    loop = agent(client, Pipeline("light"))
    loop.run("in massimo 5 parole")
    assert [e["ok"] for e in EVENTS if e["type"] == "verify"] == [False, True]
    assert loop.last_user_text == "in massimo 5 parole"


def test_light_mode_and_no_card_add_no_model_call():
    client = ScriptedClient(say("ciao, come stai"))
    loop = agent(client, Pipeline("light"))
    loop.run("saluta")
    assert len(client.sent) == 1 and [e for e in EVENTS if e["type"] == "verify"] == []
    client = ScriptedClient(say("ok"))
    agent(client, None).run("in massimo 1 parola")
    assert len(client.sent) == 1


def test_a_new_request_gets_its_own_card_and_its_own_second_try():
    client = ScriptedClient(say("a b c d"), say("a b"), say("x y z w"), say("x y"))
    loop = agent(client, Pipeline("light"))
    loop.run("in massimo 3 parole")
    loop.run("in massimo 2 parole")
    assert len(client.sent) == 4


def test_the_feedback_holds_only_the_list():
    text = feedback(["It has 7 words; at most 5 were asked."])
    assert "- It has 7 words" in text and "reply with the corrected answer only" in text
