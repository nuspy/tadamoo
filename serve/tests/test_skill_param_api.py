"""The tools of the parameter card, and a run that sets the parameters the model sets in one call."""
import json
import time

import pytest

from serve.agent_runs import RunError
from serve.tests.test_personal_skills_api import Model, manager  # noqa: F401  (fixture)


def test_a_python_expression_is_tried_and_a_description_is_turned_into_one_that_works(manager, monkeypatch):  # noqa: F811
    spec = {"name": "when", "type": "datetime", "describe": "the date and hour at which the script starts"}
    model = Model("```\nnow()\n```")
    monkeypatch.setattr(manager, "_skill_client", lambda: model)
    got = manager.skill_param("to-python", spec)
    assert got["ok"] and got["expr"] == "now()" and len(got["value"]) in (16, 19)
    assert "Description: the date and hour" in model.sent[0][1]["content"] and "now()" in model.sent[0][0]["content"]
    check = manager.skill_param("check", {**spec, "source": "python", "expr": "now() - timedelta(hours=1)"})
    assert check["ok"] and check["value"]
    assert not manager.skill_param("check", {**spec, "source": "python", "expr": "open('f')"})["ok"]


def test_an_expression_that_fails_is_given_back_to_the_model_once_with_the_reason(manager, monkeypatch):  # noqa: F811
    model = Model("datetime.now()", "now()")
    monkeypatch.setattr(manager, "_skill_client", lambda: model)
    got = manager.skill_param("to-python", {"name": "when", "type": "datetime", "describe": "now"})
    assert got["ok"] and got["expr"] == "now()" and len(model.sent) == 2
    assert "That does not work" in model.sent[1][-1]["content"]
    again = Model("nonsense(", "still nonsense(")
    monkeypatch.setattr(manager, "_skill_client", lambda: again)
    failed = manager.skill_param("to-python", {"name": "when", "type": "datetime", "describe": "now"})
    assert not failed["ok"] and failed["error"] and failed["expr"] == "still nonsense("


def test_the_card_tools_refuse_what_they_cannot_do(manager):  # noqa: F811
    for args in (("other", {}), ("check", "no"), ("to-python", {"name": "a"}), ("check", {"name": "a"}, "no")):
        with pytest.raises(RunError):
            manager.skill_param(*args)


def test_a_run_sets_the_python_parameters_itself_and_the_ones_of_the_model_in_one_call(manager, tmp_path, monkeypatch):  # noqa: F811
    ws = tmp_path / "ws"
    ws.mkdir()
    definition = {"name": "Report", "description": "d", "params": [
        {"name": "city", "type": "text", "label": "City", "default": "rome"},
        {"name": "stamp", "type": "datetime", "label": "Stamp", "source": "python", "expr": "now()"},
        {"name": "mood", "type": "text", "label": "Mood", "source": "llm", "describe": "how the day feels"},
        {"name": "topic", "type": "text", "label": "Topic", "source": "llm", "describe": "one word about the city"},
        {"name": "later", "type": "text", "label": "Later", "source": "llm", "describe": "what step two found", "when": "later"}]}
    manager.skills.save(definition, "def main(params):\n    return params.city\n")
    sid = manager.create(str(ws))["id"]
    model = Model(json.dumps({"mood": "calm", "topic": "history"}))
    monkeypatch.setattr(manager, "_skill_client", lambda: model)
    manager.run_skill(sid, "report", {})
    assert len(model.sent) == 1                                                                   # one call for both
    asked = model.sent[0][1]["content"]
    assert "- mood (text): how the day feels" in asked and "- topic (text): one word about the city" in asked and "what step two found" not in asked
    assert '"city": "rome"' in asked                                                              # the values already set are in it
    first = manager.events(sid)["events"][0]
    assert first["event"] == "user" and "Mood = calm" in first["text"] and "Topic = history" in first["text"] and "Stamp = 20" in first["text"]
    assert "Later" not in first["text"]                                                           # set in the script, later
    deadline = time.time() + 15
    while time.time() < deadline and not any(e["event"] == "done" for e in manager.events(sid)["events"]):
        time.sleep(0.05)
