"""The settings of the summary of older steps: kept and checked, handed to the conversation's process and the agent, reachable over HTTP,
worded in English and Italian; the summary call and /context and /compact in a real stack."""
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import commands
from harness.compaction import DEFAULT_THRESHOLD
from harness.session import Session, SessionStore, parse_args as session_args
from harness.stack import AgentConfig, build_agent, _summarizer
from serve.compaction_prefs import CompactionError, CompactionPrefs
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_session import Events, Scripted, say


def test_defaults_are_on_at_eighty_five_percent(tmp_path):
    prefs = CompactionPrefs(tmp_path / "c.json")
    assert prefs.read() == {"compaction": True, "compaction_threshold": DEFAULT_THRESHOLD}
    view = prefs.view()
    assert view["defaults"] == {"compaction": True, "compaction_threshold": 0.85} and view["limits"]["compaction_threshold"] == [0.5, 0.98]


def test_values_are_saved_checked_and_a_refused_value_changes_nothing(tmp_path):
    prefs = CompactionPrefs(tmp_path / "c.json")
    saved = prefs.write({"compaction": False, "compaction_threshold": 0.7})
    assert saved == {"compaction": False, "compaction_threshold": 0.7} and CompactionPrefs(tmp_path / "c.json").read() == saved
    for bad, field in (({"compaction": "yes"}, "compaction"), ({"compaction_threshold": 0.2}, "compaction_threshold"), ({"compaction_threshold": 1.0}, "compaction_threshold"),
                       ({"compaction_threshold": True}, "compaction_threshold"), ({"compaction_threshold": "high"}, "compaction_threshold")):
        with pytest.raises(CompactionError) as raised:
            prefs.write(bad)
        assert raised.value.field == field and str(raised.value)
    assert prefs.read() == saved
    (tmp_path / "c.json").write_text("{broken", encoding="utf-8")
    assert prefs.read() == {"compaction": True, "compaction_threshold": 0.85}


def test_the_flags_reach_the_session_process_arguments_and_the_agent_settings(tmp_path):
    prefs = CompactionPrefs(tmp_path / "c.json")
    args = session_args(prefs.flags())
    assert (args.no_compaction, args.compaction_threshold) == (False, 0.85)
    prefs.write({"compaction": False, "compaction_threshold": 0.6})
    args = session_args(prefs.flags())
    assert (args.no_compaction, args.compaction_threshold) == (True, 0.6)
    assert session_args([]).no_compaction is False


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    url = start_server([answer("x")])

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
    return call


def test_the_settings_page_reads_and_saves(api):
    code, view = api("GET", "/api/compaction")
    assert code == 200 and view["compaction"] is True and view["compaction_threshold"] == 0.85
    code, view = api("POST", "/api/compaction", {"compaction": False, "compaction_threshold": 0.9})
    assert code == 200 and view["compaction"] is False and view["compaction_threshold"] == 0.9
    assert api("GET", "/api/compaction")[1]["compaction_threshold"] == 0.9
    code, body = api("POST", "/api/compaction", {"compaction_threshold": 0.1})
    assert code == 400 and body["error"]["field"] == "compaction_threshold"
    assert api("GET", "/api/compaction")[1]["compaction_threshold"] == 0.9


def test_every_string_of_the_page_exists_in_english_and_italian_and_the_page_is_wired():
    web = Path(__file__).resolve().parents[1] / "serve" / "web"
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    source = (web / "compaction.js").read_text(encoding="utf-8")
    chat = (web / "agentchat.js").read_text(encoding="utf-8")
    slash = (web / "slash.js").read_text(encoding="utf-8")
    used = set(re.findall(r'T\("(cx\.[a-z.]+)"', source)) | set(re.findall(r'T\("(ch\.ev\.compaction[a-z_]*)"', chat)) | set(re.findall(r'T\("(sl\.r\.cx[a-z.]*)"', slash))
    used = {k for k in used if not k.endswith(".")} | {"sl.r.cx.by.model", "sl.r.cx.by.extract"}
    assert len(used) >= 14
    for key in used:
        assert i18n.count(f'"{key}":') == 2, key                                      # once in English and once in Italian
    html = (web / "index.html").read_text(encoding="utf-8")
    assert 'src="web/compaction.js"' in html and 'id="cx-root"' in html and 'data-t="cx.title"' in html
    assert "innerHTML" not in source                                                       # what the server says is put in as text


