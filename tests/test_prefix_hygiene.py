"""What keeps the cached start of every request the same, and what makes a change to it visible: the order of the tool list, `present_plan`
in every mode, the size and hash of each part in /context, a project file read in part, and the session settings of the model call."""
import io
import json
import threading
import time

import pytest

from harness import commands
from harness.session import Session, SessionStore, main
from harness.skills import MAX_GUIDELINE_BYTES, find_project_guidelines, guidelines_cut_notice, read_project_guidelines
from harness.stack import AgentConfig, add_agent_arguments, agent_settings, build_agent
from harness.tools import create_standard_tool_registry
from harness.tools.registry import EXTERNAL, SESSION, STABLE, EffectClass, ToolRegistry
from test_commands import Feed, make
from test_session import Events, Scripted, say


def names(registry):
    return [s["function"]["name"] for s in registry.schemas()]


def noop(**kwargs):
    return "ok"


# -- the order of the tool list ----------------------------------------------------------------------------------------

def test_tools_are_listed_stable_first_then_the_setup_s_own_then_outside_servers_each_by_name():
    r = ToolRegistry()
    r.register("zeta", "z", {"type": "object", "properties": {}}, EffectClass.PURE, noop)
    r.register("alpha", "a", {"type": "object", "properties": {}}, EffectClass.PURE, noop)
    r.mark_stable()
    r.register("mcp_b", "b", {"type": "object", "properties": {}}, EffectClass.PURE, noop, tier=EXTERNAL)
    r.register("memory_x", "m", {"type": "object", "properties": {}}, EffectClass.PURE, noop)
    r.register("mcp_a", "a", {"type": "object", "properties": {}}, EffectClass.PURE, noop, tier=EXTERNAL)
    r.register("browse", "b", {"type": "object", "properties": {}}, EffectClass.PURE, noop)
    assert names(r) == ["alpha", "zeta", "browse", "memory_x", "mcp_a", "mcp_b"]
    assert (r.get("alpha").tier, r.get("browse").tier, r.get("mcp_a").tier) == (STABLE, SESSION, EXTERNAL)


def test_a_registry_that_was_never_marked_lists_by_name_as_before():
    r = ToolRegistry()
    for n in ("b", "c", "a"):
        r.register(n, n, {"type": "object", "properties": {}}, EffectClass.PURE, noop)
    assert names(r) == ["a", "b", "c"]


def test_the_standard_tools_lead_and_a_tool_added_later_never_moves_them(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    registry, _ = create_standard_tool_registry(ws)
    before = names(registry)
    registry.register("aaa_first_by_name", "x", {"type": "object", "properties": {}}, EffectClass.PURE, noop)
    after = names(registry)
    assert after[:len(before)] == before and after[-1] == "aaa_first_by_name"


# -- present_plan in every mode ----------------------------------------------------------------------------------------

def test_the_tool_list_is_the_same_in_every_mode_and_present_plan_refuses_outside_plan_mode(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, permission_mode="default"), client=object())
    try:
        listed = json.dumps(built.agent.tools.schemas(), sort_keys=True)
        assert "present_plan" in names(built.agent.tools)
        assert "Plan mode is not on" in built.agent.tools.execute("present_plan", {"plan": "do x"})
        for mode in ("plan", "auto", "read_only", "default"):
            built.set_mode(mode)
            assert json.dumps(built.agent.tools.schemas(), sort_keys=True) == listed               # a mode change does not rewrite the prefix
    finally:
        built.close()


# -- /context shows the parts ------------------------------------------------------------------------------------------

def test_context_lists_the_size_and_hash_of_each_part_and_a_change_shows_in_the_hash(tmp_path):
    session, ev, built = make(tmp_path, Scripted(say("ok")))
    first = commands.run("context", "", session)["data"]
    by = {s["name"]: s for s in first["sections"]}
    assert {"system_core", "environment", "tools:stable", "tools:session"} <= set(by)
    assert by["system_core"]["bytes"] == len(built.builder.blocks["system_core"].encode("utf-8")) and len(by["system_core"]["sha"]) == 12
    assert by["tools:stable"]["count"] > 3 and first["prefix_bytes"] > by["system_core"]["bytes"] and len(first["prefix_sha"]) == 12
    again = commands.run("context", "", session)["data"]
    assert again["sections"] == first["sections"] and again["prefix_sha"] == first["prefix_sha"]       # nothing changed, nothing differs
    built.agent.tools.register("late_tool", "x", {"type": "object", "properties": {}}, EffectClass.PURE, noop)
    changed = commands.run("context", "", session)["data"]
    new = {s["name"]: s for s in changed["sections"]}
    assert new["tools:stable"] == by["tools:stable"] and new["tools:session"]["sha"] != by["tools:session"]["sha"]
    assert changed["prefix_sha"] != first["prefix_sha"]


