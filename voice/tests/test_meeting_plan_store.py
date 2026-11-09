"""The stored plan of meetings (callbridge/meetbot/plan_store.py) and the adapter registry the plan uses: what an item may hold, what is refused, that
a write is atomic and survives a restart, and that an address is taken by the registered adapters only. No network, no browser, no clock but a fake."""
import json
import os

import pytest

from callbridge.meetbot import adapters
from callbridge.meetbot.adapters.base import MeetingAdapter
from callbridge.meetbot.errors import Refused
from callbridge.meetbot.plan_store import KEEP_S, MAX_ITEMS, PlanStore, calendar_id, clean, parse_start

MEET = "https://meet.google.com/abc-defg-hij?pwd=secret"
NOW = 1_800_000_000.0
START = "2027-01-15T15:00:00+01:00"


@pytest.fixture
def store(tmp_path):
    return PlanStore(tmp_path / "meetings_plan.json", clock=lambda: NOW)


def refused(fn, *a, **k):
    with pytest.raises(Refused) as e:
        fn(*a, **k)
    return e.value


def test_an_item_gets_its_defaults_and_the_platform_from_the_address(store):
    item = store.add({"url": MEET, "start": START})
    assert item["platform"] == "meet" and item["title"] == "meet" and item["start"] == "2027-01-15T14:00:00Z"     # stored in UTC
    assert (item["participate"], item["role"], item["speak"], item["camera"], item["persona"], item["consent_mode"], item["source"]) == (
        False, "minutes", False, False, "", "", "manual")
    assert item["id"].startswith("p") and item["created"].endswith("Z")
    assert store.get(item["id"]) == item and store.all() == [item]


def test_every_field_is_checked(store):
    ok = {"url": MEET, "start": START}
    bad = [({"start": START}, "bad_url"), ({**ok, "url": "ftp://x"}, "bad_url"), ({**ok, "url": "https://example.com/room"}, "unsupported_platform"),
           ({**ok, "url": MEET + " x"}, "bad_url"), ({**ok, "url": "https://meet.google.com/" + "a" * 2100}, "bad_url"),
           ({"url": MEET}, "bad_start"), ({**ok, "start": "2027-01-15T15:00:00"}, "bad_start"), ({**ok, "start": "soon"}, "bad_start"), ({**ok, "start": 5}, "bad_start"),
           ({**ok, "platform": "webex"}, "unsupported_platform"), ({**ok, "platform": "zoom"}, "platform_mismatch"),
           ({**ok, "title": "x" * 121}, "bad_request"), ({**ok, "title": "a\nb"}, "bad_request"), ({**ok, "title": 5}, "bad_request"),
           ({**ok, "duration_min": 0}, "bad_request"), ({**ok, "duration_min": 1441}, "bad_request"), ({**ok, "duration_min": True}, "bad_request"),
           ({**ok, "duration_min": "30"}, "bad_request"), ({**ok, "participate": "yes"}, "bad_request"), ({**ok, "camera": 1}, "bad_request"),
           ({**ok, "role": "boss"}, "bad_request"), ({**ok, "speak": True}, "bad_request"), ({**ok, "role": "speak", "speak": False}, "bad_request"),
           ({**ok, "persona": "Ada Lovelace"}, "bad_request"), ({**ok, "persona": "../x"}, "bad_request"), ({**ok, "persona": 3}, "bad_request"),
           ({**ok, "consent_mode": "silent"}, "bad_request"), ({**ok, "notes": "x" * 501}, "bad_request"), ({**ok, "notes": "a\x00b"}, "bad_request"),
           ({**ok, "id": "x"}, "bad_request"), ({**ok, "session_id": "x"}, "bad_request"), ({**ok, "source": "google"}, "bad_request"), ([], "bad_request"), (None, "bad_request")]
    for data, code in bad:
        assert refused(store.add, data).code == code, data
    assert store.all() == [] and not store.path.exists()                       # nothing refused was written


