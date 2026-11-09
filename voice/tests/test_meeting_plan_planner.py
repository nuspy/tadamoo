"""The planner and its loop with a fake clock, a fake calendar and a fake join: the list (order, merge with the calendar, the calendar's own fields), and
what the loop does and does not do (joins once at lead_s, only a ticked item, nothing while the master switch is off, retries a refusal, shows missed and
done). The join callback stands for the service's own path; the consent rule is tested with that path in test_meeting_plan_service.py."""
import time

import pytest

from callbridge.meetbot.calendar_watch import CalendarWatcher
from callbridge.meetbot.errors import Refused
from callbridge.meetbot.plan_store import PlanStore, calendar_id, parse_start
from callbridge.meetbot.planner import JOIN_EARLY_S, JOIN_LATE_TOLERANCE_S, RETRY_S, Planner

MEET = "https://meet.google.com/abc-defg-hij"
NOW = parse_start("2027-01-15T12:00:00Z")[1]
HOUR = 3600


def iso(offset_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + offset_s))


class Rig:
    def __init__(self, tmp_path):
        self.now = NOW
        self.events: list[dict] = []
        self.enabled = True
        self.active = ""
        self.joined: list[dict] = []
        self.answer = None                                   # an exception to raise, or None
        self.store = PlanStore(tmp_path / "meetings_plan.json", clock=lambda: self.now)
        self.planner = Planner(self.store, lambda: self.events, self._join, lambda: self.enabled, lambda: self.active, clock=lambda: self.now)

    def _join(self, item):
        self.joined.append(item)
        if self.answer is not None:
            raise self.answer
        return {"session_id": f"s{len(self.joined)}"}

    def add(self, offset_s, **kw):
        return self.planner.add({"url": MEET, "start": iso(offset_s), **kw})

    def status(self, item_id):
        return self.planner.get(item_id)["status"]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def event(n, offset_s, url=MEET, provider="google", **kw):
    return {"id": f"g:{n}", "provider": provider, "title": f"Event {n}", "start": iso(offset_s), "end": iso(offset_s + 1800), "organizer": "boss@x.org", "meeting_url": url, **kw}


# ---- the list
def test_the_list_is_coming_meetings_by_time_then_recent_ones_latest_first(rig):
    late = rig.add(2 * HOUR, title="late")
    soon = rig.add(HOUR, title="soon")
    old_done = rig.add(-3 * HOUR, title="done", participate=True)
    rig.store.mark(old_done["id"], session_id="x")
    missed = rig.add(-HOUR, title="missed", participate=True)
    past = rig.add(-2 * HOUR, title="past")
    rig.add(-3 * 86400, title="too old to list")
    assert [i["title"] for i in rig.planner.view()] == ["soon", "late", "missed", "past", "done"]
    assert [i["status"] for i in rig.planner.view()] == ["upcoming", "upcoming", "missed", "past", "done"]
    assert soon["id"] != late["id"] and missed["id"] and past["id"]


def test_a_calendar_event_with_a_link_an_adapter_takes_is_listed_as_not_participating(rig):
    rig.events = [event(1, HOUR), event(2, 2 * HOUR, url=None), event(3, 3 * HOUR, url="https://example.com/room"),
                  event(4, 4 * HOUR, url="https://us02web.zoom.us/j/123456789", provider="microsoft", start=iso(4 * HOUR).replace("Z", ""))]
    items = rig.planner.view()
    assert [(i["title"], i["platform"], i["source"]) for i in items] == [("Event 1", "meet", "google"), ("Event 4", "zoom", "microsoft")]
    one = items[0]
    assert one["id"] == calendar_id("g:1") and one["participate"] is False and one["role"] == "minutes" and one["duration_min"] == 30 and one["organizer"] == "boss@x.org"
    assert items[1]["start"] == iso(4 * HOUR)                                   # a time without a zone is UTC
    assert rig.store.all() == []                                                 # nothing is stored until the person chooses something


