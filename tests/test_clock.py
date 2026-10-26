"""The agent always knows the person's local date, time and timezone: a note before each request, and a time on each message."""
import copy
from datetime import datetime, timedelta, timezone

from harness.agent_loop import AgentLoop, local_time_line
from harness.context import wire_messages
from harness.stack import AgentConfig, build_agent
from harness.tools.registry import ToolRegistry

ROME = timezone(timedelta(hours=1), "CET")


class Said:
    def __init__(self, *texts):
        self.texts, self.sent = list(texts), []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        return {"choices": [{"message": {"role": "assistant", "content": self.texts.pop(0)}}]}


class Ticking:
    """A clock that is a minute later each time it is read."""

    def __init__(self):
        self.now = datetime(2026, 10, 26, 0, 52, tzinfo=ROME)

    def __call__(self):
        self.now += timedelta(minutes=1)
        return self.now


def test_the_line_says_weekday_date_time_and_the_zone():
    assert local_time_line(datetime(2026, 10, 26, 0, 52, tzinfo=ROME)) == "[harness] The person's local date and time: Monday 2026-10-26 00:52 (UTC+01:00, CET)."
    assert local_time_line(datetime(2026, 7, 4, 15, 5, tzinfo=timezone.utc)).endswith("(UTC+00:00, UTC).")
    assert "-05:30" in local_time_line(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-5, minutes=-30))))


def test_each_request_is_preceded_by_the_time_without_the_model_asking():
    client = Said("one", "two")
    agent = AgentLoop(client, ToolRegistry(), clock=Ticking())
    agent.run("what day is it?")
    agent.run("and now?")
    first, second = client.sent
    notes = [m for m in second if m["role"] == "user" and m["content"].startswith("[harness] The person's local date and time")]
    assert len(notes) == 2 and notes[0]["content"] != notes[1]["content"]              # one for each request, each with its own minute
    users = [m for m in first if m["role"] == "user"]
    assert users[0]["content"].startswith("[harness] The person's local date") and users[1]["content"] == "what day is it?"


def test_the_start_of_the_conversation_stays_as_it_was_when_the_next_request_comes():
    client = Said("one", "two")
    agent = AgentLoop(client, ToolRegistry(), clock=Ticking())
    agent.run("first")
    agent.run("second")
    before, after = client.sent
    assert after[:len(before)] == before                            # only appended to, so the server's cache of the start is kept


def test_what_the_server_receives_has_no_time_keys_and_the_conversation_keeps_them():
    client = Said("one")
    agent = AgentLoop(client, ToolRegistry(), clock=Ticking())
    agent.run("hello")
    assert all("ts" not in m and "note" not in m for m in client.sent[0])
    stamped = [m for m in agent.messages if m["role"] != "system"]
    assert stamped and all(isinstance(m["ts"], float) for m in stamped)
    assert [m["note"] for m in agent.messages if m.get("note")] == [True]


def test_without_a_clock_nothing_changes():
    client = Said("one")
    agent = AgentLoop(client, ToolRegistry())
    agent.run("hello")
    assert [m["role"] for m in client.sent[0]] == ["system", "user"]
    assert all("ts" not in m for m in agent.messages)


def test_a_fork_keeps_the_clock():
    client = Said("one")
    other = AgentLoop(client, ToolRegistry(), clock=Ticking()).fork()
    other.run("hello")
    assert any(m.get("note") for m in other.messages)


def test_a_built_agent_has_the_clock_and_its_prompt_says_to_use_the_line(tmp_path):
    (tmp_path / "w").mkdir()
    built = build_agent(AgentConfig(workspace=str(tmp_path / "w"), data_dir=str(tmp_path / "d"), use_checkpoints=False), client=Said("hi"))
    try:
        built.agent.run("hello")
        assert any(m.get("note") and "local date and time" in m["content"] for m in built.agent.messages)
        assert "do not ask for the time" in built.agent.messages[0]["content"]
        assert all("ts" not in m for m in wire_messages(built.agent.messages))
    finally:
        built.close()
