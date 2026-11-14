"""Where the older steps are summarised, as each conversation's own inference settings: handed to its process and the agent, worded in English and
Italian; the summary call and /context and /compact in a real stack."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo import commands
from tadamoo.session import Session, SessionStore, parse_args as session_args
from tadamoo.stack import AgentConfig, agent_settings, build_agent, _summarizer
from serve import inference
from serve.agent_sessions import SessionManager
from test_session import Events, Scripted, say


def test_the_panel_settings_say_where_the_older_steps_are_folded():
    plain = inference.fields({})
    assert plain["compaction"] is True and plain["compaction_threshold"] == 0.8 and plain["context_threshold"] == 0.8 and plain["context_policy"] == "compact"
    custom = inference.fields({"policy": "compact", "threshold": "70"})
    assert custom["compaction"] is True and custom["compaction_threshold"] == 0.7 and custom["context_threshold"] == 0.7
    assert inference.fields({"policy": "rolling"})["compaction"] is False and inference.fields({"policy": "stop"})["compaction"] is False
    assert inference.fields({"policy": "compact", "threshold": "99"})["compaction_threshold"] == 0.98          # the harness never folds later than 98 per cent
    assert inference.fields({"policy": "compact", "threshold": "35"})["compaction_threshold"] == 0.5           # nor earlier than 50
    for bad in ({"policy": "never"}, {"threshold": "10"}, {"effort": "extreme"}, {"temperature": "9"}):
        with pytest.raises(ValueError):
            inference.normalize(bad)


def test_the_flags_of_a_conversation_reach_the_session_process_arguments(tmp_path):
    manager = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=None)
    rec = {"mode": "default", "workspace": str(tmp_path)}
    plain = manager._command("s1", rec, tmp_path / "h")
    assert plain[plain.index("--compaction-threshold") + 1] == "0.8" and "--no-compaction" not in plain      # a record with nothing says: on, at the default
    own = {**rec, **inference.fields({"policy": "compact", "threshold": "65", "top_p": "0.9", "stop": "END"}), "inference": {}}
    command = manager._command("s2", own, tmp_path / "h")
    assert command[command.index("--compaction-threshold") + 1] == "0.65" and "--no-compaction" not in command
    assert json.loads(command[command.index("--sampling") + 1]) == {"top_p": 0.9, "stop": ["END"]}
    off = manager._command("s3", {**rec, **inference.fields({"policy": "stop"})}, tmp_path / "h")
    assert "--no-compaction" in off and "--sampling" not in off
    assert "--no-compaction" in manager._command("s4", {**rec, "ask_only": True, **inference.fields({"policy": "rolling"})}, tmp_path / "h")     # a chat without tools too
    ns = session_args(["--no-compaction", "--compaction-threshold", "0.6", "--sampling", '{"top_k": 5}'])
    settings = agent_settings(ns)
    assert settings["compaction"] is False and settings["compaction_threshold"] == 0.6 and settings["sampling"] == {"top_k": 5}


def test_every_string_of_the_page_exists_in_english_and_italian():
    web = Path(__file__).resolve().parents[1] / "serve" / "web"
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    chat = (web / "agentchat.js").read_text(encoding="utf-8")
    slash = (web / "slash.js").read_text(encoding="utf-8")
    used = set(re.findall(r'T\("(ch\.ev\.compaction[a-z_]*)"', chat)) | set(re.findall(r'T\("(sl\.r\.cx[a-z.]*)"', slash))
    used = {k for k in used if not k.endswith(".")} | {"sl.r.cx.by.model", "sl.r.cx.by.extract"}
    assert len(used) >= 4
    for key in used:
        assert i18n.count(f'"{key}":') == 2, key                                      # once in English and once in Italian
    assert not (web / "compaction.js").exists() and 'id="cx-root"' not in (web / "index.html").read_text(encoding="utf-8")      # one place for it: the conversation's settings


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
    write = _summarizer(client)
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
        agent.messages += [{"role": "assistant", "content": f"step {i}", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": f"a{i}.txt"})}}]},
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


class Route:
    """A conversation client with a route: `local` is whether every route its function may take stays on this PC."""

    def __init__(self, local):
        self.local = local

    def stays_local(self):
        return self.local

    def chat_completion(self, messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "Goal: carry on\nOpen items: none"}}]}


def test_the_summary_goes_to_a_cloud_route_only_when_the_compaction_function_is_pointed_at_it(tmp_path):
    cloud = Route(False)
    _, _, without = make(tmp_path / "a", cloud)                                                         # the conversation is on a cloud route, the function is not
    assert without.agent.context.compaction.summarize is None
    _, _, own = make(tmp_path / "b", Route(True))
    assert own.agent.context.compaction.summarize is not None                                           # a route that stays on this PC may write it
    ws = tmp_path / "c" / "ws"
    ws.mkdir(parents=True)
    pointed = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "c" / "data"), use_memory=False, use_checkpoints=False), client=cloud, summarizer=cloud)
    assert pointed.agent.context.compaction.summarize is not None                                       # the person chose that route for the function
    plain = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "c" / "data2"), use_memory=False, use_checkpoints=False), client=Asked())
    assert plain.agent.context.compaction.summarize is not None                                         # a client with no routing is the loaded model


def test_with_the_real_registry_the_compaction_function_stays_on_this_pc_until_it_is_pointed_elsewhere(tmp_path):
    from tadamoo.llm.client import RoutedClient
    from tadamoo.llm.credentials import FileCredentials
    from tadamoo.llm.store import Ledger, SecureRegistry
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg.local_hosts = ()
    cloud = reg.add({"type": "openai_compat", "base_url": "https://api.example.com/v1", "api_key": "sk-test-abcdef", "model": "m"})["id"]
    reg.set_consent("executor", True)
    reg.assign_checked("executor", cloud)                                                        # the conversation itself is on a cloud route
    ledger = Ledger(tmp_path / "u.json")
    executor = RoutedClient("executor", reg, Asked(), ledger)
    compaction = RoutedClient("compaction", reg, Asked(), ledger)
    assert executor.stays_local() is False and compaction.stays_local() is True
    assert reg.fallback_chain("compaction") == [None]
    ok, why = reg.check("compaction", cloud)                                                     # and it cannot be pointed there without a yes for personal data
    assert ok is False and "personal data" in why
    reg.set_consent("compaction", True)
    reg.assign_checked("compaction", cloud)
    assert RoutedClient("compaction", reg, Asked(), ledger).stays_local() is False