def test_a_choice_on_a_calendar_event_stores_it_and_the_calendar_still_owns_the_rest(rig):
    rig.events = [event(1, HOUR)]
    cid = calendar_id("g:1")
    item = rig.planner.update(cid, {"participate": True, "persona": "ada", "role": "speak", "camera": True})
    assert item["participate"] and item["persona"] == "ada" and item["event_id"] == "g:1" and item["source"] == "google"
    assert rig.planner.participating("g:1") is True and rig.planner.participating("g:9") is False
    rig.events = [event(1, HOUR, title="Renamed", start=iso(HOUR + 900))]
    view = rig.planner.view()
    assert len(view) == 1 and view[0]["title"] == "Renamed" and view[0]["start"] == iso(HOUR + 900) and view[0]["persona"] == "ada"
    assert rig.store.get(cid)["title"] == "Renamed"                              # the stored snapshot follows the calendar
    with pytest.raises(Refused) as e:
        rig.planner.update(cid, {"title": "Mine"})
    assert e.value.code == "calendar_owned"
    with pytest.raises(Refused) as e:
        rig.planner.delete(cid)
    assert e.value.code == "calendar_owned"
    assert rig.planner.set_participate("g:1", False)["participate"] is False
    with pytest.raises(Refused) as e:
        rig.planner.update(calendar_id("g:404"), {"participate": True})
    assert e.value.status == 404


def test_a_refused_change_to_a_calendar_event_stores_nothing(rig):
    rig.events = [event(1, HOUR)]
    with pytest.raises(Refused):
        rig.planner.update(calendar_id("g:1"), {"role": "boss"})
    assert rig.store.all() == []


def test_a_manual_item_is_added_edited_and_deleted(rig):
    item = rig.add(HOUR, title="Mine")
    assert rig.planner.update(item["id"], {"title": "Renamed", "camera": True})["title"] == "Renamed"
    rig.planner.delete(item["id"])
    assert rig.planner.view() == [] and rig.planner.get(item["id"]) is None


# ---- the loop
def test_a_ticked_meeting_is_joined_once_at_lead_s_before_the_start(rig):
    item = rig.add(300, participate=True, persona="ada", camera=True, role="speak")
    rig.now = NOW + 300 - JOIN_EARLY_S - 1
    assert rig.planner.tick() == [] and rig.joined == []
    rig.now += 1
    assert rig.planner.tick() == [item["id"]] and len(rig.joined) == 1
    assert (rig.joined[0]["persona"], rig.joined[0]["camera"], rig.joined[0]["role"], rig.joined[0]["url"]) == ("ada", True, "speak", MEET)
    for step in (0, 30, 400, 2000):
        rig.now += step
        assert rig.planner.tick() == []
    assert len(rig.joined) == 1                                                  # only once
    done = rig.planner.get(item["id"])
    assert done["status"] == "done" and done["session_id"] == "s1" and done["joined_at"] and "last_error" not in done


def test_a_meeting_that_is_not_ticked_is_never_joined(rig):
    rig.add(0)
    rig.add(0, participate=False)
    rig.events = [event(1, 0)]
    for offset in (-100, 0, 400):
        rig.now = NOW + offset
        assert rig.planner.tick() == []
    assert rig.joined == []


def test_nothing_joins_while_the_master_switch_is_off(rig):
    item = rig.add(30, participate=True)
    rig.enabled = False
    rig.now = NOW + 30
    assert rig.planner.tick() == [] and rig.joined == []
    rig.enabled = True                                                           # turned on while the meeting is still inside its window
    assert rig.planner.tick() == [item["id"]]


def test_a_ticked_calendar_event_is_joined_and_only_while_the_calendar_lists_it(rig):
    rig.events = [event(1, 30)]
    rig.planner.set_participate("g:1", True)
    rig.now = NOW + 30
    rig.events = []                                                              # cancelled in the calendar, or the calendar did not answer
    assert rig.planner.tick() == [] and rig.joined == []
    rig.events = [event(1, 30)]
    assert rig.planner.tick() == [calendar_id("g:1")] and rig.joined[0]["event_id"] == "g:1" and rig.joined[0]["organizer"] == "boss@x.org"
    assert rig.planner.get(calendar_id("g:1"))["status"] == "done"


