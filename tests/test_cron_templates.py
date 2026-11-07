"""Scheduled message templates: {name} is filled from the context, free text is kept, and an unknown name is left literal with a note."""
from tadamoo.cron_templates import render_template


def test_known_names_are_filled():
    out = render_template("Run {plan_id} ({plan_title}); ready: {ready_step}. Today is {date}.",
                          {"plan_id": "P1", "plan_title": "Cena", "ready_step": "n3", "date": "2026-11-07"})
    assert out == "Run P1 (Cena); ready: n3. Today is 2026-11-07."


def test_an_unknown_name_stays_literal_with_a_note_once():
    out = render_template("The balance is {bilancio} and {bilancio} again, but {nope} is not there.", {"bilancio": "120"})
    assert out == "The balance is 120 and 120 again, but {nope} is not there.\n\n[harness] placeholder(s) not resolved: nope"


def test_free_text_with_no_placeholders_is_unchanged():
    assert render_template("Just run it.", {}) == "Just run it."


def test_values_are_cut_to_a_length_and_structures_become_json():
    long_value = "x" * 300
    out = render_template("{v}", {"v": long_value})
    assert out == "x" * 200
    out = render_template("{v}", {"v": {"a": 1}})
    assert out == '{"a": 1}'


def test_braces_that_are_not_a_known_placeholder_shape_are_left_alone():
    assert render_template("{Plan_ID} {123} {} {plan id}", {"Plan_ID": "x"}) == "{Plan_ID} {123} {} {plan id}"
