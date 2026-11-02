"""The limit of failed attempts: a request that has used them all is stopped as failed and the person is told; without a limit nothing changes."""
from test_agent_loop import ScriptedClient, call, make, say

from harness import agent_loop as al
from harness.bench import analysis
from harness.permissions import PermissionLevel, PermissionManager


def agent_for(ws, client, limit):
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    agent.max_failed_attempts = limit
    return agent


def misses(n):
    return [call("read_file", {"path": f"missing-{i}.txt"}, f"c{i}") for i in range(n)]


def test_a_request_that_has_used_all_its_attempts_is_stopped_as_failed_and_says_so(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(*misses(5), say("unused"))
    agent = agent_for(ws, client, 3)
    seen = []
    agent.on_event = seen.append
    agent.run("find the notes")
    assert agent.stop_reason == al.GAVE_UP and "3 failed attempts" in agent.error and "limit is 3" in agent.error
    assert len(client.steps) == 3                                     # the two calls that were never asked for are still scripted
    assert [e for e in seen if e.get("type") == "gave_up"] == [{"type": "gave_up", "attempts": 3, "limit": 3}]


def test_without_a_limit_failures_never_stop_the_request(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(*misses(6), say("Could not find them."))
    agent = agent_for(ws, client, 0)
    agent.run("find the notes")
    assert agent.stop_reason == al.FINAL and not client.steps


def test_a_call_that_worked_is_not_an_attempt_and_every_request_counts_again(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x", encoding="utf-8")
    client = ScriptedClient(*misses(2), call("read_file", {"path": "a.txt"}, "ok"), say("Done."), *misses(2), say("Done again."))
    agent = agent_for(ws, client, 3)
    agent.run("first")
    assert agent.stop_reason == al.FINAL and agent._failed_attempts == 2
    agent.run("second")
    assert agent.stop_reason == al.FINAL and agent._failed_attempts == 2


def test_a_fork_keeps_the_limit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert agent_for(ws, ScriptedClient(say("x")), 7).fork().max_failed_attempts == 7


def test_a_trial_stopped_for_it_has_its_own_class_in_the_analysis():
    assert "gave_up" in analysis.CLASSES and analysis.CLASSES["gave_up"][0] == "Too many failed attempts"