def test_a_refused_join_is_tried_again_after_a_while_and_says_why(rig):
    item = rig.add(0, participate=True)
    rig.answer = Refused(409, "busy", "A meeting is in progress: one at a time.")
    assert rig.planner.tick() == [] and len(rig.joined) == 1
    assert rig.planner.get(item["id"])["last_error"] == "A meeting is in progress: one at a time."
    rig.now += RETRY_S - 1
    rig.planner.tick()
    assert len(rig.joined) == 1                                                  # not on every turn
    rig.now += 1
    rig.answer = None
    assert rig.planner.tick() == [item["id"]] and len(rig.joined) == 2
    assert "last_error" not in rig.planner.get(item["id"])


def test_a_join_that_blows_up_does_not_stop_the_other_meetings(rig):
    first, second = rig.add(0, participate=True, title="first"), rig.add(10, participate=True, title="second")
    calls = []

    def join(item):
        calls.append(item["title"])
        if item["title"] == "first":
            raise RuntimeError("browser exploded")
        return {"session_id": "s"}

    rig.planner.join_cb = join
    rig.now = NOW + 10
    assert rig.planner.tick() == [second["id"]] and calls == ["first", "second"]
    assert "browser exploded" in rig.planner.get(first["id"])["last_error"]


def test_the_window_closes_and_the_meeting_is_missed(rig):
    ticked, quiet = rig.add(0, participate=True), rig.add(0)
    rig.enabled = False
    rig.now = NOW + JOIN_LATE_TOLERANCE_S
    assert rig.status(ticked["id"]) == "upcoming"
    rig.now += 1
    assert rig.status(ticked["id"]) == "missed" and rig.status(quiet["id"]) == "past"
    rig.enabled = True
    assert rig.planner.tick() == [] and rig.joined == []                         # too late: never joined
    assert {i["status"] for i in rig.planner.view()} == {"missed", "past"}


def test_a_running_meeting_is_running_then_done_with_its_record_id(rig):
    item = rig.add(0, participate=True)
    rig.planner.tick()
    rig.active = "s1"
    assert rig.status(item["id"]) == "running"
    rig.active = ""
    assert rig.status(item["id"]) == "done" and rig.planner.get(item["id"])["session_id"] == "s1"


def test_a_calendar_event_joined_by_hand_becomes_an_item(rig):
    rig.events = [event(1, HOUR)]
    rig.planner.mark_joined(calendar_id("g:1"), "s7", "no_avatar")
    item = rig.store.get(calendar_id("g:1"))
    assert item["session_id"] == "s7" and item["last_note"] == "no_avatar" and item["source"] == "google"


def test_old_items_are_pruned_by_the_loop(rig):
    rig.add(-20 * 86400)
    rig.planner.tick()
    assert rig.store.all() == []


# ---- the loop thread's turn
class Provider:
    def __init__(self, configured=True, events=()):
        self._c, self._e, self.calls = configured, list(events), 0

    def configured(self):
        return self._c

    def events(self, days=7):
        self.calls += 1
        return [dict(e) for e in self._e]


def test_the_watcher_reads_the_calendar_at_most_every_poll_and_not_while_off():
    now = [NOW]
    flag, ticks = [False], []
    live, dead = Provider(True, [event(1, 60)]), Provider(False, [event(2, 60)])
    w = CalendarWatcher([live, dead], lambda t: ticks.append(t) or ["x"], lambda: flag[0], poll_s=300, clock=lambda: now[0])
    assert w.tick() == [] and live.calls == 0 and ticks == []                    # the master switch is off: nothing is read, nothing is joined
    flag[0] = True
    assert w.tick() == ["x"] and live.calls == 1 and dead.calls == 0 and len(ticks) == 1
    now[0] += 15
    w.tick()
    assert live.calls == 1 and len(ticks) == 2                                   # the planner turns every tick, the calendar every poll
    now[0] += 300
    w.tick()
    assert live.calls == 2 and [e["id"] for e in w.last_events] == ["g:1"]


def test_the_loop_runs_without_any_calendar(tmp_path):
    seen = []
    w = CalendarWatcher([], lambda t: seen.append(t) or [], lambda: True, poll_s=300, tick_s=0.01)
    w.start()
    deadline = time.monotonic() + 3
    while len(seen) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    w.stop()
    assert len(seen) >= 3
