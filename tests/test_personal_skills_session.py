"""Personal skills in a conversation: the script's calls go through the agent's gate and are kept in the conversation."""
import json
import threading
import time

import pytest

from harness import agent_loop as al
from test_session import Events, Scripted, make, say


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("harness.stack.local_now", None)


def skill(script, **over):
    return {"name": "Test skill", "display": "Skill “Test skill”: n = 3", "script": script,
            "values": {"n": {"type": "number", "value": 3}}, "timeout": 60, **over}


def done(ev, after=0, seconds=30):
    return ev.wait("done", after=after, timeout=seconds) if "timeout" in ev.wait.__code__.co_varnames else ev.wait("done", after=after)


def test_the_scripts_tool_calls_go_through_the_gate_and_stay_in_the_conversation(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted())
    script = '''
from basalt_skill import tool

def main(params):
    tool("write_file", path="out.txt", content="x" * params.n)
    return tool("read_file", path="out.txt")
'''
    assert session.run_skill("t1", skill(script)) is True
    end = done(ev)
    assert end["stop_reason"] == al.FINAL and end["final"].strip().endswith("xxx")
    assert (tmp_path / "ws" / "out.txt").read_text() == "xxx"
    kinds = ev.kinds()
    assert kinds.index("accepted") < kinds.index("started") < kinds.index("skill") < kinds.index("done")
    tools = [e["data"] for e in ev.items if e["event"] == "agent" and e["data"].get("type") in ("tool_start", "tool_end")]
    assert [(t["type"], t["name"]) for t in tools] == [("tool_start", "write_file"), ("tool_end", "write_file"), ("tool_start", "read_file"), ("tool_end", "read_file")]
    messages = built.agent.messages
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert messages[1]["content"] == "Skill “Test skill”: n = 3" and messages[2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert messages[-1]["content"].strip().endswith("xxx")
    audit = [json.loads(line) for line in (built.data_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [a["action"] for a in audit if a["action"].startswith("tool_")] == ["tool_executed", "tool_executed"]


def test_the_same_call_many_times_is_not_taken_for_a_loop(tmp_path):
    session, ev, *_ = make(tmp_path, Scripted())
    (tmp_path / "ws" / "a.txt").write_text("a", encoding="utf-8")
    script = 'from basalt_skill import tool\n\ndef main(params):\n    return [tool("read_file", path="a.txt") for _ in range(6)] and "ok"\n'
    session.run_skill("t1", skill(script))
    assert done(ev)["final"] == "ok"


def test_a_tool_that_is_refused_or_unknown_is_an_error_the_script_can_catch(tmp_path):
    session, ev, *_ = make(tmp_path, Scripted())
    script = '''
from basalt_skill import tool, ToolError

def main(params):
    try:
        tool("no_such_tool")
    except ToolError as e:
        return "caught: " + str(e)[:40]
'''
    session.run_skill("t1", skill(script))
    assert done(ev)["final"].startswith("caught: Unknown tool 'no_such_tool'")


def test_the_model_is_asked_once_and_the_answer_is_kept_for_the_same_prompt(tmp_path):
    client = Scripted(say("A SUMMARY"), say("SHOULD NOT BE ASKED"))
    session, ev, store, built = make(tmp_path, client)
    cache = tmp_path / "cache"
    script = 'from basalt_skill import ai\n\ndef main(params):\n    return ai("Summarise {what} in {n} words", what="the war", n=params.n)\n'
    session.run_skill("t1", skill(script, cache_dir=str(cache)))
    assert done(ev)["final"] == "A SUMMARY"
    sent = client.sent[0]
    assert sent[0]["role"] == "system" and "one step of a program" in sent[0]["content"] and sent[1]["content"] == "Summarise the war in 3 words"
    n = len(ev.items)
    session.run_skill("t2", skill(script, cache_dir=str(cache)))
    assert done(ev, after=ev.kinds().index("done") + 1)["final"] == "A SUMMARY"
    assert len(client.sent) == 1                                                  # the second run did not ask


def test_a_question_to_the_person_waits_for_the_button(tmp_path):
    session, ev, *_ = make(tmp_path, Scripted())
    script = 'from basalt_skill import ask\n\ndef main(params):\n    return "chose " + ask("Which?", ["first", "second"])\n'
    session.run_skill("t1", skill(script))
    question = ev.wait("ask")
    assert question["question"] == "Which?" and [o["value"] for o in question["options"]] == ["first", "second"] and question["kind"] == "skill_question"
    session.answer(question["id"], "second")
    assert done(ev)["final"] == "chose second"


def test_stop_ends_a_skill_that_is_running(tmp_path):
    session, ev, *_ = make(tmp_path, Scripted())
    script = "import time\n\ndef main(params):\n    time.sleep(60)\n"
    session.run_skill("t1", skill(script))
    ev.wait("skill")
    time.sleep(0.5)
    session.cancel()
    end = done(ev)
    assert end["stop_reason"] == al.CANCELLED and "stopped" in end["final"]


def test_a_second_skill_is_refused_while_one_runs_and_a_message_goes_on_afterwards(tmp_path):
    client = Scripted(say("hello again"))
    session, ev, store, built = make(tmp_path, client)
    slow = "import time\n\ndef main(params):\n    time.sleep(1.5)\n    return 'slept'\n"
    session.run_skill("t1", skill(slow))
    assert session.run_skill("t2", skill(slow)) is False
    assert ev.wait("error")["code"] == "busy"
    assert done(ev)["final"] == "slept"
    session.send("m1", "and now?")
    assert done(ev, after=ev.kinds().index("done") + 1)["final"] == "hello again"
    roles = [m["role"] for m in client.sent[0]]
    assert roles == ["system", "user", "assistant", "user"]                       # the model sees the skill's request and its answer


def test_an_error_in_the_script_is_told_as_the_answer(tmp_path):
    session, ev, *_ = make(tmp_path, Scripted())
    session.run_skill("t1", skill("def main(params):\n    return 1 / 0\n"))
    end = done(ev)
    assert end["stop_reason"] == al.FINAL and "stopped: ZeroDivisionError" in end["final"]