def test_good_values_are_kept(store):
    item = store.add({"url": "https://teams.microsoft.com/l/meetup-join/19:abc/0", "start": "2027-01-15T14:00:00Z", "title": " Weekly ", "duration_min": 45, "participate": True,
                      "role": "speak", "persona": "ada-2", "camera": True, "consent_mode": "explicit", "notes": "agenda\nline two"})
    assert (item["platform"], item["title"], item["duration_min"], item["participate"], item["role"], item["speak"], item["persona"], item["camera"],
            item["consent_mode"], item["notes"]) == ("teams", "Weekly", 45, True, "speak", True, "ada-2", True, "explicit", "agenda\nline two")


def test_an_edit_changes_only_the_named_fields_and_is_checked(store):
    item = store.add({"url": MEET, "start": START, "title": "A", "persona": "ada", "participate": True})
    new = store.update(item["id"], {"role": "listen", "camera": True})
    assert (new["role"], new["camera"], new["persona"], new["participate"], new["title"], new["start"]) == ("listen", True, "ada", True, "A", item["start"])
    assert store.update(item["id"], {"role": "speak"})["speak"] is True and store.update(item["id"], {"role": "minutes"})["speak"] is False
    assert store.update(item["id"], {"url": "https://zoom.us/j/123456789"})["platform"] == "zoom"             # the address picks the platform again
    assert refused(store.update, item["id"], {"url": "https://example.com/x"}).code == "unsupported_platform"
    assert refused(store.update, item["id"], {"role": "x"}).code == "bad_request" and store.get(item["id"])["platform"] == "zoom"
    assert refused(store.update, "nope", {"role": "listen"}).status == 404


def test_the_calendar_owns_the_title_the_address_and_the_time(store):
    item = store.add({"url": MEET, "start": START, "title": "Standup"}, source="google", item_id=calendar_id("g:1"), extra={"event_id": "g:1"})
    assert item["id"] == calendar_id("g:1") and item["event_id"] == "g:1"
    for change in ({"title": "Mine"}, {"url": "https://zoom.us/j/1"}, {"start": "2027-02-01T10:00:00Z"}, {"duration_min": 5}):
        assert refused(store.update, item["id"], change).code == "calendar_owned", change
    assert store.update(item["id"], {"title": "Standup", "participate": True, "persona": "ada"})["participate"] is True       # the same value is no change


def test_delete_and_the_limit(store):
    a = store.add({"url": MEET, "start": START})
    store.delete(a["id"])
    assert store.all() == [] and refused(store.delete, a["id"]).status == 404
    for _ in range(MAX_ITEMS):
        store.add({"url": MEET, "start": START})
    assert refused(store.add, {"url": MEET, "start": START}).code == "plan_full"


def test_the_file_is_replaced_whole_and_read_back_after_a_restart(store, tmp_path, monkeypatch):
    item = store.add({"url": MEET, "start": START, "title": "Keep me", "participate": True})
    store.mark(item["id"], session_id="20270115-150000-meet-ab12", joined_at="2027-01-15T14:00:00Z", last_error="")
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["items"][0]["session_id"] == "20270115-150000-meet-ab12" and not list(tmp_path.glob("*.tmp"))
    again = PlanStore(store.path, clock=lambda: NOW)
    assert again.get(item["id"]) == store.get(item["id"])
    # a crash while writing leaves the old file: the write goes to a temporary file first
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.update(item["id"], {"title": "Lost"})
    monkeypatch.setattr(os, "replace", real)
    assert json.loads(store.path.read_text(encoding="utf-8"))["items"][0]["title"] == "Keep me"


def test_a_damaged_file_or_row_reads_as_less_not_as_a_crash(tmp_path):
    path = tmp_path / "meetings_plan.json"
    path.write_text("{not json", encoding="utf-8")
    assert PlanStore(path).all() == []
    good = {"id": "p1", "url": MEET, "start": "2027-01-15T14:00:00Z", "platform": "meet"}
    path.write_text(json.dumps({"version": 1, "items": [good, {"id": "../x", "start": "2027-01-15T14:00:00Z"}, {"id": "p2", "start": "no"}, 5, {"url": "x"}]}), encoding="utf-8")
    assert [i["id"] for i in PlanStore(path).all()] == ["p1"]
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert PlanStore(path).all() == []


