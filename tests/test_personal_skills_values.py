"""Personal skills: the # markers of a request, the types of a parameter and the dates said in words."""
from datetime import datetime

import pytest

from harness.personal_skills import markers
from harness.personal_skills.values import ValueError_, check_spec, parse_value, parse_when, resolve

NOW = datetime(2026, 10, 26, 14, 30, 15)             # a Monday


def test_a_marker_is_a_phrase_between_two_hashes_with_a_space_inside():
    text = "trova le notizie # di oggi # riguardanti # la guerra in Ucraina # e riassumile"
    found = markers.find(text)
    assert [m.value for m in found] == ["di oggi", "la guerra in Ucraina"]
    assert markers.clean(text) == "trova le notizie di oggi riguardanti la guerra in Ucraina e riassumile"


def test_a_marker_can_name_its_parameter():
    (mark,) = markers.find("meteo a # città: Roma # domani")
    assert (mark.name, mark.value) == ("città", "Roma")
    (plain,) = markers.find("ore # 10:30 #")                      # a colon without a space after it is not a name
    assert (plain.name, plain.value) == (None, "10:30")


@pytest.mark.parametrize("text", [
    "# Title with no closing", "#hashtag and #another", "issue #12 is open", "use `# not a mark #` here", "```\n# a #\n```",
    "a \\# b \\# c", "# #", "#include <stdio.h>\n#include <math.h>", "five#six #seven",
])
def test_what_looks_like_a_hash_but_is_not_a_marker_is_left_alone(text):
    assert markers.find(text) == []
    assert markers.clean(text) == text.replace("\\#", "#")


def test_markers_are_found_next_to_each_other_and_on_each_line():
    assert [m.value for m in markers.find("# a # # b #\nand # c d #")] == ["a", "b", "c d"]


def test_types_are_read_the_way_a_person_writes_them():
    assert parse_value({"type": "number"}, " 3,5 ") == 3.5
    assert parse_value({"type": "number"}, "1.234,5") == 1234.5
    assert parse_value({"type": "number"}, "12") == 12
    assert parse_value({"type": "bool"}, "sì") is True and parse_value({"type": "bool"}, "no") is False
    assert parse_value({"type": "choice", "choices": ["Rosso", "Blu"]}, "blu") == "Blu"
    assert parse_value({"type": "list", "item_type": "number"}, "1; 2; 3,5") == [1, 2, 3.5]
    assert parse_value({"type": "list", "item_type": "number"}, "1, 2, 3") == [1, 2, 3]
    assert parse_value({"type": "list", "item_type": "text"}, "a, b\nc") == ["a, b", "c"]           # lines or ; separate when there are some
    assert parse_value({"type": "list", "item_type": "text"}, "a, b, c") == ["a", "b", "c"]
    assert parse_value({"type": "list", "item_type": "mixed"}, "1; due; 3") == [1, "due", 3]
    assert parse_value({"type": "text"}, "a very\nlong text") == "a very\nlong text"
    for spec, raw in (({"type": "number"}, "abc"), ({"type": "bool"}, "forse"), ({"type": "choice", "choices": ["a"]}, "b"),
                      ({"type": "date"}, "not a day"), ({"type": "list", "item_type": "number"}, "1; x")):
        with pytest.raises(ValueError_):
            parse_value(spec, raw)


