"""Routines: schedule text, parallel runs that never overlap with themselves, optional limits, parked requests, persistence, and a run through the conversation manager."""
import datetime as dt
import sys
import threading
import time
from pathlib import Path

import pytest

from tadamoo.scheduler import RoutineError, Schedule, Scheduler, SessionRoutineRunner, fire_id
from serve.agent_sessions import SessionManager

STUB = str(Path(__file__).resolve().parent.parent / "serve" / "tests" / "session_stub.py")


def until(predicate, seconds=15, step=0.02):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


def ts(*args):
    return dt.datetime(*args).timestamp()


# -- schedule text -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,seconds", [("every 30m", 1800), ("Every 2h", 7200), ("every  1d", 86400), ("every 1m", 60), ("every 30s", 30), ("every 1s", 1), ("every 20000h", 72000000)])
def test_intervals(text, seconds):
    assert Schedule.parse(text).interval_s == seconds


@pytest.mark.parametrize("text", ["", "every 0s", "every 0m", "every 5x", "daily 25:00", "daily 08:61", "someday 08:00", "mon,xyz 08:00",
                                  "08:00", None, 5])
def test_bad_schedules_say_how_to_write_them(text):
    with pytest.raises(RoutineError) as e:
        Schedule.parse(text)
    assert e.value.code == "bad_schedule"


def test_fixed_times_find_the_next_matching_day():
    friday_noon = ts(2026, 10, 23, 12, 0)                        # a Friday
    assert Schedule.parse("daily 08:30").next_after(friday_noon) == ts(2026, 10, 24, 8, 30)
    assert Schedule.parse("daily 13:00").next_after(friday_noon) == ts(2026, 10, 23, 13, 0)
    assert Schedule.parse("weekdays 08:30").next_after(friday_noon) == ts(2026, 10, 26, 8, 30)       # over the weekend
    assert Schedule.parse("weekends 10:00").next_after(friday_noon) == ts(2026, 10, 24, 10, 0)
    assert Schedule.parse("mon, wed 09:00").next_after(friday_noon) == ts(2026, 10, 26, 9, 0)
    assert Schedule.parse("every 2h").next_after(friday_noon) == friday_noon + 7200


def test_the_latest_fixed_time_at_or_before_a_moment():
    saturday_noon = ts(2026, 10, 24, 12, 0)
    assert Schedule.parse("daily 08:30").previous(saturday_noon) == ts(2026, 10, 24, 8, 30)
    assert Schedule.parse("daily 13:00").previous(saturday_noon) == ts(2026, 10, 23, 13, 0)
    assert Schedule.parse("weekdays 08:30").previous(saturday_noon) == ts(2026, 10, 23, 8, 30)
    assert Schedule.parse("mon 09:00").previous(ts(2026, 10, 26, 9, 0)) == ts(2026, 10, 26, 9, 0)
    assert Schedule.parse("every 2h").previous(saturday_noon) == 0


# -- cron expressions and single moments -------------------------------------------------------------------

def test_a_cron_expression_finds_the_next_and_the_previous_time():
    friday_noon = ts(2026, 10, 23, 12, 0)
    weekdays = Schedule.parse("Cron  50 14 * * 1-5")
    assert weekdays.next_after(friday_noon) == ts(2026, 10, 23, 14, 50)
    assert weekdays.next_after(ts(2026, 10, 23, 14, 50)) == ts(2026, 10, 26, 14, 50)             # over the weekend
    assert weekdays.previous(friday_noon) == ts(2026, 10, 22, 14, 50)
    assert weekdays.previous(ts(2026, 10, 25, 9, 0)) == ts(2026, 10, 23, 14, 50)
    quarters = Schedule.parse("cron */15 9-10 * * *")
    assert quarters.next_after(ts(2026, 10, 23, 9, 50)) == ts(2026, 10, 23, 10, 0)
    assert quarters.next_after(ts(2026, 10, 23, 10, 45)) == ts(2026, 10, 24, 9, 0)
    assert Schedule.parse("cron 0 8 * * 7").next_after(friday_noon) == ts(2026, 10, 25, 8, 0)      # 7 is Sunday, as 0 is


def test_a_restricted_day_of_month_and_day_of_week_are_either_one():
    both = Schedule.parse("cron 0 9 13 * 5")                      # the 13th, or a Friday
    assert both.next_after(ts(2026, 10, 23, 12, 0)) == ts(2026, 10, 30, 9, 0)
    assert both.next_after(ts(2026, 11, 1, 0, 0)) == ts(2026, 11, 6, 9, 0)
    assert Schedule.parse("cron 0 9 13 * *").next_after(ts(2026, 10, 23, 12, 0)) == ts(2026, 11, 13, 9, 0)
    assert Schedule.parse("cron 0 9 * * 5").next_after(ts(2026, 10, 23, 12, 0)) == ts(2026, 10, 30, 9, 0)


@pytest.mark.parametrize("text", ["cron * * * *", "cron * * * * * *", "cron 60 * * * *", "cron * 24 * * *", "cron * * 0 * *", "cron * * * 13 *",
                                  "cron */0 * * * *", "cron a * * * *", "cron 5-1 * * * *", "cron * * * * 8"])
def test_a_bad_cron_expression_says_how_to_write_one(text):
    with pytest.raises(RoutineError) as e:
        Schedule.parse(text)
    assert e.value.code == "bad_schedule" and "minute hour" in str(e.value)


