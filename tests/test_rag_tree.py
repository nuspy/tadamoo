"""The tree of an archive's documents: its shape, the three states with their cascade, what is followed, and the schedules (intervals and occurrences) with what is due."""
import datetime as dt

import pytest

from tadamoo.rag.sources import tree


def ts(*a):
    return dt.datetime(*a).timestamp()


# -- the frequency --------------------------------------------------------------------------------

def test_a_frequency_is_checked_field_by_field_and_the_error_names_what_is_wrong():
    assert tree.clean_freq({"mode": "manual"}) == {"mode": "manual"} and tree.clean_freq({"mode": "parts"}) == {"mode": "parts"}
    assert tree.clean_freq({"mode": "interval", "every": 6, "unit": "hours"}) == {"mode": "interval", "every": 6, "unit": "hours"}
    assert tree.clean_freq({"mode": "occurrence", "type": "weekday", "days": [3, 0, 3], "at": "7:5"}) == {"mode": "occurrence", "type": "weekday", "days": [0, 3], "at": "07:05"}
    assert tree.clean_freq({"mode": "occurrence", "type": "monthday", "days": [15, "last", 1]})["days"] == [1, 15, "last"]
    assert tree.clean_freq({"mode": "occurrence", "type": "nth_weekday", "nth": -1, "weekday": 4}) == {"mode": "occurrence", "type": "nth_weekday", "at": "03:00", "nth": -1, "weekday": 4}
    bad = [{}, {"mode": "daily"}, {"mode": "interval", "every": 0, "unit": "hours"}, {"mode": "interval", "every": 2, "unit": "fortnights"}, {"mode": "interval", "every": 2, "unit": "minutes"},
           {"mode": "interval", "every": True, "unit": "days"}, {"mode": "occurrence", "type": "weekday", "days": [7]}, {"mode": "occurrence", "type": "weekday", "days": []},
           {"mode": "occurrence", "type": "monthday", "days": [32]}, {"mode": "occurrence", "type": "nth_weekday", "nth": 5, "weekday": 1},
           {"mode": "occurrence", "type": "first_workday", "at": "25:00"}, {"mode": "occurrence", "type": "elsewhen"}, "weekly"]
    for b in bad:
        with pytest.raises(tree.TreeError):
            tree.clean_freq(b)


def test_an_interval_is_due_when_its_time_has_passed_since_the_last_check_or_since_it_was_set_and_months_follow_the_calendar():
    six_h = {"mode": "interval", "every": 6, "unit": "hours"}
    assert not tree.is_due(six_h, ts(2026, 11, 17, 6), 0, ts(2026, 11, 17, 11, 59)) and tree.is_due(six_h, ts(2026, 11, 17, 6), 0, ts(2026, 11, 17, 12))
    assert not tree.is_due(six_h, 0, ts(2026, 11, 17, 6), ts(2026, 11, 17, 11)) and tree.is_due(six_h, 0, ts(2026, 11, 17, 6), ts(2026, 11, 17, 13))          # never run: counted from when it was set
    month = {"mode": "interval", "every": 1, "unit": "months"}
    assert not tree.is_due(month, ts(2027, 1, 31, 3), 0, ts(2027, 2, 27, 23)) and tree.is_due(month, ts(2027, 1, 31, 3), 0, ts(2027, 2, 28, 3))              # 31 January + 1 month = 28 February
    assert tree.is_due({"mode": "interval", "every": 1, "unit": "years"}, ts(2026, 2, 28), 0, ts(2027, 2, 28))
    for none in (None, {"mode": "manual"}, {"mode": "parts"}):
        assert not tree.is_due(none, 0, 0, ts(2030, 1, 1))


