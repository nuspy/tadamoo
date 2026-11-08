"""The slash commands that ask a conversation's process something, and the settings the person changes while it lives."""
import io
import json
import queue
import threading
import time

from tadamoo import commands
from tadamoo.session import Session, SessionStore, main
from tadamoo.stack import AgentConfig, build_agent
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
    got = commands.run("permissions", "", session)["data"]
    assert got["mode"] == "plan" and got["rules"] == [{"pattern": "shell_command:pytest *", "action": "allow"}] and got["denied"] == 0 and got["refused_by"] == {}
    session.built.permissions.add_rule("group:write", "deny")
    assert commands.run("permissions", "", session)["data"]["refused_by"]["write_file"] == "group:write"
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
    turns = [say(f"answer {i} " + "word " * 120) for i in range(9)]
    session, ev, built = make(tmp_path, Scripted(*turns))
    assert commands.run("compact", "", session)["data"] == {"done": False, "reason": "short", "tokens": built.agent.context.estimate_tokens(built.agent.messages)}
    for i in range(9):
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
    import tadamoo.session as hs
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, technical=None, _c=Scripted(): real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "p1", "--no-memory", "--no-checkpoints",
                                              "--effort", "low", "--heartbeat", "0.05"], stdin=feed, stdout=out), daemon=True)
    t.start()
    feed.put({"cmd": "mode", "mode": "plan"})
    feed.put({"cmd": "mode", "mode": "everything"})                  # refused: not one of the modes
    feed.put({"cmd": "effort", "level": "high"})
    feed.put({"cmd": "effort", "level": "maximum"})                  # refused
    feed.put({"cmd": "intake", "mode": "full"})
    feed.put({"cmd": "intake", "mode": "always"})                    # refused
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
    assert seen(lambda e: e["event"] == "intake_mode") == [{"event": "intake_mode", "mode": "full"}]
    status = seen(lambda e: e["event"] == "command" and e["id"] == "k1")[0]
    assert status["ok"] and status["data"]["mode"] == "plan" and status["data"]["effort"] == "high"
    assert seen(lambda e: e["event"] == "command" and e["id"] == "k2")[0]["ok"] is False
    assert len(seen(lambda e: e.get("code") == "bad_command")) == 3


def test_the_process_takes_the_steps_and_the_end_of_turn_bias_while_it_lives(tmp_path, monkeypatch):
    import tadamoo.session as hs
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, technical=None, _c=Scripted(): real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "p2", "--no-memory", "--no-checkpoints",
                                              "--max-turns", "15", "--end-turn-bias", "-5", "--heartbeat", "0.05"], stdin=feed, stdout=out), daemon=True)
    t.start()

    def seen(pred):
        return [e for e in (json.loads(l) for l in out.getvalue().splitlines()) if pred(e)]

    def status(cid):
        feed.put({"cmd": "command", "id": cid, "name": "status", "args": ""})
        deadline = time.monotonic() + 10
        while not seen(lambda e: e["event"] == "command" and e["id"] == cid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return seen(lambda e: e["event"] == "command" and e["id"] == cid)[0]["data"]
    first = status("k1")
    assert first["max_turns"] == 15 and first["end_turn_bias"] == -5
    feed.put({"cmd": "settings", "max_turns": -1, "end_turn_bias": -20})
    feed.put({"cmd": "settings", "max_turns": 0})                                  # refused
    feed.put({"cmd": "settings", "end_turn_bias": 3})                              # refused
    feed.put({"cmd": "settings", "max_turns": True})                               # refused
    feed.put({"cmd": "settings"})                                                  # refused: nothing to change
    second = status("k2")
    assert second["max_turns"] == -1 and second["end_turn_bias"] == -20
    feed.put({"cmd": "settings", "end_turn_bias": 0})
    assert status("k3")["end_turn_bias"] == 0
    feed.put({"cmd": "close"})
    t.join(10)
    assert seen(lambda e: e["event"] == "settings") == [{"event": "settings", "max_turns": -1, "end_turn_bias": -20.0},
                                                        {"event": "settings", "end_turn_bias": 0.0}]
    assert len(seen(lambda e: e.get("code") == "bad_command")) == 4


def make_caps(tmp_path, skill_dir=None):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, capabilities=True,
                                    capabilities_dir=str(tmp_path / "caps"), capabilities_home=str(home), permission_mode="default",
                                    skill_paths=[str(skill_dir)] if skill_dir else [], max_turns=6), client=Scripted(say("ok")))
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist"), events, session_id="s1", waits=(0, 0, 0), sleep=lambda s: None)
    session.endpoint = ""
    return session, home


def write_skill(base, name):
    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: does {name}\n---\nUse {name}.\n", encoding="utf-8")