def test_a_cron_expression_that_never_comes_is_refused():
    with pytest.raises(RoutineError) as e:
        Schedule.parse("cron 0 0 31 2 *").next_after(ts(2026, 1, 1, 0, 0))
    assert e.value.code == "bad_schedule"


def test_a_single_moment_comes_once():
    once = Schedule.parse("at 2026-10-24 18:00")
    assert once.next_after(ts(2026, 10, 23, 12, 0)) == ts(2026, 10, 24, 18, 0)
    assert once.next_after(ts(2026, 10, 24, 18, 0)) == float("inf") and once.previous(ts(2026, 10, 24, 17, 59)) == 0.0
    assert once.previous(ts(2026, 10, 25, 0, 0)) == ts(2026, 10, 24, 18, 0)
    for text in ("at 2026-13-01 10:00", "at 2026-02-30 10:00", "at tomorrow"):
        with pytest.raises(RoutineError):
            Schedule.parse(text)


def test_occurrences_count_what_came_between_two_moments():
    daily = Schedule.parse("daily 08:30")
    assert daily.occurrences(ts(2026, 10, 20, 9, 0), ts(2026, 10, 23, 9, 0)) == 3
    assert Schedule.parse("every 1h").occurrences(ts(2026, 10, 23, 8, 0), ts(2026, 10, 23, 11, 30)) == 3
    assert daily.occurrences(ts(2026, 10, 23, 9, 0), ts(2026, 10, 23, 10, 0)) == 0


# -- fires: run ids, time to live, dedup, restarts ---------------------------------------------------------

def test_a_fire_has_one_id_for_its_planned_local_time():
    assert fire_id("abc", ts(2026, 10, 23, 8, 30)) == fire_id("abc", ts(2026, 10, 23, 8, 30) + 0.4)
    assert fire_id("abc", ts(2026, 10, 23, 8, 30)) != fire_id("abd", ts(2026, 10, 23, 8, 30))
    assert fire_id("abc", ts(2026, 10, 23, 8, 30)) != fire_id("abc", ts(2026, 10, 24, 8, 30))


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="the clock can only be moved to a zone with daylight saving where tzset exists")
def test_the_hour_that_repeats_when_the_clocks_go_back_is_one_fire(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Rome")
    time.tzset()
    try:
        first = dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.timezone.utc).timestamp()       # 02:30 summer time
        second = first + 3600                                                              # 02:30 winter time
        assert fire_id("abc", first) == fire_id("abc", second)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_scheduled_run_carries_the_id_of_its_fire_and_the_log_says_how_it_ended(tmp_path, folder):
    runner = FakeRunner()
    sched, clock = make(tmp_path, runner)
    rid = add(sched, folder, schedule="every 1h")["id"]
    due = sched._due[rid]
    sched.start()
    clock.now = due + 5
    sched.check()
    until(lambda: runner.ran == ["tests"])
    fire = until(lambda: next((f for f in sched.fires_log(rid) if f["state"] == "done"), None))
    assert fire["id"] == fire_id(rid, due) and fire["planned"] == due and fire["status"] == "done"
    assert sched.runs(rid)[0]["id"] == fire["id"]
    sched.shutdown()


def test_a_fire_later_than_the_time_to_live_is_skipped_and_written_down(tmp_path, folder):
    sched, _ = make(tmp_path, now=ts(2026, 10, 23, 12, 0))
    rid = add(sched, folder, schedule="daily 08:30", ttl_min=60)["id"]
    late, _ = make(tmp_path, now=ts(2026, 10, 24, 10, 0))                  # 90 minutes after 08:30
    late.check()
    assert late.view()["queued"] == []
    fire = late.fires_log(rid)[0]
    assert fire["state"] == "skipped_late" and fire["planned"] == ts(2026, 10, 24, 8, 30) and fire["missed"] == 1
    assert late._due[rid] == ts(2026, 10, 25, 8, 30)
    again, _ = make(tmp_path, now=ts(2026, 10, 24, 10, 30))               # and it is not looked at again after another restart
    again.check()
    assert again.view()["queued"] == [] and len(again.fires_log(rid)) == 1


def test_a_fire_inside_the_time_to_live_still_runs_late(tmp_path, folder):
    sched, _ = make(tmp_path, now=ts(2026, 10, 23, 12, 0))
    rid = add(sched, folder, schedule="daily 08:30", ttl_min=60)["id"]
    late, _ = make(tmp_path, now=ts(2026, 10, 24, 9, 0))                   # 30 minutes after
    late.check()
    assert late.view()["queued"] == [rid] and late.fires_log(rid)[0]["state"] == "claimed"


def test_a_routine_without_a_time_to_live_runs_however_late(tmp_path, folder):
    sched, _ = make(tmp_path, now=ts(2026, 10, 23, 12, 0))
    rid = add(sched, folder, schedule="daily 08:30")["id"]
    late, _ = make(tmp_path, now=ts(2026, 10, 24, 23, 0))
    late.check()
    assert late.view()["queued"] == [rid]


def test_missed_fires_are_counted_when_one_is_skipped(tmp_path, folder):
    sched, _ = make(tmp_path, now=ts(2026, 10, 20, 12, 0))
    rid = add(sched, folder, schedule="daily 08:30", ttl_min=60)["id"]
    late, _ = make(tmp_path, now=ts(2026, 10, 23, 12, 0))                  # the PC was off for three mornings
    late.check()
    assert late.fires_log(rid)[0]["missed"] == 3 and late.view()["queued"] == []


