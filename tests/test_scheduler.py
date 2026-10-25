"""Routines: schedule text, the queue (one at a time, the person's conversation first), persistence, and a run through the conversation manager."""
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

@pytest.mark.parametrize("text,seconds", [("every 30m", 1800), ("Every 2h", 7200), ("every  1d", 86400), ("every 1m", 60)])
def test_intervals(text, seconds):
    assert Schedule.parse(text).interval_s == seconds


@pytest.mark.parametrize("text", ["", "every 30s", "every 0m", "every 5x", "daily 25:00", "daily 08:61", "someday 08:00", "mon,xyz 08:00",
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


def test_a_new_routine_is_disabled_read_only_and_never_runs_until_enabled(tmp_path, folder):
    sched, clock = make(tmp_path)
    r = add(sched, folder)
    assert r["enabled"] is False and r["mode"] == "read_only" and r["max_turns"] == 10
    clock.now += 10 * 3600
    sched.check()
    assert sched.view()["queued"] == []


def test_validation_messages(tmp_path, folder):
    sched, _ = make(tmp_path)
    for kw, code in [({"name": ""}, "bad_name"), ({"prompt": " "}, "bad_prompt"), ({"workspace": "relative"}, "bad_folder"),
                     ({"workspace": str(tmp_path / "nope")}, "bad_folder"), ({"schedule": "never"}, "bad_schedule"),
                     ({"mode": "root"}, "bad_mode"), ({"mode": "auto"}, "auto_confirm"), ({"allow": ["a\nb"]}, "bad_rules"),
                     ({"max_turns": 0}, "bad_steps")]:
        args = {"name": "n", "prompt": "p", "workspace": folder, "schedule": "every 1h", **kw}
        with pytest.raises(RoutineError) as e:
            sched.add(**args)
        assert e.value.code == code
    assert add(sched, folder, mode="auto", confirm_auto=True)["confirm_auto"] is True


def test_an_enabled_interval_routine_runs_when_due_once_and_the_result_is_recorded(tmp_path, folder):
    runner = FakeRunner()
    sched, clock = make(tmp_path, runner)
    rid = add(sched, folder)["id"]
    sched.set_enabled(rid, True)
    clock.now += 1800
    sched.check()
    assert sched.view()["queued"] == []                          # not yet
    clock.now += 1900
    sched.start()
    sched.check()
    sched.check()                                                # a second look does not queue it twice
    until(lambda: runner.ran == ["tests"])
    until(lambda: sched.view()["running"] is None and sched.runs())
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


def test_a_fixed_time_that_passed_while_off_is_not_made_up(tmp_path, folder):
    sched, clock = make(tmp_path, now=ts(2026, 10, 23, 12, 0))
    rid = add(sched, folder, schedule="daily 08:30")["id"]
    sched.set_enabled(rid, True)
    again, _ = make(tmp_path, now=ts(2026, 10, 24, 9, 0))        # started again after 08:30 the next day
    again.check()
    assert again.view()["queued"] == []
    again.check(ts(2026, 10, 25, 8, 31))
    assert again.view()["queued"] == [rid]


def test_one_routine_at_a_time(tmp_path, folder):
    hold = threading.Event()
    runner = FakeRunner(hold)
    sched, clock = make(tmp_path, runner)
    for name in ("a", "b", "c"):
        sched.set_enabled(add(sched, folder, name=name)["id"], True)
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: runner.active == 1)
    time.sleep(0.3)
    assert runner.active == 1 and len(sched.view()["queued"]) == 3
    hold.set()
    until(lambda: len(runner.ran) == 3)
    assert runner.most_active == 1
    sched.shutdown()


def test_a_conversation_of_the_person_goes_first(tmp_path, folder):
    runner = FakeRunner()
    runner.busy_now = True
    sched, clock = make(tmp_path, runner)
    sched.set_enabled(add(sched, folder)["id"], True)
    clock.now += 4000
    sched.start()
    sched.check()
    time.sleep(0.4)
    assert runner.ran == [] and len(sched.view()["queued"]) == 1       # waiting, not running
    runner.busy_now = False
    until(lambda: runner.ran == ["tests"])
    sched.shutdown()


def test_waiting_too_long_for_the_conversation_is_recorded_as_skipped(tmp_path, folder):
    runner = FakeRunner()
    runner.busy_now = True
    sched, clock = make(tmp_path, runner, max_wait_s=0.2)
    sched.set_enabled(add(sched, folder)["id"], True)
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: sched.runs() and sched.runs()[0]["status"] == "skipped")
    assert runner.ran == []
    sched.shutdown()


def test_a_failing_runner_is_a_failed_result_and_the_next_routine_still_runs(tmp_path, folder):
    runner = FakeRunner()
    sched, clock = make(tmp_path, runner)
    sched.set_enabled(add(sched, folder, name="bad", prompt="explode")["id"], True)
    sched.set_enabled(add(sched, folder, name="good")["id"], True)
    clock.now += 4000
    sched.start()
    sched.check()
    until(lambda: len(sched.runs()) == 2)
    statuses = {r["name"]: r["status"] for r in sched.runs()}
    assert statuses == {"bad": "failed", "good": "done"}
    assert "boom" in next(r for r in sched.runs() if r["name"] == "bad")["summary"]
    sched.shutdown()


def test_run_now_works_for_a_disabled_routine_and_disabling_dequeues(tmp_path, folder):
    runner = FakeRunner()
    sched, clock = make(tmp_path, runner)
    rid = add(sched, folder)["id"]
    sched.start()
    sched.run_now(rid)
    until(lambda: runner.ran == ["tests"])
    runner.busy_now = True                                       # the next run waits, so it is still in the queue
    sched.set_enabled(rid, True)
    clock.now += 4000
    sched.check()
    assert rid in sched.view()["queued"]
    sched.set_enabled(rid, False)
    assert rid not in sched.view()["queued"]
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


def routine(folder, prompt):
    return {"name": "daily check", "prompt": prompt, "workspace": folder, "mode": "read_only", "allow": [], "max_turns": 5}


def test_a_routine_is_a_conversation_that_is_closed_when_it_ends(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    out = runner.run(routine(folder, "check the build"), threading.Event())
    assert out["status"] == "done" and out["summary"] == "echo: check the build"
    session = manager.get(out["session"])
    assert session["state"] == "closed" and session["title"] == "Routine: daily check" and session["mode"] == "read_only"


def test_a_question_nobody_can_answer_ends_the_routine_as_asked(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
    out = runner.run(routine(folder, "ask"), threading.Event())
    assert out["status"] == "asked" and out["summary"] == "Archive it?"
    assert manager.get(out["session"])["state"] == "closed"


def test_a_cancelled_routine_stops_its_conversation(manager, folder):
    runner = SessionRoutineRunner(manager, poll_s=0.05)
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