def test_occurrences_fire_on_the_days_asked_once_and_never_for_a_time_before_the_schedule_was_set():
    mon_thu = tree.clean_freq({"mode": "occurrence", "type": "weekday", "days": [0, 3], "at": "03:00"})
    last_mon = ts(2026, 11, 16, 3, 1)                                                    # Monday 16 November 2026
    assert not tree.is_due(mon_thu, last_mon, 0, ts(2026, 11, 19, 2, 59)) and tree.is_due(mon_thu, last_mon, 0, ts(2026, 11, 19, 3, 0))         # Thursday at 03:00
    assert not tree.is_due(mon_thu, ts(2026, 11, 19, 3, 0), 0, ts(2026, 11, 19, 23))                                                          # already done today
    assert not tree.is_due(mon_thu, 0, ts(2026, 11, 19, 12), ts(2026, 11, 19, 13))                                                            # set after today's 03:00: waits for Monday
    assert tree.is_due(mon_thu, 0, ts(2026, 11, 19, 12), ts(2026, 11, 23, 3, 5))
    d = lambda y, m, day: dt.date(y, m, day)                                                                                                   # noqa: E731
    last_workday = tree.clean_freq({"mode": "occurrence", "type": "last_workday"})
    assert [tree._matches(d(2027, 1, x), last_workday) for x in (29, 30, 31)] == [True, False, False]                                           # 31 January 2027 is a Sunday
    first_workday = tree.clean_freq({"mode": "occurrence", "type": "first_workday"})
    assert tree._matches(d(2027, 5, 3), first_workday) and not tree._matches(d(2027, 5, 1), first_workday)                                      # 1 May 2027 is a Saturday
    second_tue = tree.clean_freq({"mode": "occurrence", "type": "nth_weekday", "nth": 2, "weekday": 1})
    last_fri = tree.clean_freq({"mode": "occurrence", "type": "nth_weekday", "nth": -1, "weekday": 4})
    assert tree._matches(d(2026, 11, 10), second_tue) and not tree._matches(d(2026, 11, 3), second_tue) and tree._matches(d(2026, 11, 27), last_fri) and not tree._matches(d(2026, 11, 20), last_fri)
    month_end = tree.clean_freq({"mode": "occurrence", "type": "monthday", "days": [1, "last"]})
    assert tree._matches(d(2026, 2, 28), month_end) and tree._matches(d(2028, 2, 29), month_end) and tree._matches(d(2026, 3, 1), month_end) and not tree._matches(d(2026, 2, 27), month_end)


# -- the tree -------------------------------------------------------------------------------------

DOCS = [{"id": f"d{i}", "name": n} for i, n in enumerate(("c.md", "d.md", "e.md", "Section page", "Other page", "by hand.pdf"))]
DATA = {"sources": [{"id": "s1", "kind": "folder", "label": "docs"}, {"id": "s2", "kind": "mediawiki", "label": "wiki.example"}],
        "links": {"d0": {"source": "s1", "key": "a/b/c.md"}, "d1": {"source": "s1", "key": "a/d.md"}, "d2": {"source": "s1", "key": "e.md"},
                  "d3": {"source": "s2", "key": "Portal/Section/Section page"}, "d4": {"source": "s2", "key": "Portal/Other page"}}, "nodes": {}}


def shape(roots):
    return [(n["id"], n["type"], n["state"]) for n, _ in tree.walk(roots)]


def test_the_tree_has_a_node_for_each_kind_each_source_each_directory_and_each_document_and_hand_added_ones_apart():
    roots = tree.build(DATA, DOCS)
    assert [r["id"] for r in roots] == ["k:folder", "k:mediawiki", "k:hand"]
    ids = [n["id"] for n, _ in tree.walk(roots)]
    assert ids == ["k:folder", "s:s1", "d:s1:a", "d:s1:a/b", "x:d0", "x:d1", "x:d2", "k:mediawiki", "s:s2", "d:s2:Portal", "x:d4", "d:s2:Portal/Section", "x:d3", "k:hand", "x:d5"]
    assert tree.docs_under(tree.find(roots, "d:s1:a")) == ["d0", "d1"] and tree.find(roots, "x:d5")["hand"] and all(s == "on" for _, _, s in shape(roots))
    assert tree.find(roots, "nothing") is None