def test_a_restart_after_a_fire_was_claimed_does_not_run_it_again(tmp_path, folder):
    sched, clock = make(tmp_path)
    rid = add(sched, folder, schedule="every 1h")["id"]
    clock.now += 3700
    sched.check()
    assert sched.view()["queued"] == [rid]                                # claimed, then the program ends before it finishes
    again, _ = make(tmp_path, now=clock.now + 600)
    again.check()
    assert again.view()["queued"] == []                                   # the claim counts: no second run at start
    assert [f["state"] for f in again.fires_log(rid)] == ["interrupted"]


def test_routines_with_one_key_never_have_two_fires_in_flight(tmp_path, folder):
    sched, clock = make(tmp_path)
    a = add(sched, folder, name="a", schedule="every 1h", dedup_key="morning")["id"]
    b = add(sched, folder, name="b", schedule="every 1h", dedup_key="morning")["id"]
    c = add(sched, folder, name="c", schedule="every 1h")["id"]
    clock.now += 3700
    sched.check()
    assert sched.view()["queued"] == [a, c]
    assert [f["state"] for f in sched.fires_log(b)] == ["skipped_dup"]


def test_a_fire_waiting_in_the_queue_is_cancelled_when_its_routine_is_turned_off_or_removed(tmp_path, folder):
    sched, clock = make(tmp_path)
    a = add(sched, folder, name="a", schedule="every 1h")["id"]
    b = add(sched, folder, name="b", schedule="every 1h")["id"]
    clock.now += 3700
    sched.check()
    sched.set_enabled(a, False)
    sched.remove(b)
    assert sched.view()["queued"] == []
    assert [f["state"] for f in sched.fires_log(a)] == ["cancelled"] and [f["state"] for f in sched.fires_log(b)] == ["cancelled"]


def test_a_single_moment_runs_once_and_then_the_routine_is_off(tmp_path, folder):
    runner = FakeRunner()
    sched, clock = make(tmp_path, runner)
    rid = add(sched, folder, schedule="at 2026-10-23 14:00")["id"]
    assert sched.view()["routines"][0]["next_run"] == ts(2026, 10, 23, 14, 0)
    sched.start()
    clock.now = ts(2026, 10, 23, 14, 1)
    sched.check()
    until(lambda: runner.ran == ["tests"])
    until(lambda: sched.get(rid)["enabled"] is False)
    assert sched.view()["routines"][0]["next_run"] is None
    clock.now = ts(2026, 10, 23, 15, 0)
    sched.check()
    assert runner.ran == ["tests"]
    sched.shutdown()
    again, _ = make(tmp_path, now=ts(2026, 10, 24, 9, 0))
    again.check()
    assert again.view()["queued"] == []


def test_a_single_moment_past_its_time_to_live_is_skipped_and_the_routine_is_off(tmp_path, folder):
    sched, _ = make(tmp_path, now=ts(2026, 10, 23, 12, 0))
    rid = add(sched, folder, schedule="at 2026-10-23 14:00", ttl_min=30)["id"]
    late, _ = make(tmp_path, now=ts(2026, 10, 23, 16, 0))
    late.check()
    assert late.view()["queued"] == [] and late.get(rid)["enabled"] is False
    assert late.fires_log(rid)[0]["state"] == "skipped_late"


def test_the_time_to_live_and_the_key_are_checked(tmp_path, folder):
    sched, _ = make(tmp_path)
    for bad in (0, -5, True, "60", 1.5):
        with pytest.raises(RoutineError) as e:
            add(sched, folder, ttl_min=bad)
        assert e.value.code == "bad_ttl"
    for bad in ("two words", "x" * 81, 5):
        with pytest.raises(RoutineError) as e:
            add(sched, folder, dedup_key=bad)
        assert e.value.code == "bad_key"
    kept = add(sched, folder, ttl_min=45, dedup_key="pnl-morning")["id"]
    assert sched.get(kept)["ttl_min"] == 45 and sched.update(kept, ttl_min=None)["ttl_min"] is None


# -- the scheduler with a stand-in runner ------------------------------------------------------------------

class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeRunner:
    def __init__(self, hold=None):
        self.ran, self.active, self.most_active, self.hold = [], 0, 0, hold
        self.busy_now = False
        self.lock = threading.Lock()

    def busy(self):
        return self.busy_now

    def run(self, routine, cancel):
        with self.lock:
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        try:
            if self.hold:
                self.hold.wait(10)
            if routine["prompt"] == "explode":
                raise RuntimeError("boom")
            self.ran.append(routine["name"])
            return {"status": "done", "summary": "ok: " + routine["prompt"], "session": "s"}
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "project"
    f.mkdir()
    return str(f)


def make(tmp_path, runner=None, now=ts(2026, 10, 23, 12, 0), **kw):
    clock = Clock(now)
    sched = Scheduler(tmp_path / "data", runner or FakeRunner(), clock=clock, tick_s=3600, poll_s=0.02, **kw)
    return sched, clock


def add(sched, folder, name="tests", schedule="every 1h", prompt="run the tests", **kw):
    return sched.add(name, prompt, folder, schedule, **kw)


