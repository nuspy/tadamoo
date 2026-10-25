"""The slash commands that ask a conversation's process something, and the settings the person changes while it lives."""
import io
import json
import queue
import threading
import time

from harness import commands
from harness.session import Session, SessionStore, main
from harness.stack import AgentConfig, build_agent
from test_session import Events, Scripted, say


def make(tmp_path, client, mode="default", memory=False):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=memory, use_checkpoints=False,
                                    permission_mode=mode, max_turns=6), client=client)
    events = Events()
    store = SessionStore(tmp_path / "hist")
    session = Session(built, store, events, session_id="s1", waits=(0, 0, 0), sleep=lambda s: None)
    session.endpoint = ""                                  # no server to ask for the window: the default is used
    return session, events, built


def test_status_says_what_the_conversation_is_set_to(tmp_path):
    session, ev, built = make(tmp_path, Scripted(say("Ciao.")))
    session.send("m1", "ciao")
    ev.wait("done")
    out = commands.run("status", "", session)
    assert out["ok"] and out["data"]["mode"] == "default" and out["data"]["turns"] == 1 and out["data"]["effort"] == ""
    assert out["data"]["tools"] > 3 and out["data"]["messages"] >= 2 and 0 < out["data"]["share"] < 1
    session.set_effort("low")
    assert commands.run("status", "", session)["data"]["effort"] == "low"
    session.set_effort("")
    assert commands.run("status", "", session)["data"]["effort"] == ""


def test_the_thinking_level_reaches_the_model_calls_of_every_task(tmp_path):
    seen = []

    class Spy(Scripted):
        def chat_completion(self, messages, tools=None, **kwargs):
            seen.append(kwargs.get("extra"))
            return super().chat_completion(messages, tools, **kwargs)

    session, ev, built = make(tmp_path, Spy(say("uno"), say("due")))
    session.set_effort("medium")
    session.send("m1", "a")
    ev.wait("done")
    session.set_effort("")
    session.send("m2", "b")
    ev.wait("done", after=ev.kinds().index("done") + 1)
    assert seen[0] == {"reasoning_effort": "medium"} and not seen[1]
    session.set_effort("low")
    assert session.agent.fork().sampling == {"reasoning_effort": "low"}          # a side task starts with the setting


def test_the_lists_are_data_and_an_unknown_name_is_an_error(tmp_path):
    session, ev, built = make(tmp_path, Scripted(), mode="plan", memory=True)
    session.built.permissions.add_rule("shell_command:pytest *", "allow")
    assert commands.run("permissions", "", session)["data"] == {"mode": "plan", "rules": [{"pattern": "shell_command:pytest *", "action": "allow"}], "denied": 0, "untrusted_input": False}
    names = [t["name"] for t in commands.run("tools", "", session)["data"]["tools"]]
    assert "read_file" in names and names == sorted(names)
    assert commands.run("hooks", "", session)["data"]["pre_tool"] >= 1
    assert commands.run("mcp", "", session)["data"] == {"servers": []}
    built.memory.add_memory("the build needs python 3.12", "project")
    mem = commands.run("memory", "", session)["data"]
    assert mem["count"] == 1 and mem["items"][0]["content"] == "the build needs python 3.12"
    assert commands.run("memory", "python", session)["data"]["items"][0]["category"] == "project"
    assert commands.run("memory", "zebra", session)["data"]["items"] == []
    assert commands.run("rm", "", session) == {"ok": False, "error": "unknown command: rm"}


def test_compact_shortens_the_conversation_when_nothing_is_under_way(tmp_path):
    turns = [say(f"answer {i} " + "word " * 120) for i in range(6)]
    session, ev, built = make(tmp_path, Scripted(*turns))
    assert commands.run("compact", "", session)["data"] == {"done": False, "reason": "short", "tokens": built.agent.context.estimate_tokens(built.agent.messages)}
    for i in range(6):
        n = len(ev.items)
        session.send(f"m{i}", f"question {i} " + "text " * 100)
        ev.wait("done", after=n)
    before = len(built.agent.messages)
    out = commands.run("compact", "", session)["data"]
    assert out["done"] and out["after"] < out["before"] and len(built.agent.messages) < before
    assert any(m.get("summary") for m in built.agent.messages) and built.agent.messages[0]["role"] == "system"
    assert json.loads((tmp_path / "hist" / "history.json").read_text(encoding="utf-8"))["messages"][0].get("summary")
    assert commands.run("context", "", session)["data"]["summarised"]


class Feed:
    def __init__(self):
        self.q = queue.Queue()

    def put(self, obj):
        self.q.put(json.dumps(obj) + "\n")

    def __iter__(self):
        return self

    def __next__(self):
        line = self.q.get(timeout=20)
        if line is None:
            raise StopIteration
        return line


def test_the_process_takes_a_mode_a_level_and_a_command_while_it_lives(tmp_path, monkeypatch):
    import harness.session as hs
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, _c=Scripted(): real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "p1", "--no-memory", "--no-checkpoints",
                                              "--effort", "low", "--heartbeat", "0.05"], stdin=feed, stdout=out), daemon=True)
    t.start()
    feed.put({"cmd": "mode", "mode": "plan"})
    feed.put({"cmd": "mode", "mode": "everything"})                  # refused: not one of the modes
    feed.put({"cmd": "effort", "level": "high"})
    feed.put({"cmd": "effort", "level": "maximum"})                  # refused
    feed.put({"cmd": "command", "id": "k1", "name": "status", "args": ""})
    feed.put({"cmd": "command", "id": "k2", "name": "nothing", "args": ""})

    def seen(pred):
        return [e for e in (json.loads(l) for l in out.getvalue().splitlines()) if pred(e)]
    deadline = time.monotonic() + 10
    while len(seen(lambda e: e["event"] == "command")) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    feed.put({"cmd": "close"})
    t.join(10)
    assert seen(lambda e: e["event"] == "mode") == [{"event": "mode", "mode": "plan"}]
    assert seen(lambda e: e["event"] == "effort") == [{"event": "effort", "level": "high"}]
    status = seen(lambda e: e["event"] == "command" and e["id"] == "k1")[0]
    assert status["ok"] and status["data"]["mode"] == "plan" and status["data"]["effort"] == "high"
    assert seen(lambda e: e["event"] == "command" and e["id"] == "k2")[0]["ok"] is False
    assert len(seen(lambda e: e.get("code") == "bad_command")) == 2
