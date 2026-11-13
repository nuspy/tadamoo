"""A plan that was running when the program stopped takes up again where it was; a step that failed is recovered by the agent instead of stopping the plan.
The program that starts again is a new PlanService on the same folder; the agent's conversation is a recorder."""
import sys
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