def test_a_new_routine_is_born_enabled_without_limits_and_runs_when_due(tmp_path, folder):
    runner = FakeRunner()
    sched, clock = make(tmp_path, runner)
    r = add(sched, folder)
    assert r["enabled"] is True and r["max_turns"] is None and r["max_minutes"] is None and r["yield_s"] is None
    assert "mode" not in r and "allow" not in r
    sched.start()
    clock.now += 4000
    sched.check()
    until(lambda: runner.ran == ["tests"])
    sched.shutdown()


def test_validation_messages(tmp_path, folder):
    sched, _ = make(tmp_path)
    for kw, code in [({"name": ""}, "bad_name"), ({"prompt": " "}, "bad_prompt"), ({"workspace": "relative"}, "bad_folder"),
                     ({"workspace": str(tmp_path / "nope")}, "bad_folder"), ({"schedule": "never"}, "bad_schedule"),
                     ({"max_turns": 0}, "bad_steps"), ({"max_turns": -1}, "bad_steps"), ({"max_turns": "5"}, "bad_steps"),
                     ({"max_minutes": 0}, "bad_minutes"), ({"max_minutes": True}, "bad_minutes")]:
        args = {"name": "n", "prompt": "p", "workspace": folder, "schedule": "every 1h", **kw}
        with pytest.raises(RoutineError) as e:
            sched.add(**args)
        assert e.value.code == code
    with pytest.raises(TypeError):
        add(sched, folder, mode="auto")                                     # a routine has no mode: it always runs in Automatic
    r = add(sched, folder, max_turns=500, max_minutes=90)
    assert (r["max_turns"], r["max_minutes"]) == (500, 90)
    assert sched.update(r["id"], max_turns=None, max_minutes=None)["max_minutes"] is None


def test_old_stored_routines_lose_their_mode_and_a_stored_limit_of_no_steps_is_empty(tmp_path, folder):
    sched, clock = make(tmp_path)
    rid = add(sched, folder)["id"]
    sched.routines[0].update(mode="read_only", allow=["shell_command:ls"], max_turns=-1, last_status="asked")
    sched.results.append({"id": "x", "routine": rid, "name": "tests", "started": 1, "ended": 2, "status": "asked", "summary": "", "session": ""})
    sched._save()
    sched._save_runs()
    again, _ = make(tmp_path, now=clock.now)
    r = again.get(rid)
    assert "mode" not in r and "allow" not in r and r["max_turns"] is None and r["last_status"] == "parked"
    assert again.runs(rid)[0]["status"] == "parked"


def test_an_enabled_interval_routine_runs_when_due_once_and_the_result_is_recorded(tmp_path, folder):
    runner = FakeRunner()
    sched, clock = make(tmp_path, runner)
    rid = add(sched, folder)["id"]
    clock.now += 1800
    sched.check()
    assert sched.view()["queued"] == []                          # not yet
    clock.now += 1900
    sched.start()
    sched.check()
    sched.check()                                                # a second look does not queue it twice
    until(lambda: runner.ran == ["tests"])
    until(lambda: sched.view()["running"] == [] and sched.runs())
    run = sched.runs(rid)[0]
    assert run["status"] == "done" and run["summary"] == "ok: run the tests" and run["name"] == "tests"
    assert sched.get(rid)["last_status"] == "done" and sched.get(rid)["last_run"] > 0
    clock.now += 100
    sched.check()
    assert sched.view()["queued"] == []                          # the next one is an hour after the check that queued it
    sched.shutdown()


def test_routines_and_results_survive_a_restart_and_an_overdue_interval_runs_once_at_start(tmp_path, folder):
    sched, clock = make(tmp_path)
    rid = add(sched, folder)["id"]
    sched.set_enabled(rid, True)
    sched.results.append({"id": "x", "routine": rid, "name": "tests", "started": 1, "ended": 2, "status": "done", "summary": "", "session": ""})
    sched._save_runs()
    sched.routines[0]["last_run"] = clock.now - 5 * 3600
    sched._save()
    again, _ = make(tmp_path, now=clock.now)
    assert [r["id"] for r in again.view()["routines"]] == [rid] and again.get(rid)["enabled"] is True
    assert again.runs(rid)[0]["id"] == "x"
    again.check()
    assert again.view()["queued"] == [rid]                       # five hours since its last run, every hour: due at once


def test_a_fixed_time_that_passed_while_off_is_run_once_late_at_start(tmp_path, folder):
    sched, clock = make(tmp_path, now=ts(2026, 10, 23, 12, 0))
    rid = add(sched, folder, schedule="daily 08:30")["id"]
    again, _ = make(tmp_path, now=ts(2026, 10, 24, 9, 0))        # started again after 08:30 the next day
    again.check()
    assert again.view()["queued"] == [rid]                       # made up, late, once
    assert again._due[rid] == ts(2026, 10, 25, 8, 30)


def test_a_fixed_time_that_ran_on_time_or_came_before_the_routine_is_not_made_up(tmp_path, folder):
    sched, clock = make(tmp_path, now=ts(2026, 10, 23, 12, 0))
    rid = add(sched, folder, schedule="daily 08:30")["id"]
    sched.routines[0]["last_run"] = ts(2026, 10, 24, 8, 30, 5)    # it did run at its time
    sched._save()
    again, _ = make(tmp_path, now=ts(2026, 10, 24, 9, 0))
    again.check()
    assert again.view()["queued"] == []
    again.check(ts(2026, 10, 25, 8, 31))
    assert again.view()["queued"] == [rid]
    late, _ = make(tmp_path / "other", now=ts(2026, 10, 24, 9, 0))
    new = add(late, folder, schedule="daily 08:30")["id"]        # made at 09:00: 08:30 was before it existed
    again2, _ = make(tmp_path / "other", now=ts(2026, 10, 24, 9, 30))
    again2.check()
    assert again2.view()["queued"] == [] and new in again2._due


