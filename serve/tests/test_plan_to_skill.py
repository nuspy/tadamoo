"""A plan that ended well becomes a personal skill: where each step's work comes from, the steps with no record approved one by one, the parameters
changed by form or by talking, and the script written from the plan's structure."""
import json

import pytest

from serve.agent_runs import RunError
from serve.tests.test_personal_skills_api import Model, manager  # noqa: F401  (fixture)

PLAN_ID = "uova"
PARAMS = [{"name": "basket", "type": "path", "value_seen": "basket", "label": "Basket"}]


def call(cid, name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def node(nid, title, status="completed", type="action", assignee="agent", deps=(), **extra):
    return {"id": nid, "type": type, "title": title, "description": extra.pop("description", ""), "assignee": assignee, "dependencies": list(deps), "outputs": [],
            "conditions": {}, "state_variables": extra.pop("state_variables", {}), "status": status, **extra}


def plan_of_eggs():
    return {"plan_id": PLAN_ID, "title": "Eggs per person", "order": ["a", "c", "b", "q", "r"], "shared_state": {"uova": 30, "presenti": 12, "a_testa": 2.5, "dove": "cestino"},
            "nodes": {"a": node("a", "Count the eggs", produces=[{"name": "uova", "type": "number"}], state_variables={"outputs": {"uova": 30}}),
                      "c": node("c", "Count the people", produces=[{"name": "presenti", "type": "number"}], state_variables={"outputs": {"presenti": 12}}),
                      "b": node("b", "Divide", deps=["a", "c"], description="Divide {uova} by {presenti}", produces=[{"name": "a_testa", "type": "number"}]),
                      "q": node("q", "Which basket?", type="input", assignee="user", spec={"prompt": "Which basket?", "into": "dove", "kind": "text"}),
                      "r": node("r", "Show it", type="output", spec={"name": "risultato", "template": "{a_testa}"})}}


def conversation_that_ran_the_plan(manager, tmp_path):  # noqa: F811
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = manager.create(str(ws))["id"]
    messages = [{"role": "user", "content": "run the eggs plan", "ts": 1791935520.0}]
    for cid, nid, tool, args, result, out in (("1", "a", "list_dir", {"path": "basket"}, "egg egg egg", {"uova": 30}), ("2", "c", "read_file", {"path": "guests.txt"}, "12 people", {"presenti": 12})):
        messages += [call("s" + cid, "plan_step_start", plan_id=PLAN_ID, node_id=nid), {"role": "tool", "tool_call_id": "s" + cid, "content": "Step started"},
                     call("t" + cid, tool, **args), {"role": "tool", "tool_call_id": "t" + cid, "content": result},
                     call("f" + cid, "read_file", path="missing.txt"), {"role": "tool", "tool_call_id": "f" + cid, "content": "Tool execution error: no such file"},
                     call("m" + cid, "plan_mark", plan_id=PLAN_ID, node_id=nid, status="completed", outputs=out), {"role": "tool", "tool_call_id": "m" + cid, "content": "ok"}]
    folder = manager.history_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "history.json").write_text(json.dumps({"turns": 1, "messages": messages}), encoding="utf-8")
    return sid


def test_a_plan_that_ended_well_shows_where_the_work_of_each_step_comes_from(manager, tmp_path, monkeypatch):  # noqa: F811
    sid = conversation_that_ran_the_plan(manager, tmp_path)
    proposal = json.dumps({"name": "Eggs", "description": "eggs per person", "params": [{"name": "basket", "type": "path", "value_seen": "basket", "explanation": "where"}]})
    monkeypatch.setattr(manager, "_skill_client", lambda: Model(proposal))
    got = manager.plan_skill_propose(plan_of_eggs(), PLAN_ID)
    how = {row["id"]: row["how"] for row in got["coverage"]}
    assert how == {"a": "recorded", "c": "recorded", "b": "missing", "q": "asks", "r": "plan"} and got["session"] == sid
    assert [r["n"] for r in got["requests"]] == [1, 2, 3, 4, 5]
    assert {p["name"] for p in got["params"]} == {"basket", "dove"}                                  # the question of the plan is a parameter as it is
    assert got["coverage"][0]["tools"] == ["list_dir"]                                              # the plan's own calls are not part of the work


