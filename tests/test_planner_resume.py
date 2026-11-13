"""A plan that was running when the program stopped takes up again where it was; a step that failed is recovered by the agent instead of stopping the plan.
The program that starts again is a new PlanService on the same folder; the agent's conversation is a recorder."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve.plans import RECOVER_LIMIT, RESUME_LIMIT, PlanService
from test_planner_starts import Recorder, n, plan, start


def boot(tmp_path):
    svc = PlanService(tmp_path / "planner")
    svc.launcher, svc.run_active = Recorder(), lambda plan_id: False
    return svc


def work(svc, plan_id, node, status, **extra):
    return svc.handle_event(plan_id, {"type": "plan:mark", "node_id": node, "status": status, "actor": "chat", **extra})


def chain_plan():
    return plan("p", start("go", 1), n("one", deps=["go"]), n("two", deps=["one"]), n("three", deps=["two"]))


def test_a_run_that_was_cut_short_takes_up_again_at_the_step_it_was_on_and_keeps_what_was_done(tmp_path):
    first = boot(tmp_path)
    first.create({"plan": chain_plan()})
    first.start_plan("p", 1, "the person")
    work(first, "p", "one", "in_progress")
    work(first, "p", "one", "completed", outputs={"found": 3})
    work(first, "p", "two", "in_progress")                                           # the PC restarts here
    again = boot(tmp_path)
    assert again.resume_interrupted() == ["p"]
    dag = again.bridge("p").dag
    assert dag.nodes["one"].status == "completed" and dag.shared_state["found"] == 3          # the step done stays done, with what it wrote
    assert dag.nodes["two"].status == "pending" and dag.nodes["two"].state_variables["interrupted"]["resume"] == 1
    assert dag.ready_nodes() == ["two"]
    assert [r[0] for r in again.launcher.runs] == ["p"] and "two" in again.launcher.runs[0][2] and "taken up again" in again.launcher.runs[0][2]
    assert again.resume_interrupted() == []                                          # once for each start of the program
    assert any(h["type"] == "resume" for h in again.bridge("p").history)


def test_a_plan_that_was_not_running_is_left_alone_and_so_is_a_paused_or_a_finished_one(tmp_path):
    first = boot(tmp_path)
    first.create({"plan": plan("idle", start("go", 1), n("a", deps=["go"]))})
    first.create({"plan": plan("paused", start("go", 1), n("a", deps=["go"]))})
    first.start_plan("paused", 1, "the person")
    first.set_paused("paused", "user", True)
    first.create({"plan": plan("done", start("go", 1), n("a", deps=["go"]))})
    first.start_plan("done", 1, "the person")
    work(first, "done", "a", "completed")
    first.create({"plan": plan("old", n("a"), n("b", deps=["a"]))})                  # a plan someone began by hand long ago, never started as a run
    work(first, "old", "a", "completed", )
    again = boot(tmp_path)
    assert again.resume_interrupted() == []
    assert again.launcher.runs == [] and again.bridge("done").run == {}


def test_a_plan_taken_up_again_without_a_step_more_is_left_for_the_person_after_a_few_times(tmp_path):
    first = boot(tmp_path)
    first.create({"plan": chain_plan()})
    first.start_plan("p", 1, "the person")
    work(first, "p", "one", "in_progress")
    seen = []
    for _ in range(RESUME_LIMIT + 1):
        again = boot(tmp_path)
        seen.append(again.resume_interrupted())
        work(again, "p", "one", "in_progress")                                       # and it is cut short again, before any step is done
    assert seen[:RESUME_LIMIT] == [["p"]] * RESUME_LIMIT and seen[RESUME_LIMIT] == []
    last = boot(tmp_path)
    assert last.bridge("p").run["stalled"] is True
    # a step done in between is progress: the count starts again
    work(last, "p", "one", "completed")
    work(last, "p", "two", "in_progress")
    after = boot(tmp_path)
    assert after.resume_interrupted() == ["p"] and after.bridge("p").run["resumes"] == 1


def test_a_step_that_failed_for_good_is_given_to_the_agent_to_find_the_cause(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("make", deps=["go"], description="Make the video", retry_policy={"max_attempts": 1, "wait_seconds": 0, "backoff": 1}))})
    svc.start_plan("p", 1, "the person")
    svc.launcher.runs.clear()
    work(svc, "p", "make", "in_progress")
    res = work(svc, "p", "make", "failed", reason="ffmpeg: no such filter 'zoompan2'")
    assert res["ok"] and svc.bridge("p").dag.nodes["make"].status == "failed"
    [(plan_id, name, text)] = svc.launcher.runs
    assert plan_id == "p" and name.startswith("Plan recovery") and "zoompan2" in text and "write or change code" in text and "recovery 1 of" in text
    node = svc.bridge("p").dag.nodes["make"]
    assert node.state_variables["recoveries"] == 1 and node.state_variables["attempts"] == 0
    assert node.state_variables["failures"][0]["reason"] == "ffmpeg: no such filter 'zoompan2'"


def test_a_step_that_still_has_tries_is_not_a_failure_to_recover(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("make", deps=["go"], retry_policy={"max_attempts": 3, "wait_seconds": 30, "backoff": 2}))})
    svc.start_plan("p", 1, "the person")
    svc.launcher.runs.clear()
    work(svc, "p", "make", "in_progress")
    res = work(svc, "p", "make", "failed", reason="timeout")
    assert res["retry"]["attempt"] == 1 and svc.launcher.runs == []


def test_the_recovery_of_the_same_step_is_tried_a_few_times_and_then_left_for_the_person(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("make", deps=["go"], retry_policy={"max_attempts": 1, "wait_seconds": 0, "backoff": 1}))})
    svc.start_plan("p", 1, "the person")
    svc.launcher.runs.clear()
    for i in range(RECOVER_LIMIT + 1):
        work(svc, "p", "make", "pending") if i else None
        work(svc, "p", "make", "in_progress")
        work(svc, "p", "make", "failed", reason=f"error {i}")
    assert len(svc.launcher.runs) == RECOVER_LIMIT
    assert any("left for you" in j["text"] for j in svc.bridge("p").dag.journal)
    assert "error 0" in svc.launcher.runs[-1][2] and "error 2" in svc.launcher.runs[-1][2]       # each recovery is told what the earlier tries found


def test_the_reason_a_step_failed_is_kept_and_the_tool_asks_for_it(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", n("a", retry_policy={"max_attempts": 1, "wait_seconds": 0, "backoff": 1}))})
    work(svc, "p", "a", "in_progress")
    work(svc, "p", "a", "failed", reason="x" * 2000)
    reasons = svc.bridge("p").dag.nodes["a"].state_variables["failures"]
    assert len(reasons) == 1 and len(reasons[0]["reason"]) == 600


# -- the steps the plan works out itself, and time running out ----------------------------------------------------------

POLICY = {"max_attempts": 2, "wait_seconds": 60, "backoff": 1}


def test_a_step_the_plan_works_out_itself_that_cannot_be_done_is_given_to_the_agent_when_its_tries_are_used_up(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("calc", "set_var", deps=["go"], spec={"name": "x", "expr": "missing + 1"}, retry_policy=POLICY), n("after", deps=["calc"]))})
    svc.start_plan("p", 1, "the person")
    svc.launcher.runs.clear()
    dag = svc.bridge("p").dag
    calc = dag.nodes["calc"]
    first = calc.state_variables["next_attempt"]
    assert calc.status == "pending" and calc.state_variables["attempts"] == 1 and "missing" in calc.state_variables["auto_error"]
    svc.tick("p", first - 1)
    assert calc.state_variables["attempts"] == 1 and svc.launcher.runs == []        # it is looked at again, but a try is not counted before its wait
    svc.tick("p", first + 1)
    assert calc.status == "failed" and dag.nodes["after"].state_variables["parked_by"] == ["calc"]
    [(plan_id, name, text)] = svc.launcher.runs
    assert name.startswith("Plan recovery") and "'set_var' step that the plan works out itself" in text and "missing + 1" in text and "recovery 1 of" in text


def test_the_moment_the_cause_goes_away_the_step_is_done_without_waiting_for_its_next_try(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("get", "get_var", deps=["go"], spec={"name": "later"}, retry_policy=POLICY), n("after", deps=["get"]))})
    svc.start_plan("p", 1, "the person")
    dag = svc.bridge("p").dag
    assert dag.nodes["get"].state_variables["attempts"] == 1
    dag.shared_state["later"] = 5
    svc.tick("p", dag.nodes["get"].state_variables["next_attempt"] - 30)
    assert dag.nodes["get"].status == "completed" and "attempts" not in dag.nodes["get"].state_variables and "auto_error" not in dag.nodes["get"].state_variables


def test_a_wait_that_runs_out_of_time_is_recovered_and_waits_afresh_when_marked_pending(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("w", "wait_condition", deps=["go"], spec={"expr": "ready", "timeout_s": 120, "poll_s": 60, "on_timeout": "fail"}), n("after", deps=["w"]))})
    svc.start_plan("p", 1, "the person")
    svc.launcher.runs.clear()
    dag = svc.bridge("p").dag
    started = dag.nodes["w"].state_variables["wait_started"]
    svc.tick("p", started + 121)
    assert dag.nodes["w"].state_variables["timed_out"] and dag.nodes["after"].state_variables["parked_by"] == ["w"]
    [(plan_id, name, text)] = svc.launcher.runs
    assert "timed out" in text and "waited longer than it may" in text and "'wait_condition'" in text
    dag.mark("w", "pending")                                                        # the agent has set what it waited for right, and puts it back
    assert dag.nodes["w"].status == "pending" and "timed_out" not in dag.nodes["w"].state_variables and "wait_started" not in dag.nodes["w"].state_variables
    assert dag.nodes["after"].status == "pending" and "parked_by" not in dag.nodes["after"].state_variables       # and what is below it is let go
    dag.advance(started + 500)
    assert dag.nodes["w"].status == "pending" and dag.nodes["w"].state_variables["wait_started"] == started + 500        # it waits afresh: the old start is not held against it


def test_a_step_that_timed_out_in_the_agents_hands_is_recovered_too(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("slow", deps=["go"]))})
    svc.start_plan("p", 1, "the person")
    svc.launcher.runs.clear()
    work(svc, "p", "slow", "in_progress")
    res = svc.handle_event("p", {"type": "plan:timeout", "node_id": "slow", "actor": "chat"})
    assert res["ok"] and len(svc.launcher.runs) == 1 and "timed out" in svc.launcher.runs[0][2]


def test_what_is_for_the_person_is_not_recovered_by_the_agent(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("ask", "input", deps=["go"], assignee="user", spec={"prompt": "How many?", "into": "n"}), n("mine", deps=["go"], assignee="user"))})
    svc.start_plan("p", 1, "the person")
    svc.launcher.runs.clear()
    svc.handle_event("p", {"type": "plan:timeout", "node_id": "ask", "actor": "system"})
    svc.handle_event("p", {"type": "plan:mark", "node_id": "mine", "status": "failed", "actor": "user"})
    assert svc.launcher.runs == []


def test_a_failed_or_timed_out_step_of_a_run_that_was_going_is_recovered_when_the_program_starts(tmp_path):
    first = boot(tmp_path)
    first.create({"plan": plan("p", start("go", 1), n("make", deps=["go"], retry_policy={"max_attempts": 1, "wait_seconds": 0, "backoff": 1}), n("w", "wait_condition", deps=["go"], spec={"expr": "ready", "timeout_s": 120, "poll_s": 60, "on_timeout": "fail"}))})
    first.start_plan("p", 1, "the person")
    first.launcher = None                                                           # the program stops before a recovery could begin
    work(first, "p", "make", "in_progress")
    work(first, "p", "make", "failed", reason="disk full")
    started = first.bridge("p").dag.nodes["w"].state_variables["wait_started"]
    first.tick("p", started + 200)
    again = boot(tmp_path)
    assert again.resume_interrupted() == ["p"]
    recovered = sorted(r[1] for r in again.launcher.runs if r[1].startswith("Plan recovery"))
    assert recovered == ["Plan recovery: make", "Plan recovery: w"] and "disk full" in " ".join(r[2] for r in again.launcher.runs)
    dag = again.bridge("p").dag
    assert dag.nodes["make"].state_variables["recoveries"] == 1 and dag.nodes["w"].state_variables["recoveries"] == 1


def test_the_recoveries_of_an_automatic_step_stop_at_the_limit_too(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("calc", "set_var", deps=["go"], spec={"name": "x", "expr": "missing + 1"}, retry_policy={"max_attempts": 1, "wait_seconds": 0, "backoff": 1}))})
    svc.start_plan("p", 1, "the person")
    node = svc.bridge("p").dag.nodes["calc"]
    for i in range(RECOVER_LIMIT):
        assert node.status == "failed" and len(svc.launcher.runs) >= i + 1
        work(svc, "p", "calc", "pending")
        svc.tick("p", time.time() + 10 + i)
    assert node.status == "failed" and len([r for r in svc.launcher.runs if r[1].startswith("Plan recovery")]) == RECOVER_LIMIT
    assert any("left for you" in j["text"] for j in svc.bridge("p").dag.journal)
