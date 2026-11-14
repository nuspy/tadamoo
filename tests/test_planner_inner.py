"""A step that does several things in a row keeps in the plan which of them it has finished, with their essential results, so that after an interruption (a crash, a
restart of the app or of the PC) it goes on from there and does not do them again. The restart is a new PlanService on the same folder."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import PlanDAG, PlanError
from tadamoo.planner import inner as inner_mod
from tadamoo.planner.nodes import clean_spec, inner_state, inner_text
from serve.plans import PlanService
from test_planner_starts import Recorder, n, plan, start

STEPS = ["voice", "stills", "takes", "broll", "edit"]


def video(**kw):
    return n("video", deps=["go"], spec={"steps": STEPS}, **kw)


def video_plan():
    return plan("p", start("go", 1), video(), n("after", deps=["video"]))


def boot(tmp_path):
    svc = PlanService(tmp_path / "planner")
    svc.launcher, svc.run_active = Recorder(), lambda plan_id: False
    return svc


# -- what is kept -------------------------------------------------------------------------------------------------------------------

def test_a_step_names_its_inner_steps_in_order_and_only_those_can_be_recorded():
    assert clean_spec("action", {"steps": STEPS})["steps"] == STEPS
    for bad in (["a", "a"], ["x" * 61]):
        with pytest.raises(PlanError, match="different names"):
            clean_spec("action", {"steps": bad})
    dag = PlanDAG.from_dict(video_plan())
    dag.begin(1, "the person")
    with pytest.raises(PlanError, match="these inner steps: voice, stills"):
        dag.record_inner("video", "dubbing", {})
    info = dag.record_inner("video", "stills", {"made": 6})                       # in any order: they are kept in the order of the step
    info = dag.record_inner("video", "voice", {"files": ["voice/a.wav", "voice/b.wav"]})
    assert [d["name"] for d in dag.nodes["video"].state_variables["inner_done"]] == ["voice", "stills"]
    assert (info["next"], info["left"]) == ("takes", ["takes", "broll", "edit"])


def test_the_result_is_the_essential_plain_data_of_bounded_size_and_a_step_done_again_is_replaced():
    dag = PlanDAG.from_dict(video_plan())
    dag.begin(1, "the person")
    with pytest.raises(PlanError, match="plain data"):
        dag.record_inner("video", "voice", ["not", "an", "object"])
    with pytest.raises(PlanError, match="essential"):
        dag.record_inner("video", "voice", {"content": "x" * 3000})
    with pytest.raises(PlanError, match="name of an inner step"):
        dag.record_inner("video", "  ", {})
    dag.record_inner("video", "voice", {"made": 1})
    dag.record_inner("video", "voice", {"made": 2})
    assert dag.nodes["video"].state_variables["inner_done"][0]["result"] == {"made": 2} and len(dag.nodes["video"].state_variables["inner_done"]) == 1
    dag.forget_inner("video", "voice")
    assert dag.nodes["video"].state_variables["inner_done"] == []
    with pytest.raises(PlanError, match="not among"):
        dag.forget_inner("video", "voice")


def test_a_step_that_names_none_takes_any_name_and_a_step_that_is_over_or_not_a_task_takes_none():
    dag = PlanDAG.from_dict(plan("p", start("go", 1), n("free", deps=["go"]), n("calc", "set_var", deps=["go"], spec={"name": "x", "value": 1})))
    dag.begin(1, "the person")
    info = dag.record_inner("free", "anything", {"files": ["a"]})
    assert info["declared"] == [] and info["next"] is None and [d["name"] for d in info["done"]] == ["anything"]
    with pytest.raises(PlanError, match="not a step that is being done"):
        dag.record_inner("calc", "x", {})
    dag.mark("free", "completed")
    with pytest.raises(PlanError, match="not a step that is being done"):
        dag.record_inner("free", "more", {})


def test_what_is_kept_is_saved_with_the_plan_and_a_rewind_forgets_it_but_the_history_keeps_it():
    dag = PlanDAG.from_dict(video_plan())
    dag.begin(1, "the person")
    dag.record_inner("video", "voice", {"made": 4})
    again = PlanDAG.from_dict(json.loads(json.dumps(dag.to_dict())))
    assert again.nodes["video"].state_variables["inner_done"][0]["result"] == {"made": 4}
    again.rewind("video")
    assert "inner_done" not in again.nodes["video"].state_variables
    assert again.nodes["video"].state_variables["history"][-1]["inner_done"][0]["name"] == "voice"


def test_the_words_a_step_taken_up_again_is_told_say_what_is_finished_and_where_to_go_on():
    dag = PlanDAG.from_dict(video_plan())
    dag.begin(1, "the person")
    node = dag.nodes["video"]
    assert "Its inner steps, in order: voice, stills" in inner_text(node.spec, node.state_variables) and "plan_step_done" in inner_text(node.spec, node.state_variables)
    dag.record_inner("video", "voice", {"files": ["voice/a.wav"], "made": 3})
    text = inner_text(node.spec, node.state_variables)
    assert text.startswith("Already done inside this step (do not do them again): voice (") and "voice/a.wav" in text
    assert "Continue from: stills. Still to do: stills, takes, broll, edit." in text
    for s in STEPS[1:]:
        dag.record_inner("video", s, {})
    assert "All its declared steps are done" in inner_text(node.spec, node.state_variables)
    free = PlanDAG.from_dict(plan("q", start("go", 1), n("w", deps=["go"])))
    free.begin(1, "the person")
    free.record_inner("w", "one", {})
    free_node = free.nodes["w"]
    assert "Continue with what comes after them" in inner_text(free_node.spec, free_node.state_variables)
    assert inner_text({}, {}) == "" and inner_state({}, {})["done"] == []


# -- after an interruption, and a restart ---------------------------------------------------------------------------------------------------

def test_a_step_cut_short_in_the_middle_goes_on_from_the_inner_step_it_had_reached_after_a_restart(tmp_path):
    first = boot(tmp_path)
    first.create({"plan": video_plan()})
    first.start_plan("p", 1, "the person")
    first.handle_event("p", {"type": "plan:mark", "node_id": "video", "status": "in_progress", "session": "S1", "actor": "chat"})
    assert first.bridge("p").dag.nodes["video"].state_variables["session"] == "S1"
    # the commands of the production say what they finished, and are found by the conversation that runs them
    first.record_inner("voice", {"files": ["voice/all.wav"], "blocks": 32}, session="S1")
    got = first.record_inner("stills", {"made": 6}, session="S1")
    assert (got["plan_id"], got["node_id"]) == ("p", "video") and got["inner"]["next"] == "takes"
    # the PC restarts here: a new program reads the plan from its folder
    again = boot(tmp_path)
    assert again.resume_interrupted() == ["p"]
    node = again.bridge("p").dag.nodes["video"]
    assert node.status == "pending" and "session" not in node.state_variables
    assert [d["name"] for d in node.state_variables["inner_done"]] == ["voice", "stills"]            # what was finished stays finished
    assert node.state_variables["inner_done"][0]["result"] == {"files": ["voice/all.wav"], "blocks": 32}
    [(plan_id, name, text)] = again.launcher.runs
    assert "Already done inside this step (do not do them again): voice (" in text and "voice/all.wav" in text
    assert "Continue from: takes. Still to do: takes, broll, edit." in text
    # the conversation that takes it up goes on, records the next stage, and the step is finished
    again.handle_event("p", {"type": "plan:mark", "node_id": "video", "status": "in_progress", "session": "S2", "actor": "chat"})
    again.record_inner("takes", {"made": 9}, session="S2")
    again.record_inner("broll", {"clips": 12}, session="S2")
    again.record_inner("edit", {"output": "video.mp4"}, session="S2")
    again.handle_event("p", {"type": "plan:mark", "node_id": "video", "status": "completed", "actor": "chat"})
    done = again.bridge("p").dag.nodes["video"].state_variables["inner_done"]
    assert [d["name"] for d in done] == STEPS and done[-1]["result"] == {"output": "video.mp4"}


def test_a_step_that_failed_is_recovered_knowing_what_it_had_finished(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("video", deps=["go"], spec={"steps": STEPS}, retry_policy={"max_attempts": 1, "wait_seconds": 0, "backoff": 1}))})
    svc.start_plan("p", 1, "the person")
    svc.launcher.runs.clear()
    svc.handle_event("p", {"type": "plan:mark", "node_id": "video", "status": "in_progress", "session": "S1", "actor": "chat"})
    svc.record_inner("voice", {"made": 32}, session="S1")
    svc.handle_event("p", {"type": "plan:mark", "node_id": "video", "status": "failed", "reason": "the GPU stopped", "actor": "chat"})
    [(_, name, text)] = svc.launcher.runs
    assert name.startswith("Plan recovery") and "Already done inside this step (do not do them again): voice" in text and "Continue from: stills" in text


def test_the_conversation_is_found_by_its_step_unless_there_are_none_or_several(tmp_path):
    svc = boot(tmp_path)
    svc.create({"plan": plan("p", start("go", 1), n("a", deps=["go"]), n("b", deps=["go"]))})
    svc.start_plan("p", 1, "the person")
    with pytest.raises(ValueError, match="no step is being done"):
        svc.record_inner("x", {}, session="S1")
    with pytest.raises(ValueError, match="say which step"):
        svc.record_inner("x", {})
    for node in ("a", "b"):
        svc.handle_event("p", {"type": "plan:mark", "node_id": node, "status": "in_progress", "session": "S1", "actor": "chat"})
    with pytest.raises(ValueError, match="several steps"):
        svc.record_inner("x", {}, session="S1")
    assert svc.record_inner("x", {"n": 1}, session="S1", plan_id="p", node_id="a")["inner"]["done"][0]["name"] == "x"       # named: no doubt
    with pytest.raises(ValueError, match="not a step that is being done|not asked|is not"):
        svc.record_inner("x", {}, plan_id="p", node_id="go")


# -- what the program's commands do -----------------------------------------------------------------------------------------------------------

def test_a_command_records_a_stage_through_the_server_and_a_server_that_does_not_answer_never_stops_it(monkeypatch):
    sent = []

    def fake(self, method, path, payload=None):
        sent.append((self.endpoint, self.api_key, method, path, payload))
        return {}

    monkeypatch.setattr(inner_mod.PlannerClient, "_call", fake)
    monkeypatch.setenv("TADAMOO_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("TADAMOO_API_KEY", "k")
    monkeypatch.setenv("TADAMOO_SESSION_ID", "S9")
    assert inner_mod.record("voice", {"made": 3}) is True
    endpoint, key, method, path, payload = sent[0]
    assert (endpoint, key, method, path) == ("http://127.0.0.1:9999", "k", "POST", "/api/plans/inner")
    assert payload["step"] == "voice" and payload["result"] == {"made": 3} and payload["session"] == "S9"

    def down(self, method, path, payload=None):
        raise OSError("refused")

    monkeypatch.setattr(inner_mod.PlannerClient, "_call", down)
    assert inner_mod.record("voice", {}) is False


def test_each_phase_of_a_production_that_ends_is_a_stage_told_to_the_plan(monkeypatch):
    told = []
    monkeypatch.setattr(inner_mod, "record", lambda step, result=None, **where: told.append((step, result, where)) or True)
    listen = inner_mod.phase_recorder("E:/work/run", url="http://x", key="k")
    listen({"event": "voice", "note": "round 1"})                                  # not a phase
    listen({"event": "phase", "phase": "voice", "blocks": 32, "voice": "gitta", "nested": {"a": 1}})
    listen({"event": "phase", "phase": "stills", "made": 6})
    assert told == [("voice", {"folder": "E:/work/run", "blocks": 32, "voice": "gitta"}, {"url": "http://x", "key": "k"}),
                    ("stills", {"folder": "E:/work/run", "made": 6}, {"url": "http://x", "key": "k"})]