@pytest.mark.parametrize("words,expected", [
    ("oggi", "2026-10-26T00:00"), ("ieri", "2026-10-25T00:00"), ("domani", "2026-10-27T00:00"), ("dopodomani", "2026-10-28T00:00"),
    ("l'altro ieri", "2026-10-24T00:00"), ("yesterday", "2026-10-25T00:00"), ("tomorrow", "2026-10-27T00:00"),
    ("adesso", "2026-10-26T14:30:15"), ("now", "2026-10-26T14:30:15"),
    ("tra 3 giorni", "2026-10-29T00:00"), ("in 2 weeks", "2026-11-09T00:00"), ("3 giorni fa", "2026-10-23T00:00"), ("2 days ago", "2026-10-24T00:00"),
    ("tra 2 ore", "2026-10-26T16:30:15"), ("tra 1 mese", "2026-11-26T00:00"),
    ("lunedì prossimo", "2026-11-02T00:00"), ("next friday", "2026-10-30T00:00"), ("venerdì scorso", "2026-10-23T00:00"), ("last monday", "2026-10-19T00:00"),
    ("inizio mese", "2026-10-01T00:00"), ("fine mese", "2026-10-31T00:00"), ("inizio settimana", "2026-10-26T00:00"),
    ("2026-12-25", "2026-12-25T00:00"), ("25/12/2026", "2026-12-25T00:00"), ("25 dicembre 2026", "2026-12-25T00:00"), ("dec 25, 2026", "2026-12-25T00:00"),
    ("domani 09:15", "2026-10-27T09:15"), ("2026-12-25 18:00", "2026-12-25T18:00"), ("ieri alle 8:05", "2026-10-25T08:05"),
])
def test_dates_said_in_words_are_worked_out_from_now(words, expected):
    got = parse_when(words, NOW)
    assert got is not None and got.isoformat(timespec="seconds" if got.second else "minutes") == expected


@pytest.mark.parametrize("words", ["", "boh", "31/02/2026", "3 giorni", "tra x giorni", "fine"])
def test_what_is_not_a_date_is_not_one(words):
    assert parse_when(words, NOW) is None


def test_a_date_parameter_keeps_an_iso_date_and_a_datetime_an_iso_moment():
    assert parse_value({"type": "date"}, "ieri", NOW) == "2026-10-25"
    assert parse_value({"type": "datetime"}, "domani 09:15", NOW) == "2026-10-27T09:15"
    assert parse_value({"type": "datetime"}, "adesso", NOW) == "2026-10-26T14:30:15"


def test_a_default_in_words_is_worked_out_each_time_the_skill_runs():
    specs = [{"name": "day", "type": "date", "default": "ieri"}, {"name": "topic", "type": "text"}, {"name": "n", "type": "number", "default": 5}]
    first, errors = resolve(specs, {"topic": "war"}, NOW)
    assert errors == {} and first["day"] == {"type": "date", "value": "2026-10-25"} and first["n"]["value"] == 5
    later, _ = resolve(specs, {"topic": "war"}, datetime(2026, 11, 3, 8, 0))
    assert later["day"]["value"] == "2026-11-02"                                       # the same skill, another day


def test_what_is_wrong_or_unknown_is_said_for_each_parameter_and_a_missing_value_does_not_stop_the_run():
    specs = [{"name": "topic", "type": "text"}, {"name": "when", "type": "date"}, {"name": "extra", "type": "text", "required": False}]
    values, errors = resolve(specs, {"when": "never", "nope": "x"}, NOW)
    assert set(errors) == {"when", "nope"} and values["extra"] == {"type": "text", "value": None}
    assert values["topic"] == {"type": "text", "value": None, "missing": True}
    assert "not a date" in errors["when"] and "not a parameter" in errors["nope"]


def test_a_value_left_out_is_the_default_then_the_last_one_used_then_missing():
    specs = [{"name": "a", "type": "text", "default": "d"}, {"name": "b", "type": "number"}, {"name": "c", "type": "text"}]
    values, errors = resolve(specs, {}, NOW, {"a": "old", "b": 7})
    assert errors == {} and values["a"]["value"] == "d" and values["b"]["value"] == 7 and values["c"].get("missing") is True
    assert resolve(specs, {"b": 9}, NOW, {"b": 7})[0]["b"]["value"] == 9
    stale, errors = resolve([{"name": "d", "type": "date"}], {}, NOW, {"d": "not a date"})
    assert errors == {} and stale["d"]["missing"] is True


def test_a_definition_that_cannot_work_is_refused():
    assert check_spec({"name": "ok", "type": "date", "default": "ieri"}) == []
    for bad in ({"name": "Bad Name"}, {"name": "a", "type": "color"}, {"name": "a", "type": "choice"}, {"name": "a", "type": "list", "item_type": "x"},
                {"name": "a", "type": "number", "default": "abc"}, "text"):
        assert check_spec(bad), bad