def test_the_steps_with_no_record_are_proposed_for_approval_and_the_script_waits_for_it(manager, tmp_path, monkeypatch):  # noqa: F811
    sid = conversation_that_ran_the_plan(manager, tmp_path)
    plan = plan_of_eggs()
    script = "```python\nfrom tadamoo_skill import tool, ai\n\ndef step_a(params):\n    return {'uova': 30}\n\ndef main(params):\n    return tool('list_dir', path=params.basket)\n```"
    model = Model(json.dumps({"steps": [{"name": "calculate", "arguments": {"expression": "30/12"}, "why": "divide"}]}), script)
    monkeypatch.setattr(manager, "_skill_client", lambda: model)
    filled = manager.plan_skill_fill(plan, PLAN_ID, None)
    assert list(filled["proposed"]) == ["b"] and filled["proposed"]["b"][0]["name"] == "calculate"
    with pytest.raises(RunError) as err:                                                           # nothing is written for a step nobody approved
        manager.plan_skill_script(plan, PLAN_ID, "Eggs", "", PARAMS, {})
    assert err.value.code == "missing_steps" and "b" in str(err.value)
    got = manager.plan_skill_script(plan, PLAN_ID, "Eggs", "eggs per person", PARAMS, {"b": filled["proposed"]["b"]})
    prompt = model.sent[-1][1]["content"]
    assert "The plan:" in prompt and "step_<id>" in prompt and "(proposed, never run)" in prompt and "Divide {uova} by {presenti}" in prompt
    assert "<<params.basket>>" in prompt and "FAILED" in prompt                                    # the failed read is told as not part of the way
    assert got["skill"]["source"] == {"plan": PLAN_ID, "conversation": sid} and "step_a" in got["script"]
    assert manager.skills.save(got["skill"], got["script"])["status"] == "approved"


def test_a_step_left_to_the_model_is_an_empty_approval_and_a_plan_that_has_not_ended_is_refused(manager, tmp_path, monkeypatch):  # noqa: F811
    conversation_that_ran_the_plan(manager, tmp_path)
    monkeypatch.setattr(manager, "_skill_client", lambda: Model("```python\ndef main(params):\n    return 'x'\n```"))
    got = manager.plan_skill_script(plan_of_eggs(), PLAN_ID, "Eggs", "", [], {"b": []})
    assert got["script"].strip().startswith("def main")
    pending = plan_of_eggs()
    pending["nodes"]["b"]["status"] = "pending"
    for action in (lambda: manager.plan_skill_propose(pending, PLAN_ID), lambda: manager.plan_skill_script(pending, PLAN_ID, "x", "", [], {})):
        with pytest.raises(RunError) as err:
            action()
        assert err.value.code == "not_finished"


def test_the_parameters_are_changed_by_talking_and_a_reply_that_makes_no_sense_changes_nothing(manager, tmp_path, monkeypatch):  # noqa: F811
    conversation_that_ran_the_plan(manager, tmp_path)
    reply = json.dumps({"reply": "Done: guests too.", "params": [*PARAMS, {"name": "guests", "label": "Guests", "type": "path", "value_seen": "guests.txt", "explanation": "who"}]})
    monkeypatch.setattr(manager, "_skill_client", lambda: Model(reply))
    got = manager.plan_skill_chat(plan_of_eggs(), PLAN_ID, PARAMS, "add the guests file as a parameter")
    assert got["changed"] and [p["name"] for p in got["params"]] == ["basket", "guests"] and got["reply"] == "Done: guests too."
    monkeypatch.setattr(manager, "_skill_client", lambda: Model("not json at all"))
    again = manager.plan_skill_chat(plan_of_eggs(), PLAN_ID, PARAMS, "remove it")
    assert again["changed"] is False and again["params"] == PARAMS
    with pytest.raises(RunError):
        manager.plan_skill_chat(plan_of_eggs(), PLAN_ID, "no", "x")


def test_a_plan_nobody_ran_in_a_conversation_has_every_action_to_approve(manager, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(manager, "_skill_client", lambda: Model("{}"))
    got = manager.plan_skill_propose(plan_of_eggs(), PLAN_ID)
    assert got["session"] == "" and {row["id"]: row["how"] for row in got["coverage"]}["a"] == "missing"