# -- in a real stack ----------------------------------------------------------------------------------------------------


class Asked:
    """What the summary function sent to the client."""

    def __init__(self):
        self.calls = []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.calls.append((messages, tools, kwargs))
        return {"choices": [{"message": {"role": "assistant", "content": "Goal: carry on\nOpen items: none"}}]}


def test_the_summary_call_is_a_separate_bounded_call_through_the_client_and_keeps_the_newest_text():
    client = Asked()
    write = _summarizer(client, 1500)
    big = "A" * 30000 + "MIDDLE" + "B" * 120000 + "THE-NEWEST-LINE"
    assert write("instructions", big) == "Goal: carry on\nOpen items: none"
    messages, tools, kwargs = client.calls[0]
    assert tools is None and kwargs["max_tokens"] == 1500 and kwargs["timeout"] > 60 and kwargs["temperature"] == 0.0
    sent = messages[1]["content"]
    assert len(sent) < 121_000 and sent.endswith("THE-NEWEST-LINE") and "left out" in sent and messages[0]["content"] == "instructions"
    assert _summarizer(None) is None


def make(tmp_path, client, **cfg):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, max_turns=6, **cfg), client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist"), events, session_id="s1", waits=(0, 0, 0), sleep=lambda s: None)
    session.endpoint = ""
    return session, events, built


def test_the_stack_builds_the_compaction_from_the_settings(tmp_path):
    _, _, built = make(tmp_path, Scripted(), compaction_threshold=0.7)
    comp = built.agent.context.compaction
    assert comp.enabled and comp.threshold == 0.7 and comp.summarize is not None
    _, _, off = make(tmp_path / "x", Scripted(), compaction=False)
    assert off.agent.context.compaction.enabled is False


def test_context_shows_the_compaction_and_compact_works_on_one_long_turn(tmp_path):
    session, ev, built = make(tmp_path, Scripted(*[say("ok")] * 3))
    agent = built.agent
    agent.messages += [{"role": "user", "content": "read everything"}]
    for i in range(14):
        agent.messages += [{"role": "assistant", "content": f"step {i}", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": f"a{i}.py"})}}]},
                           {"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": f"file {i} " + "word " * 300}]
    shown = commands.run("context", "", session)["data"]["compaction"]
    assert shown["enabled"] and shown["threshold"] == 0.85 and shown["count"] == 0 and shown["reports"] == []
    before = len(agent.messages)
    out = commands.run("compact", "", session)["data"]
    assert out["done"] and out["after"] < out["before"] and len(agent.messages) < before
    report = out["compaction"]
    assert report["reason"] == "manual" and report["prefix"]["system_stable"] and report["prefix"]["reread_tokens"] > 0
    assert agent.messages[0]["role"] == "system" and any(m.get("summary") for m in agent.messages)
    after = commands.run("context", "", session)["data"]["compaction"]
    assert after["count"] == 1 and after["reports"][0]["n"] == 1 and commands.run("context", "", session)["data"]["summarised"]


def test_the_process_of_a_conversation_is_started_with_the_settings_and_they_reach_the_agent(tmp_path):
    from harness.stack import agent_settings
    from serve.agent_sessions import SessionManager
    prefs = CompactionPrefs(tmp_path / "c.json")
    manager = SessionManager(tmp_path / "j", "http://127.0.0.1:1", compaction_flags=prefs.flags, command=None)
    rec = {"mode": "default", "workspace": str(tmp_path)}
    plain = manager._command("s1", rec, tmp_path / "h")
    assert plain[plain.index("--compaction-threshold") + 1] == "0.85" and "--no-compaction" not in plain
    prefs.write({"compaction": False, "compaction_threshold": 0.6})
    flags = prefs.flags()
    command = manager._command("s1", rec, tmp_path / "h")
    start = command.index(flags[0])
    assert command[start:start + len(flags)] == flags
    assert "--no-compaction" in manager._command("s2", {**rec, "ask_only": True}, tmp_path / "h")          # a chat without tools too
    settings = agent_settings(session_args(flags))
    assert settings["compaction"] is False and settings["compaction_threshold"] == 0.6