# -- a project file read in part ---------------------------------------------------------------------------------------

def test_a_project_file_cut_at_the_limit_says_so_in_the_prompt_the_context_and_the_status(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    body = "- rule\n" * 6000
    (ws / "AGENTS.md").write_bytes(body.encode("utf-8"))
    cut = len(body.encode("utf-8")) - MAX_GUIDELINE_BYTES
    path, text, ignored = read_project_guidelines(ws)
    assert path.name == "AGENTS.md" and ignored == cut and len(text.encode("utf-8")) == MAX_GUIDELINE_BYTES
    assert find_project_guidelines(ws)[1] == text                                                # the old reading is unchanged
    notice = guidelines_cut_notice("AGENTS.md", cut)
    assert notice == f"[AGENTS.md was cut at 32 KiB; {cut} bytes ignored; read the file directly]"
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False), client=object())
    try:
        assert notice in built.agent.messages[0]["content"] and built.notices == [notice]
    finally:
        built.close()
    session, ev, built = make(tmp_path, Scripted(say("ok")))                    # a conversation in the same folder
    assert commands.run("context", "", session)["data"]["notices"] == [notice]
    assert commands.run("status", "", session)["data"]["notices"] == [notice]


def test_a_project_file_that_fits_has_no_notice(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("short rules", encoding="utf-8")
    assert read_project_guidelines(ws)[2] == 0
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False), client=object())
    try:
        assert built.notices == [] and "was cut" not in built.agent.messages[0]["content"] and "short rules" in built.agent.messages[0]["content"]
    finally:
        built.close()


# -- the settings of the model call ------------------------------------------------------------------------------------

def test_the_options_set_the_temperature_the_ceiling_and_the_blocks_and_default_to_none():
    import argparse
    parser = argparse.ArgumentParser()
    add_agent_arguments(parser)
    plain = agent_settings(parser.parse_args([]))
    assert plain["temperature"] is None and plain["max_tokens"] is None and plain["instruction_blocks"] is False
    given = agent_settings(parser.parse_args(["--temperature", "0.3", "--max-tokens", "2048", "--instruction-blocks"]))
    assert given["temperature"] == 0.3 and given["max_tokens"] == 2048 and given["instruction_blocks"] is True


def test_a_built_agent_forces_no_temperature_unless_the_config_sets_one(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    off = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d1"), use_memory=False), client=object())
    on = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d2"), use_memory=False, temperature=0.0, max_tokens=900, end_turn_bias=-1.0), client=object())
    try:
        assert off.agent.sampling == {} and off.agent.max_tokens is None
        assert on.agent.sampling == {"end_turn_bias": -1.0, "temperature": 0.0} and on.agent.max_tokens == 900
        assert on.agent.fork().max_tokens == 900 and on.agent.fork().sampling["temperature"] == 0.0
    finally:
        off.close()
        on.close()


def test_the_process_takes_the_temperature_and_the_ceiling_while_it_lives(tmp_path, monkeypatch):
    import harness.session as hs
    ws = tmp_path / "ws"
    ws.mkdir()
    real_build = hs.build_agent
    monkeypatch.setattr(hs, "build_agent", lambda cfg, client=None, summarizer=None, _c=Scripted(): real_build(cfg, client=_c))
    feed, out = Feed(), io.StringIO()
    t = threading.Thread(target=lambda: main(["--workspace", str(ws), "--history-dir", str(tmp_path / "h"), "--session-id", "t1", "--no-memory", "--no-checkpoints",
                                              "--heartbeat", "0.05"], stdin=feed, stdout=out), daemon=True)
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
    assert first["temperature"] is None and first["max_tokens"] is None
    feed.put({"cmd": "settings", "temperature": 0.2, "max_tokens": 3000})
    feed.put({"cmd": "settings", "temperature": -1})                               # refused
    feed.put({"cmd": "settings", "temperature": True})                             # refused
    feed.put({"cmd": "settings", "max_tokens": 0})                                 # refused
    second = status("k2")
    assert second["temperature"] == 0.2 and second["max_tokens"] == 3000
    feed.put({"cmd": "settings", "temperature": None, "max_tokens": None})
    third = status("k3")
    assert third["temperature"] is None and third["max_tokens"] is None
    feed.put({"cmd": "close"})
    t.join(10)
    assert seen(lambda e: e["event"] == "settings") == [{"event": "settings", "temperature": 0.2, "max_tokens": 3000},
                                                        {"event": "settings", "temperature": None, "max_tokens": None}]
    assert len(seen(lambda e: e.get("code") == "bad_command")) == 3
