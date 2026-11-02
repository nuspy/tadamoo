"""A message that changes the work leaves the rest of a batch of calls unrun, each recorded as such so the protocol stays whole."""
import json

from test_agent_loop import ScriptedClient, make, say

from harness.permissions import PermissionLevel, PermissionManager


def batch(*names):
    calls = [{"id": f"c{i}", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": n, "content": "x"})}} for i, n in enumerate(names)]
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": calls}}]}


def test_the_calls_after_a_changing_message_are_not_run_and_are_recorded(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(batch("a.txt", "b.txt", "c.txt"), say("ok"))
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    seen = []
    agent.batch_interrupt = lambda: "Not run: the person wrote something." if (ws / "a.txt").exists() else ""
    agent.on_event = seen.append
    agent.run("write three files")
    assert (ws / "a.txt").exists() and not (ws / "b.txt").exists() and not (ws / "c.txt").exists()
    tool_results = [m for m in client.sent[1] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["c0", "c1", "c2"] and tool_results[1]["content"].startswith("Not run")
    assert any(e.get("type") == "batch_interrupted" and e["skipped"] == 2 for e in seen)


def test_without_a_reason_the_whole_batch_runs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(batch("a.txt", "b.txt"), say("ok"))
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    agent.batch_interrupt = lambda: ""
    agent.run("write two files")
    assert (ws / "a.txt").exists() and (ws / "b.txt").exists()


def test_the_session_stops_a_batch_for_a_message_that_steers_and_not_for_one_that_waits(tmp_path):
    from test_session_tasks import Model, make as make_session
    session, _, _, _ = make_session(tmp_path, Model(), judge=lambda text, ctx: "steer" if "stop" in text else "queue")
    task = session._begin_main("t1", "do the work", "user", None, False)
    assert session._batch_stop(task) == ""                                                   # nothing was written
    session.inbox.append({"turn": "t2", "text": "write the report later, when you are done"})
    assert session._batch_stop(task) == ""
    session.inbox.clear()
    session.inbox.append({"turn": "t3", "text": "stop, use the other folder"})
    assert session._batch_stop(task).startswith("Not run")
    assert session.inbox[0]["peek"] == "steer"                                               # the answer is kept for when the message is routed