def test_different_routines_run_in_parallel(tmp_path, folder):
    hold = threading.Event()
    runner = FakeRunner(hold)
    sched, clock = make(tmp_path, runner)
    ids = [add(sched, folder, name=name)["id"] for name in ("a", "b", "c")]
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: runner.active == 3)
    assert sorted(sched.view()["running"]) == sorted(ids) and sched.view()["queued"] == []
    hold.set()
    until(lambda: len(runner.ran) == 3)
    sched.shutdown()


def test_the_same_routine_never_overlaps_with_itself(tmp_path, folder):
    hold = threading.Event()
    runner = FakeRunner(hold)
    sched, clock = make(tmp_path, runner)
    rid = add(sched, folder)["id"]
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: runner.active == 1)
    clock.now += 4000
    sched.check()                                                # due again while it runs: it waits, once
    sched.check()
    sched.run_now(rid)
    time.sleep(0.3)
    assert runner.active == 1 and sched.view()["queued"] == [rid]
    hold.set()
    until(lambda: len(runner.ran) == 2)
    assert runner.most_active == 1
    sched.shutdown()


def test_a_conversation_of_the_person_does_not_hold_a_routine_back(tmp_path, folder):
    runner = FakeRunner()
    runner.busy_now = True
    sched, clock = make(tmp_path, runner)
    add(sched, folder)
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: runner.ran == ["tests"])
    sched.shutdown()


def test_a_failing_runner_is_a_failed_result_and_the_next_routine_still_runs(tmp_path, folder):
    runner = FakeRunner()
    sched, clock = make(tmp_path, runner)
    add(sched, folder, name="bad", prompt="explode")
    add(sched, folder, name="good")
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: len(sched.runs()) == 2)
    statuses = {r["name"]: r["status"] for r in sched.runs()}
    assert statuses == {"bad": "failed", "good": "done"}
    assert "boom" in next(r for r in sched.runs() if r["name"] == "bad")["summary"]
    sched.shutdown()


def test_run_now_works_for_a_disabled_routine_and_disabling_dequeues(tmp_path, folder):
    runner = Cooperative()
    sched, clock = make(tmp_path, runner)
    rid = add(sched, folder)["id"]
    sched.set_enabled(rid, False)
    sched.start()
    sched.run_now(rid)
    until(lambda: runner.active == 1)
    sched.run_now(rid)                                           # the same routine: it waits for the run in progress
    assert rid in sched.view()["queued"]
    sched.set_enabled(rid, False)
    assert rid not in sched.view()["queued"]
    sched.shutdown()


def test_pending_requests_are_kept_per_routine_dismissed_and_dropped_with_the_routine(tmp_path, folder):
    sched, _ = make(tmp_path)
    rid, other = add(sched, folder)["id"], add(sched, folder, name="other")["id"]
    request = {"session": "s1", "key": "a1", "kind": "ask", "question": "Archive it?", "options": ["Yes", "No"]}
    sched._park(rid, request)
    sched._park(rid, request)                                    # the same question is listed once
    sched._park(other, {"session": "s2", "kind": "blocked", "question": "pay may have run"})
    assert [p["question"] for p in sched.pending(rid)] == ["Archive it?"] and sched.pending(rid)[0]["options"] == ["Yes", "No"]
    assert len(sched.view()["pending"]) == 2
    again, _ = make(tmp_path)
    assert len(again.pending()) == 2                             # kept in a file
    again.dismiss(again.pending(rid)[0]["id"])
    assert again.pending(rid) == []
    with pytest.raises(RoutineError) as e:
        again.dismiss("nope")
    assert e.value.code == "gone"
    again.remove(other)
    assert again.pending() == []


class Asking(FakeRunner):
    """A routine that parks a request and goes on."""

    park = None

    def run(self, routine, cancel):
        self.park(routine["id"], {"session": "s", "key": "k", "question": "May I book it?"})
        self.ran.append(routine["name"])
        return {"status": "done", "summary": "went on", "session": "s"}


def test_a_parked_request_does_not_stop_the_run_or_mark_the_routine(tmp_path, folder):
    runner = Asking()
    sched, clock = make(tmp_path, runner)
    rid = add(sched, folder)["id"]
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: sched.runs(rid))
    assert sched.runs(rid)[0]["status"] == "done" and sched.get(rid)["last_status"] == "done"
    [p] = sched.pending(rid)
    assert p["question"] == "May I book it?" and p["run"] == sched.runs(rid)[0]["id"] and p["name"] == "tests"
    sched.shutdown()


def test_update_and_remove(tmp_path, folder):
    sched, _ = make(tmp_path)
    rid = add(sched, folder)["id"]
    assert sched.update(rid, schedule="daily 07:00", prompt="new")["schedule"] == "daily 07:00"
    with pytest.raises(RoutineError):
        sched.update(rid, schedule="nonsense")
    sched.remove(rid)
    with pytest.raises(RoutineError) as e:
        sched.get(rid)
    assert e.value.code == "gone"


# -- through the conversation manager, with the stand-in agent process ---------------------------------------

@pytest.fixture
def manager(tmp_path):
    mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", tick_s=0.1,
                         command=lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)])
    yield mgr
    mgr.shutdown()


