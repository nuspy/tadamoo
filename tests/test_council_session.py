"""The council from the conversation's side: the box of the conversation and /council debate a real question before the model's first step, as the
tool `ask_council` called for the person; the model then answers with the verdict read as untrusted text. Scripted models, no engine."""
import copy
import threading

import pytest

from tadamoo.council import CHAIR, clean_settings
from tadamoo.llm.credentials import FileCredentials
from tadamoo.llm.store import SecureRegistry
from tadamoo.session import Session, SessionStore, main as session_main, parse_args
from tadamoo.stack import AgentConfig, build_agent
from test_session import Events, Scripted, say


class Speaker:
    """A council member: it answers with its own function name, and the moderator ends the debate at once."""

    def __init__(self, name, log, on_call=None):
        self.name, self.log, self.on_call = name, log, on_call

    def chat_completion(self, messages, **kw):
        self.log.append(self.name)
        if self.on_call:
            self.on_call()
        if self.name == CHAIR:
            return {"choices": [{"message": {"content": "Agreement: use SQLite.\nRecommendation: SQLite.\nCONSENSUS: YES"}}]}
        return {"choices": [{"message": {"content": f"{self.name} says: SQLite is enough"}}]}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("tadamoo.stack.local_now", None)
    monkeypatch.setattr("tadamoo.stack.capstate.read", lambda agent, **kw: {})


@pytest.fixture
def world(tmp_path, monkeypatch):
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg._set_extra("council", clean_settings({"rounds": 1}))
    log = []
    hooks = {}
    monkeypatch.setattr("tadamoo.llm.registry", lambda path=None: reg)
    monkeypatch.setattr("tadamoo.llm.client_for", lambda function, local, *a, **k: Speaker(function, log, hooks.get("on_call")))
    return reg, log, hooks


def make(tmp_path, client, **cfg):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="default", max_turns=6,
                                    **cfg), client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None)
    from tadamoo.llm import client_for
    session.council_clients = lambda function: client_for(function, None)
    return session, events, built


def user_text(call):
    return [m["content"] for m in call if m["role"] == "user"]


def test_with_the_box_on_a_real_question_is_debated_before_the_model_answers_and_the_verdict_is_untrusted_data(tmp_path, world):
    reg, log, _ = world
    model = Scripted(say("Use SQLite."))
    session, ev, built = make(tmp_path, model, council=True)
    session.council_each = True
    session.send("t1", "Should the cache of this tool live in Redis or in SQLite?")
    done = ev.wait("done")
    assert done["final"] == "Use SQLite." and log == ["council:proposer", "council:critic", "council:engineer", CHAIR]
    assert len(model.sent) == 1                                                       # one call of the model: it read the verdict, it did not ask for it
    seen = copy.deepcopy(model.sent[0])
    tool = next(m for m in seen if m["role"] == "tool")
    assert tool["name"] == "ask_council" and "<untrusted_data" in tool["content"] and "Council: 3 advisers, 1 round(s), 4 model calls" in tool["content"]
    assert "Moderator's last summary" in tool["content"] and "round 1 critic" in tool["content"]
    kinds = [(e["data"]["type"], e["data"].get("name")) for e in ev.items if e["event"] == "agent" and e["data"].get("type", "").startswith("tool_")]
    assert ("tool_start", "ask_council") in kinds and ("tool_end", "ask_council") in kinds
    assert seen[-1]["role"] == "tool"                                                 # the debate is the last thing the model reads before it answers


def test_a_command_an_acknowledgement_and_a_card_answer_are_not_debated(tmp_path, world):
    reg, log, _ = world
    model = Scripted(say("Ok."), say("Prego."))
    session, ev, built = make(tmp_path, model, council=True)
    session.council_each = True
    session.send("t1", "ok")
    ev.wait("done")
    session.send("t2", "grazie mille")
    ev.wait("done", after=ev.kinds().index("done") + 1)
    assert log == [] and "ask_council" not in [m.get("name") for m in built.agent.messages]


