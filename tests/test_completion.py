"""The "work until done" switch and the check of whether a request is met when the agent says it is done."""
import json

from test_agent_loop import ScriptedClient, call, make, say

from tadamoo import completion, persistence, side_uses
from tadamoo.permissions import PermissionLevel, PermissionManager


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


def test_a_file_given_to_the_person_counts_as_a_file_written_and_the_mission_card_names_commands_and_deliveries(tmp_path):
    from types import SimpleNamespace as NS
    ws = tmp_path / "ws"
    ws.mkdir()
    agent, _ = make(ws, ScriptedClient(say("unused")), permissions=PermissionManager(level=PermissionLevel.AUTO))
    agent.turns = [NS(tool_calls=[NS(decision="executed", tool_name="publish_output_file", is_error=False, output="ok", arguments={"path": "D:/docs/report.md"}),
                                  NS(decision="executed", tool_name="shell_command", is_error=False, output={"exit_code": 0}, arguments={"command": "curl -L -o a.pdf https://x.example/a.pdf"})])]
    assert "1 file writes or edits" in agent._work_line()
    card = agent._mission_note()
    assert "delivered so far: D:/docs/report.md" in card and "Commands run so far" in card and "curl -L -o a.pdf" in card


def test_a_message_that_is_not_the_persons_does_not_take_the_request_away_from_the_checks(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(say("Done."), say("unused"))
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    seen = []
    agent.completion = lambda request, answer, work: seen.append(request) or ""
    agent.add_user_message("write a story of 10000 words about Noah")
    agent.add_user_message("[planner] a plan lacks a date", origin="planner")
    assert agent.last_user_text == ""                                       # what the permission checks read: nothing in the note stands for the person
    agent.run(None)
    assert seen and seen[0].startswith("write a story of 10000 words")


def test_a_reader_that_finds_the_request_unclear_does_not_send_the_agent_back():
    class Reader:
        def stays_local(self):
            return True

        def chat_completion(self, messages, **kw):
            return {"choices": [{"message": {"content": 'no: The request "write the answer" is ambiguous and lacks a specific question'}}]}

    check = completion.make_completion(lambda: True, client=Reader())
    assert check("write the answer", "Here it is: rain, 6 degrees.", "") == ""
    assert completion.UNCLEAR.search("The request text is missing from the prompt")
    assert not completion.UNCLEAR.search("The second chapter was not written")


def test_a_short_follow_up_is_read_with_the_request_before_it(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(say("Rain."), say("It is 6 degrees."), say("unused"))
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    seen = []
    agent.completion = lambda request, answer, work: seen.append(request) or ""
    agent.run("what will the weather in Budapest be tomorrow")
    agent.run("write the answer")
    assert "earlier request" in seen[-1] and "weather in Budapest" in seen[-1]
    assert "judge the answer against the message above" in seen[-1]                    # the earlier request is context, not the thing to judge
    assert seen[0] == "what will the weather in Budapest be tomorrow"