def test_a_node_that_is_off_turns_off_everything_below_without_losing_their_own_settings_and_a_node_shows_mixed_between():
    data = {**DATA, "nodes": {"d:s1:a/b": {"follow": False}}}
    roots = tree.build(data, DOCS)
    assert {n["id"]: n["state"] for n, _ in tree.walk(roots)}["d:s1:a/b"] == "off" and tree.find(roots, "x:d0")["state"] == "off"
    assert tree.find(roots, "d:s1:a")["state"] == "mixed" and tree.find(roots, "s:s1")["state"] == "mixed" and tree.find(roots, "k:folder")["state"] == "mixed" and tree.find(roots, "k:mediawiki")["state"] == "on"
    data["nodes"] = {"d:s1:a": {"follow": False}, "x:d1": {"follow": False}}                      # a switched off, and a document below it with its own switch off too
    roots = tree.build(data, DOCS)
    assert [tree.find(roots, i)["state"] for i in ("d:s1:a", "d:s1:a/b", "x:d0", "x:d1", "x:d2")] == ["off", "off", "off", "off", "on"]
    data["nodes"] = {"x:d1": {"follow": False}}                                                    # a back on: the document keeps its own setting
    roots = tree.build(data, DOCS)
    assert [tree.find(roots, i)["state"] for i in ("d:s1:a", "x:d0", "x:d1")] == ["mixed", "on", "off"] and tree.find(roots, "x:d1")["own"] is False
    data["nodes"] = {"s:s1": {"follow": False}, "s:s2": {"follow": False}}
    assert tree.find(tree.build(data, DOCS), "k:folder")["state"] == "off"


def test_an_item_is_followed_when_neither_it_nor_anything_above_it_is_off_and_a_removed_key_is_never_read_again():
    assert tree.followed(DATA, "s1", "a/b/c.md") and tree.followed(DATA, "s1", "a/new.md") and tree.followed(DATA, "unknown", "x")
    data = {**DATA, "nodes": {"d:s1:a/b": {"follow": False}}}
    assert not tree.followed(data, "s1", "a/b/c.md") and not tree.followed(data, "s1", "a/b/new.md") and tree.followed(data, "s1", "a/d.md")
    data["nodes"] = {"s:s1": {"follow": False}}
    assert not tree.followed(data, "s1", "e.md") and tree.followed(data, "s2", "Portal/Other page")
    data["nodes"] = {"x:d2": {"follow": False}}
    assert not tree.followed(data, "s1", "e.md") and tree.followed(data, "s1", "a/d.md")
    gone = {**DATA, "excluded": {"s1": ["a/b", "e.md"]}}
    assert tree.excluded(gone, "s1", "a/b/c.md") and tree.excluded(gone, "s1", "a/b/deep/f.md") and tree.excluded(gone, "s1", "e.md") and not tree.excluded(gone, "s1", "a/d.md") and not tree.excluded(gone, "s2", "e.md")
    assert not tree.excluded(gone, "s1", "a/bc.md")                                                # a directory removed does not take a name that only starts alike


def test_what_is_due_is_found_by_the_schedule_of_each_node_skipping_nodes_that_are_off_and_those_a_due_node_above_already_covers():
    day = {"mode": "interval", "every": 1, "unit": "days"}
    now = ts(2026, 11, 20, 12)
    data = {**DATA, "nodes": {"s:s1": {"freq": day, "since": ts(2026, 11, 1), "last": ts(2026, 11, 18)}, "d:s1:a": {"freq": day, "since": ts(2026, 11, 1), "last": 0},
                              "s:s2": {"freq": day, "since": ts(2026, 11, 1), "last": ts(2026, 11, 20, 6), "follow": True},
                              "k:mediawiki": {"freq": {"mode": "parts"}}, "x:d4": {"freq": day, "since": ts(2026, 11, 1), "last": 0, "follow": False}}}
    due = tree.due_nodes(tree.build(data, DOCS), now)
    assert [n["id"] for n in due] == ["s:s1"]                                                      # a:s1 covers d:s1:a; s:s2 ran six hours ago; x:d4 is off; "parts" is never due by itself
    data["nodes"]["s:s1"]["last"] = ts(2026, 11, 20, 3)
    assert [n["id"] for n in tree.due_nodes(tree.build(data, DOCS), now)] == ["d:s1:a"]            # now the parent is not due, the directory below it is
    data["nodes"]["s:s1"] = {"freq": day, "since": ts(2026, 11, 1), "last": 0, "follow": False}
    assert tree.due_nodes(tree.build(data, DOCS), now) == []                                      # a source that is off takes its directories with it