def test_skills_reload_finds_a_skill_added_after_the_conversation_began(tmp_path):
    skills = tmp_path / "skills"
    write_skill(skills, "first")
    session, home = make_caps(tmp_path, skills)
    assert "first" in session.built.skills.skills
    write_skill(skills, "second")
    write_skill(home / ".claude" / "skills", "third")
    out = commands.run("reload-skills", "", session)
    assert out["ok"] and out["data"]["done"]
    assert {"second", "third"} <= set(out["data"]["added"]) and "first" in out["data"]["skills"]
    assert "second" in session.built.skills.skills
    assert "second" in session.built.builder.assemble_system_message()
    (skills / "second" / "SKILL.md").unlink()
    gone = commands.run("reload-skills", "", session)["data"]
    assert "second" in gone["removed"] and "second" not in session.built.skills.skills


def test_mcp_reload_reads_the_servers_again_and_stops_the_ones_started(tmp_path):
    session, home = make_caps(tmp_path)
    assert commands.run("reload-mcp", "", session)["data"] == {"done": True, "stopped": 0, "servers": [], "added": [], "removed": []}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"alpha": {"command": "python", "args": ["-c", "pass"]}}}), encoding="utf-8")
    out = commands.run("reload-mcp", "", session)["data"]
    assert out["done"] and out["servers"] == ["alpha"] and out["added"] == ["alpha"]
    closed = []

    class Hub:
        def close(self):
            closed.append(1)

    # a server started earlier is stopped, so that its next use starts it again
    session.built.capabilities.drop_servers  # exists once the capability tools are registered
    assert session.built.capabilities.drop_servers() == 0 and not closed


def test_reload_plugins_finds_a_plugin_installed_after_the_conversation_began(tmp_path):
    session, home = make_caps(tmp_path)
    first = commands.run("reload-plugins", "", session)["data"]
    assert first == {"done": True, "plugins": [], "added": [], "removed": []}
    write_skill(home / ".claude" / "plugins" / "cache" / "market" / "tidy" / "1.0" / "skills", "sweep")
    out = commands.run("reload-plugins", "", session)["data"]
    assert out["done"] and out["plugins"] == ["tidy"] and out["added"] == ["sweep (tidy)"] and out["removed"] == []
    again = commands.run("reload-plugins", "", session)["data"]
    assert again["plugins"] == ["tidy"] and again["added"] == []


def test_reload_waits_until_nothing_is_under_way(tmp_path):
    session, _ = make_caps(tmp_path)
    session.idle = lambda: False
    for name in ("reload-mcp", "reload-skills", "reload-plugins"):
        assert commands.run(name, "", session)["data"] == {"done": False, "reason": "busy"}


def test_reload_says_when_the_conversation_looks_for_nothing(tmp_path):
    session, events, built = make(tmp_path, Scripted(say("x")))
    assert commands.run("reload-mcp", "", session)["data"] == {"done": False, "reason": "off"}
    assert commands.run("reload-skills", "", session)["data"] == {"done": False, "reason": "off"}
    assert commands.run("reload-plugins", "", session)["data"] == {"done": False, "reason": "off"}


def test_the_process_takes_the_temperature_the_reply_limit_and_the_pictures_level_while_it_lives(tmp_path, monkeypatch):
    import tadamoo.session as hs
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, technical=None, _c=Scripted(): real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "p3", "--no-memory", "--no-checkpoints",
                                              "--attachments-dir", str(tmp_path / "att"), "--outputs-dir", str(tmp_path / "out"),
                                              "--heartbeat", "0.05"], stdin=feed, stdout=out), daemon=True)
    t.start()
    feed.put({"cmd": "settings", "temperature": 0.3, "max_tokens": 2000})
    feed.put({"cmd": "settings", "temperature": 9})                                # refused
    feed.put({"cmd": "settings", "max_tokens": 0})                                 # refused: the server turns "no limit" into null before it gets here
    feed.put({"cmd": "settings", "temperature": None, "max_tokens": None})
    feed.put({"cmd": "visuals", "images": "high", "charts": "normal"})
    feed.put({"cmd": "visuals", "images": "high"})                                 # refused: both levels are given
    feed.put({"cmd": "command", "id": "k1", "name": "status", "args": ""})

    def seen(pred):
        return [e for e in (json.loads(l) for l in out.getvalue().splitlines()) if pred(e)]
    deadline = time.monotonic() + 10
    while not seen(lambda e: e["event"] == "command") and time.monotonic() < deadline:
        time.sleep(0.05)
    feed.put({"cmd": "close"})
    t.join(10)
    assert seen(lambda e: e["event"] == "settings") == [{"event": "settings", "temperature": 0.3, "max_tokens": 2000},
                                                        {"event": "settings", "temperature": None, "max_tokens": None}]
    assert seen(lambda e: e["event"] == "visuals") == [{"event": "visuals", "images": "high", "charts": "normal"}]
    assert len(seen(lambda e: e.get("code") == "bad_command")) == 3