def test_with_the_box_off_nothing_is_debated_and_turning_it_on_applies_from_the_next_message(tmp_path, world):
    reg, log, _ = world
    model = Scripted(say("Plain."), say("Debated."))
    session, ev, built = make(tmp_path, model, council=True)
    session.send("t1", "Should the cache of this tool live in Redis or in SQLite?")
    ev.wait("done")
    assert log == []
    session.set_council(True)
    session.send("t2", "And should the cache expire after one day or never?")
    ev.wait("done", after=ev.kinds().index("done") + 1)
    assert len(log) == 4


def test_slash_council_debates_the_question_once_without_the_command_in_the_message(tmp_path, world):
    reg, log, _ = world
    model = Scripted(say("Yes."), say("Fine."))
    session, ev, built = make(tmp_path, model, council=True)
    session.send("t1", "/council Is a cache needed at all?")
    ev.wait("done")
    assert len(log) == 4
    asked = next(m for m in built.agent.messages if m["role"] == "assistant" and m.get("tool_calls"))
    assert '"question": "Is a cache needed at all?"' in asked["tool_calls"][0]["function"]["arguments"]
    assert not any("/council" in str(m.get("content")) for m in built.agent.messages if m["role"] == "user")
    session.send("t2", "ok then")                                                     # not sticky: the next message goes straight to the model
    ev.wait("done", after=ev.kinds().index("done") + 1)
    assert len(log) == 4
    session.send("t3", "/council   ")
    assert ev.wait("council")["state"] == "usage" and len(log) == 4


def test_a_conversation_without_tools_gets_the_tool_for_the_debate_only(tmp_path, world):
    reg, log, _ = world
    model = Scripted(say("Use SQLite."))
    session, ev, built = make(tmp_path, model)                                       # as an ask-only conversation is built: no council tool of its own
    from tadamoo.tools.registry import ToolRegistry
    built.agent.tools = ToolRegistry()
    session.ask_only = True
    session.council_each = True
    session.send("t1", "Should the cache of this tool live in Redis or in SQLite?")
    ev.wait("done")
    assert len(log) == 4 and built.agent.tools.get("ask_council") is None
    assert any(m.get("name") == "ask_council" for m in model.sent[0] if m["role"] == "tool")


def test_stop_during_the_debate_ends_the_turn_without_a_model_answer(tmp_path, world):
    reg, log, hooks = world
    model = Scripted(say("never"))
    session, ev, built = make(tmp_path, model, council=True)
    hooks["on_call"] = lambda: built.agent.cancel.set() if len(log) == 1 else None
    session.council_each = True
    session.send("t1", "Should the cache of this tool live in Redis or in SQLite?")
    done = ev.wait("done")
    assert done["stop_reason"] == "cancelled" and model.sent == [] and len(log) == 1


def test_fewer_than_two_enabled_advisers_is_told_and_the_model_answers_alone(tmp_path, world):
    reg, log, _ = world
    reg._set_extra("council", clean_settings({"roles": [{"name": "one", "job": "j"}]}))
    model = Scripted(say("Alone."))
    session, ev, built = make(tmp_path, model, council=True)
    session.council_each = True
    session.send("t1", "Should the cache of this tool live in Redis or in SQLite?")
    assert ev.wait("done")["final"] == "Alone." and ev.wait("council")["state"] == "unavailable" and log == []


def test_the_flag_and_the_command_reach_the_process(tmp_path, world):
    assert parse_args(["--council-each"]).council_each is True and parse_args([]).council_each is False
    import io
    import json
    ws = tmp_path / "ws"
    ws.mkdir()
    out = io.StringIO()
    lines = "\n".join(json.dumps(c) for c in [{"cmd": "council", "on": True}, {"cmd": "council", "on": "yes"}, {"cmd": "close"}]) + "\n"
    session_main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--no-memory", "--no-checkpoints", "--no-planner"], stdin=io.StringIO(lines), stdout=out)
    events = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    assert {"event": "council", "on": True} in events and any(e.get("code") == "bad_command" for e in events)