def routine(folder, prompt, **fields):
    return {"id": "r1", "name": "daily check", "prompt": prompt, "workspace": folder, "max_turns": None, "max_minutes": None, **fields}


def test_a_routine_is_an_automatic_conversation_that_is_closed_when_it_ends(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    out = runner.run(routine(folder, "check the build"), threading.Event())
    assert out["status"] == "done" and out["summary"] == "echo: check the build"
    session = manager.get(out["session"])
    assert session["state"] == "closed" and session["title"] == "Routine: daily check" and session["mode"] == "auto"


def test_a_question_goes_to_the_list_and_the_run_goes_on_until_it_is_stopped(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05, cancel_grace_s=1)
    parked = []
    runner.park = lambda rid, req: parked.append((rid, req))
    cancel, out = threading.Event(), {}
    worker = threading.Thread(target=lambda: out.update(runner.run(routine(folder, "ask"), cancel)))
    worker.start()
    until(lambda: parked)
    time.sleep(0.3)
    assert worker.is_alive()                                         # not ended as "asked"
    assert parked[0][0] == "r1" and parked[0][1]["question"] == "Archive it?" and parked[0][1]["kind"] == "ask"
    cancel.set()
    worker.join(40)
    assert out["status"] == "cancelled"


def test_a_question_that_ends_the_turn_is_listed_and_the_run_is_parked_not_failed(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05, cancel_grace_s=1)
    parked = []
    runner.park = lambda rid, req: parked.append(req)
    out = runner.run(routine(folder, "question"), threading.Event())
    assert out["status"] == "parked" and out["summary"] == "Which day?"
    assert parked[0]["question"] == "Which day?" and parked[0]["options"] == ["Sat"]
    assert manager.get(out["session"])["state"] == "closed"


def test_what_the_harness_decided_for_the_person_is_listed_in_the_result(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05, cancel_grace_s=1)
    out = runner.run(routine(folder, "assume"), threading.Event())
    assert out["status"] == "done" and out["summary"].startswith("all done")
    assert "Assumptions made" in out["summary"] and "- Which database? -> PostgreSQL" in out["summary"] and "Call the vet" not in out["summary"]   # a parked one is not an assumption


def test_the_steps_limit_is_optional_and_a_time_limit_ends_the_run(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05, cancel_grace_s=1)
    steps, real = [], manager.create
    manager.create = lambda *a, **k: (steps.append(a[5]), real(*a, **k))[1]
    assert runner.run(routine(folder, "hello"), threading.Event())["status"] == "done"
    assert runner.run(routine(folder, "hello", max_turns=7), threading.Event())["status"] == "done"
    assert steps == [-1, 7]                                          # empty means no limit on the steps
    out = runner.run(routine(folder, "queue-me", max_minutes=0.01), threading.Event())
    assert out["status"] == "timeout"


def test_a_cancelled_routine_stops_its_conversation(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05, cancel_grace_s=1)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    out = runner.run(routine(folder, "queue-me"), cancel)             # the stand-in never finishes this one
    assert out["status"] == "cancelled"


def test_the_runner_is_busy_only_for_conversations_that_are_not_its_own(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    assert runner.busy() is False
    mine = manager.create(folder, ask_only=True)["id"]
    manager.send(mine, "queue-me")                                    # the person's conversation, at work
    until(lambda: manager.get(mine)["state"] == "busy")
    assert runner.busy() is True
    manager.close(mine)
    assert runner.busy() is False


def test_the_scheduler_runs_a_routine_through_the_manager(manager, tmp_path, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    clock = Clock(ts(2026, 10, 23, 12, 0))
    sched = Scheduler(tmp_path / "data", runner, clock=clock, tick_s=3600, poll_s=0.05)
    rid = sched.add("morning", "summarise", folder, "every 1h")["id"]
    sched.set_enabled(rid, True)
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: sched.runs(rid), seconds=30)
    assert sched.runs(rid)[0]["status"] == "done" and sched.runs(rid)[0]["summary"] == "echo: summarise"
    sched.shutdown()


class Cooperative(FakeRunner):
    """A routine that works until it is asked to stop."""

    def run(self, routine, cancel):
        with self.lock:
            self.active += 1
        try:
            end = time.monotonic() + 10
            while time.monotonic() < end:
                if cancel.is_set():
                    return {"status": "cancelled", "summary": "It was stopped.", "session": "s"}
                time.sleep(0.01)
            return {"status": "done", "summary": "finished", "session": "s"}
        finally:
            with self.lock:
                self.active -= 1


def start_one(tmp_path, folder, runner, **fields):
    sched, clock = make(tmp_path, runner)
    sched.set_enabled(add(sched, folder, **fields)["id"], True)
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: runner.active == 1)
    return sched


def test_a_running_routine_gives_way_when_a_conversation_of_the_person_has_been_at_work_for_its_budget(tmp_path, folder):
    runner = Cooperative()
    sched = start_one(tmp_path, folder, runner, yield_s=0)
    time.sleep(0.2)
    assert runner.active == 1                                           # nobody is talking: it goes on
    runner.busy_now = True
    until(lambda: sched.runs() and sched.runs()[0]["status"] == "yielded")
    run = sched.runs()[0]
    assert run["summary"].startswith("Stopped because a conversation of yours needed the model") and runner.active == 0
    assert sched.view()["queued"] == [] and sched.get(run["routine"])["yield_s"] == 0       # not run again by itself: the next due time does that
    sched.shutdown()


def test_the_budget_is_a_number_of_seconds_and_a_short_conversation_does_not_stop_the_routine(tmp_path, folder):
    runner = Cooperative()
    sched = start_one(tmp_path, folder, runner, yield_s=1)
    runner.busy_now = True
    time.sleep(0.4)
    runner.busy_now = False                                             # the conversation ended inside the budget
    time.sleep(1.3)
    assert runner.active == 1 and sched.runs() == []
    runner.busy_now = True
    until(lambda: sched.runs() and sched.runs()[0]["status"] == "yielded", seconds=5)
    sched.shutdown()


def test_without_a_budget_a_routine_is_never_stopped_for_a_conversation_and_the_person_can_stop_it(tmp_path, folder):
    runner = Cooperative()
    sched = start_one(tmp_path, folder, runner)
    assert sched.view()["routines"][0]["yield_s"] is None
    runner.busy_now = True
    time.sleep(0.6)
    assert runner.active == 1
    sched.cancel_running()
    until(lambda: sched.runs() and sched.runs()[0]["status"] == "cancelled")
    sched.shutdown()


@pytest.mark.parametrize("bad", [-1, 3601, "60", True, 1.5])
def test_a_budget_that_is_not_a_whole_number_of_seconds_in_range_is_refused(tmp_path, folder, bad):
    sched, _ = make(tmp_path)
    with pytest.raises(RoutineError):
        add(sched, folder, yield_s=bad)
    routine = add(sched, folder)
    with pytest.raises(RoutineError):
        sched.update(routine["id"], yield_s=bad)
    assert sched.update(routine["id"], yield_s=30)["yield_s"] == 30 and sched.update(routine["id"], yield_s=None)["yield_s"] is None


def test_a_routine_is_not_indexed_for_later_search_unless_the_person_says_so(tmp_path, folder):
    sched, _ = make(tmp_path, FakeRunner())
    first = add(sched, folder)
    assert first["index_past"] is False
    assert sched.update(first["id"], index_past=True)["index_past"] is True
    with pytest.raises(RoutineError, match="on or off"):
        sched.update(first["id"], index_past="yes")
    assert add(sched, folder, name="other", index_past=True)["index_past"] is True


# -- a check before the model, the outcome of each run, and the kill switch ----------------------------------------------------

def _make_with_runner(tmp_path, seen):
    class R:
        def run(self, routine, cancel):
            seen.append(routine)
            return {"status": "done", "summary": "Looked at it.\nOUTCOME: progress"}
    return Scheduler(tmp_path, R())


def _wait_runs(sched, n):
    end = time.monotonic() + 10
    while len(sched.results) < n and time.monotonic() < end:
        time.sleep(0.05)


def test_a_check_that_finds_nothing_ends_the_run_without_waking_the_model(tmp_path):
    seen = []
    sched = _make_with_runner(tmp_path, seen)
    folder = tmp_path / "w"
    folder.mkdir()
    r = sched.add("quiet", "do it", str(folder), "every 1d", check=f'"{sys.executable}" -c "import sys; print(\'no news\'); sys.exit(1)"')
    sched.start()
    sched.run_now(r["id"])
    _wait_runs(sched, 1)
    sched.shutdown()
    assert not seen and sched.results[0]["status"] == "skipped" and sched.results[0]["outcome"] == "nothing_new"


def test_a_check_that_passes_gives_its_words_to_the_model_and_the_outcome_is_kept_for_the_next_run(tmp_path):
    seen = []
    sched = _make_with_runner(tmp_path, seen)
    folder = tmp_path / "w"
    folder.mkdir()
    r = sched.add("news", "do it", str(folder), "every 1d", check=f'"{sys.executable}" -c "print(\'3 new files\')"')
    sched.start()
    sched.run_now(r["id"])
    _wait_runs(sched, 1)
    sched.run_now(r["id"])
    _wait_runs(sched, 2)
    sched.shutdown()
    assert seen[0]["check_output"] == "3 new files" and seen[0]["previous"] == []
    assert sched.results[0]["outcome"] == "progress" and seen[1]["previous"][0]["outcome"] == "progress"
    from tadamoo.scheduler import with_history
    text = with_history(seen[1])
    assert "Earlier runs" in text and "progress" in text and "3 new files" in text and text.rstrip().endswith("blocked.")


def test_the_kill_switch_stops_everything_until_it_is_taken_off(tmp_path):
    seen = []
    sched = _make_with_runner(tmp_path, seen)
    folder = tmp_path / "w"
    folder.mkdir()
    r = sched.add("any", "do it", str(folder), "every 1d")
    sched.emergency_stop(True)
    assert sched.view()["stopped"] and (tmp_path / "routines.stop").exists()
    sched.start()
    sched.run_now(r["id"])
    time.sleep(1.5)
    assert not seen
    sched.emergency_stop(False)
    _wait_runs(sched, 1)
    sched.shutdown()
    assert len(seen) == 1 and not sched.view()["stopped"]


# -- a fire that speaks to an existing conversation instead of starting one --------------------------------


def until_delivered(manager, delivery_id, seconds=15):
    return until(lambda: manager.deliveries.get(delivery_id)["state"] in ("delivered", "dead"), seconds)


def test_a_session_delivery_sends_the_message_to_an_existing_conversation_and_never_starts_one(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    target = manager.create(folder, alias="night")["id"]
    before = manager.view()["sessions"]
    out = runner.run(routine(folder, "watch the log", deliver_to={"kind": "session", "ref": "night"}, message="Check in, {date}."), threading.Event())
    assert out["status"] == "done" and "night" in out["summary"]
    assert len(manager.view()["sessions"]) == len(before)                     # no new conversation
    delivery_id = next(d["id"] for d in manager.deliveries.entries() if d["target"] == "night")
    until_delivered(manager, delivery_id)
    assert manager.deliveries.get(delivery_id)["provenance"] == "cron:r1"
    assert manager.history(target)["messages"][0]["content"].startswith("Check in, 20")      # the date was filled


def test_a_plan_delivery_carries_the_plans_provenance(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    manager.create(folder, alias="night")
    out = runner.run(routine(folder, "run the plan", deliver_to={"kind": "plan", "ref": "night"}, message="Run {plan_id}.", link={"plan": "P1"}),
                     threading.Event())
    assert out["status"] == "done"
    delivery_id = next(d["id"] for d in manager.deliveries.entries() if d["target"] == "night")
    until_delivered(manager, delivery_id)
    assert manager.deliveries.get(delivery_id)["provenance"] == "plan:P1"
    assert manager.history(manager.resolve("night"))["messages"][0]["content"] == "Run P1."


def test_a_session_delivery_to_nobody_is_a_failed_run_not_a_crash(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    out = runner.run(routine(folder, "x", deliver_to={"kind": "session", "ref": "nobody"}), threading.Event())
    assert out["status"] == "failed" and "unreachable" in out["summary"]


def test_without_a_message_the_prompt_itself_is_delivered(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    manager.create(folder, alias="night")
    runner.run(routine(folder, "watch the printer log", deliver_to={"kind": "session", "ref": "night"}), threading.Event())
    delivery_id = next(d["id"] for d in manager.deliveries.entries() if d["target"] == "night")
    until_delivered(manager, delivery_id)
    assert manager.history(manager.resolve("night"))["messages"][0]["content"] == "watch the printer log"


def test_a_session_delivery_through_the_scheduler_itself_is_recorded_as_a_run(tmp_path, manager, folder):
    sched = Scheduler(tmp_path / "data", SessionRoutineRunner(manager, poll_s=0.05), tick_s=3600, poll_s=0.02)
    manager.create(folder, alias="night")
    rid = sched.add("check", "watch it", folder, "every 1h", deliver_to={"kind": "session", "ref": "night"}, message="hi")["id"]
    sched.start()
    sched.check(sched.clock() + 4000)
    until(lambda: sched.runs(rid) and sched.runs(rid)[0]["status"] == "done")
    sched.shutdown()


def test_a_bad_deliver_to_message_or_link_is_refused(tmp_path, folder):
    sched, _ = make(tmp_path)
    for bad, code in (({"deliver_to": {"kind": "elsewhere", "ref": ""}}, "bad_deliver_to"),
                      ({"deliver_to": {"kind": "session", "ref": ""}}, "bad_deliver_to"),
                      ({"deliver_to": {"kind": "new_session", "ref": "x"}}, "bad_deliver_to"),
                      ({"message": "x" * 4001}, "bad_message"),
                      ({"link": {"plan": "P1", "extra": 1}}, "bad_link"),
                      ({"link": {"plan": 5}}, "bad_link")):
        with pytest.raises(RoutineError) as e:
            add(sched, folder, **bad)
        assert e.value.code == code, bad


def test_deliver_to_message_and_link_are_kept_and_updated(tmp_path, folder):
    sched, _ = make(tmp_path)
    rid = add(sched, folder, deliver_to={"kind": "session", "ref": "night"}, message="hi {date}", link={"plan": "P1"})["id"]
    routine = sched.get(rid)
    assert routine["deliver_to"] == {"kind": "session", "ref": "night"} and routine["message"] == "hi {date}" and routine["link"] == {"plan": "P1"}
    changed = sched.update(rid, deliver_to={"kind": "new_session", "ref": ""}, message="", link=None)
    assert changed["deliver_to"] == {"kind": "new_session", "ref": ""} and changed["message"] == "" and changed["link"] == {}


def test_an_old_routine_without_the_new_fields_defaults_to_new_session(tmp_path, folder):
    sched, _ = make(tmp_path)
    rid = add(sched, folder)["id"]
    sched.routines[0].pop("deliver_to", None)
    sched.routines[0].pop("message", None)
    sched.routines[0].pop("link", None)
    sched._save()
    again, _ = make(tmp_path)
    assert again.get(rid)["deliver_to"] == {"kind": "new_session", "ref": ""} and again.get(rid)["message"] == "" and again.get(rid)["link"] == {}


def test_a_message_delivery_needs_no_workspace_unless_it_has_a_check(tmp_path):
    sched, _ = make(tmp_path)
    rid = sched.add("tell", "x", "", "every 1h", deliver_to={"kind": "session", "ref": "night"})["id"]
    assert sched.get(rid)["workspace"] == ""
    with pytest.raises(RoutineError) as e:
        sched.add("tell2", "x", "", "every 1h", deliver_to={"kind": "session", "ref": "night"}, check="pytest -q")
    assert e.value.code == "bad_folder"


def test_a_new_session_routine_still_needs_a_real_folder(tmp_path):
    sched, _ = make(tmp_path)
    with pytest.raises(RoutineError) as e:
        sched.add("x", "y", "", "every 1h")
    assert e.value.code == "bad_folder"