def test_what_started_long_ago_is_forgotten(store):
    old = store.add({"url": MEET, "start": "2026-12-01T14:00:00Z"})
    recent = store.add({"url": MEET, "start": "2027-01-02T14:00:00Z"})
    store._clock = lambda: parse_start("2027-01-01T00:00:00Z")[1] + KEEP_S - 3600          # 14 days less an hour after the 1st of January
    assert store.prune() == 1 and [i["id"] for i in store.all()] == [recent["id"]] and old["id"] not in [i["id"] for i in store.all()]


def test_the_recorded_fields_are_only_the_planners(store):
    item = store.add({"url": MEET, "start": START})
    store.mark(item["id"], session_id="s1", last_error="boom")
    assert store.get(item["id"])["last_error"] == "boom"
    store.mark(item["id"], last_error="")
    assert "last_error" not in store.get(item["id"])
    with pytest.raises(KeyError):
        store.mark(item["id"], title="x")
    store.mark("missing", session_id="x")                                   # a deleted item is not an error


def test_parse_start_wants_a_zone_unless_it_is_told_to_assume_utc():
    assert parse_start("2027-01-15T15:00:00+01:00") == ("2027-01-15T14:00:00Z", parse_start("2027-01-15T14:00:00Z")[1])
    assert parse_start("2027-01-15T15:00:00") is None and parse_start("2027-01-15T15:00:00", assume_utc=True)[0] == "2027-01-15T15:00:00Z"
    assert parse_start("2027-01-15", assume_utc=True)[0] == "2027-01-15T00:00:00Z" and parse_start(None) is None and parse_start("x" * 50) is None


# ---- the adapter registry
def test_the_registered_platforms_and_the_detection_of_an_address():
    assert adapters.platform_names() == ["meet", "teams", "zoom"]
    assert adapters.detect(MEET) == "meet" and adapters.detect("https://us02web.zoom.us/j/123456789?pwd=x") == "zoom"
    assert adapters.detect("https://teams.microsoft.com/l/meetup-join/19:abc/0") == "teams"
    assert adapters.detect("https://example.com/room") is None and adapters.detect("not a url") is None and adapters.adapter_for("https://example.com/room") is None
    assert clean({"url": MEET, "start": START})["platform"] == "meet"
    err = refused(clean, {"url": "https://webex.com/meet/x", "start": START})
    assert err.code == "unsupported_platform" and "meet, teams, zoom" in err.message


def test_a_new_platform_is_one_registered_class(monkeypatch):
    monkeypatch.setattr(adapters, "_ADAPTERS", list(adapters._ADAPTERS))

    class Jitsi(MeetingAdapter):
        platform = "jitsi"

        @staticmethod
        def matches(url):
            return url.startswith("https://meet.jit.si/")

    adapters.register(Jitsi)
    assert adapters.platform_names() == ["meet", "teams", "zoom", "jitsi"] and adapters.detect("https://meet.jit.si/room") == "jitsi"
    assert clean({"url": "https://meet.jit.si/room", "start": START})["platform"] == "jitsi"
    assert isinstance(adapters.adapter_for("https://meet.jit.si/room"), Jitsi)


def test_an_adapter_without_a_name_a_clash_or_a_matcher_is_refused(monkeypatch):
    monkeypatch.setattr(adapters, "_ADAPTERS", list(adapters._ADAPTERS))

    class NoName(MeetingAdapter):
        @staticmethod
        def matches(url):
            return True

    class Clash(MeetingAdapter):
        platform = "zoom"

        @staticmethod
        def matches(url):
            return True

    class NoMatcher(MeetingAdapter):
        platform = "other"

    for cls, kind in ((NoName, ValueError), (Clash, ValueError), (NoMatcher, TypeError), (int, TypeError)):
        with pytest.raises(kind):
            adapters.register(cls)
    assert adapters.platform_names() == ["meet", "teams", "zoom"]
