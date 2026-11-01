"""The "work until done" switch and the check of whether a request is met when the agent says it is done."""
import json

from test_agent_loop import ScriptedClient, call, make, say

from harness import completion, persistence, side_uses
from harness.permissions import PermissionLevel, PermissionManager


class Judge:
    def __init__(self, word):
        self.word = word
        self.calls = 0

    def stays_local(self):
        return True

    def chat_completion(self, messages, **kw):
        self.calls += 1
        return {"choices": [{"message": {"content": self.word}}]}


def test_the_choice_is_on_until_the_person_turns_it_off_and_is_kept_in_a_file(tmp_path):
    prefs = persistence.Prefs(tmp_path / "w.json")
    assert prefs.read() is True
    assert prefs.write(False) is False and persistence.Prefs(tmp_path / "w.json").read() is False
    (tmp_path / "w.json").write_text("{broken", encoding="utf-8")
    assert persistence.Prefs(tmp_path / "w.json").read() is True


def test_where_nothing_is_configured_it_is_off(tmp_path):
    persistence.configure(None)
    assert persistence.enabled() is False
    persistence.configure(tmp_path / "w.json")
    assert persistence.enabled() is True
    persistence.configure(None)


def test_an_answer_that_admits_it_did_not_finish_is_sent_back_and_an_honest_finish_is_not():
    check = completion.make_completion(lambda: True)
    assert "could not finish" in check("fix it", "Sorry, I was unable to complete the fix.", "")
    assert "could not finish" in check("sistemalo", "Non sono riuscito a farlo.", "")
    assert check("fix it", "Done: the tests pass.", "") == ""


def test_the_model_reader_sends_back_with_what_is_missing_and_a_yes_lets_the_answer_stand(monkeypatch):
    monkeypatch.setattr(completion, "side_extra_for", lambda *a, **k: {"cache_slot": 1})
    side_uses.save("completion_check", on=True)
    assert completion.make_completion(lambda: True, Judge("no: the README was not updated"))("update all", "I changed the code.", "3 tool calls").endswith("then answer again.")
    assert "the README was not updated" in completion.make_completion(lambda: True, Judge("no: the README was not updated"))("x", "y", "")
    assert completion.make_completion(lambda: True, Judge("yes"))("x", "y", "") == ""
    judge = Judge("no: something")
    assert completion.make_completion(lambda: False, judge)("x", "I could not do it", "") == "" and judge.calls == 0


def test_the_loop_goes_back_to_work_at_most_twice_per_request(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(say("I could not finish."), say("I could not finish, sorry."), say("I could not finish again."), say("unused"))
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    seen = []
    agent.completion = completion.make_completion(lambda: True)
    agent.on_event = seen.append
    agent.run("do the thing")
    assert len(client.steps) == 1                                           # three answers were asked for, the third stands
    assert sum("could not finish" in str(m.get("content")) and m.get("role") == "user" for m in client.sent[2]) == 2
    assert [e["round"] for e in seen if e.get("type") == "completion_check"] == [1, 2]
    assert agent.mission["completion_rounds"] == 2 and agent.mission["request"] == "do the thing"


def test_with_the_switch_off_the_first_answer_stands(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(say("I could not finish."), say("unused"))
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    agent.completion = completion.make_completion(lambda: False)
    agent.run("do the thing")
    assert len(client.steps) == 1


def test_the_line_about_the_work_counts_the_calls_and_the_last_command_and_does_not_claim_changes_it_cannot_see(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(call("write_file", {"path": "a.txt", "content": "x"}, "w"), call("shell_command", {"command": "python -c \"print(1)\""}, "s"), say("done"))
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    agent.run("write and run")
    line = agent._work_line()
    assert "2 tool calls (0 failed)" in line and "1 file writes or edits" in line and "1 commands run; the last one ended with exit code 0" in line
