"""Routines: schedule text, parallel runs that never overlap with themselves, optional limits, parked requests, persistence, and a run through the conversation manager."""
import datetime as dt
import sys
import threading
import time
from pathlib import Path

import pytest

from harness.scheduler import RoutineError, Schedule, Scheduler, SessionRoutineRunner
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
                                  "08:00", "cron * * * * *", None, 5])
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
