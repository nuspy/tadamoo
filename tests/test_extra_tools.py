"""The extra tools in the registry: what is offered when, and the ACP and A2A tools end to end through the permission layer."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.permissions import PermissionLevel, PermissionManager
from harness.tools import create_standard_tool_registry
from harness.tools.registry import ToolArgumentError
from harness.tools.extra import register_extra_tools
from test_a2a import FakeAgent, _fast_polling  # noqa: F401  (fixture)
from test_agent_loop import ScriptedClient

FAKE_ACP = [sys.executable, str(Path(__file__).resolve().parent / "fake_acp_agent.py")]


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def agents_dir(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    return d


def write(directory, name, agents):
    (directory / name).write_text(json.dumps({"agents": agents}), encoding="utf-8")


def names(registry):
    return set(registry.names())


def test_run_code_and_subagents_are_offered_by_default_and_nothing_else(ws, agents_dir):
    registry, sandbox = create_standard_tool_registry(ws)
    added = register_extra_tools(registry, {"external_agents_dir": str(agents_dir)}, workspace=ws, sandbox=sandbox, client=ScriptedClient())
    assert added == ["run_code", "spawn_subagent"]
    assert {"run_code", "spawn_subagent"} <= names(registry)
    assert "delegate_to_acp_agent" not in names(registry) and "ask_a2a_agent" not in names(registry)


def test_the_configuration_can_narrow_the_list_and_a_missing_client_means_no_subagents(ws, agents_dir):
    registry, sandbox = create_standard_tool_registry(ws)
    assert register_extra_tools(registry, {"extra_tools": ["run_code"], "external_agents_dir": str(agents_dir)}, workspace=ws, client=ScriptedClient()) == ["run_code"]
    other, _ = create_standard_tool_registry(ws)
    assert register_extra_tools(other, {"extra_tools": [], "external_agents_dir": str(agents_dir)}, workspace=ws, client=ScriptedClient()) == []
    third, _ = create_standard_tool_registry(ws)
    assert register_extra_tools(third, {"external_agents_dir": str(agents_dir)}, workspace=ws) == ["run_code"]


def test_the_agent_files_decide_whether_the_acp_and_a2a_tools_exist(ws, agents_dir):
    write(agents_dir, "acp_agents.json", {"fake": {"command": FAKE_ACP, "description": "a test agent"}, "broken": {"description": "no command"}})
    write(agents_dir, "a2a_agents.json", {"remote": {"url": "http://127.0.0.1:1"}})
    registry, _ = create_standard_tool_registry(ws)
    added = register_extra_tools(registry, {"external_agents_dir": str(agents_dir)}, workspace=ws)
    assert "delegate_to_acp_agent" in added and "ask_a2a_agent" in added
    acp = registry.get("delegate_to_acp_agent")
    assert acp.parameters["properties"]["agent"]["enum"] == ["fake"] and "a test agent" in acp.description
    assert acp.untrusted and acp.effect.value == "irreversible" and registry.get("ask_a2a_agent").untrusted
    (agents_dir / "acp_agents.json").write_text("{not json", encoding="utf-8")
    again, _ = create_standard_tool_registry(ws)
    assert "delegate_to_acp_agent" not in register_extra_tools(again, {"external_agents_dir": str(agents_dir)}, workspace=ws)


def acp_registry(ws, agents_dir, permissions):
    write(agents_dir, "acp_agents.json", {"fake": {"command": FAKE_ACP, "env": ["PASS_ME"]}})
    registry, sandbox = create_standard_tool_registry(ws)
    register_extra_tools(registry, {"external_agents_dir": str(agents_dir)}, workspace=ws, sandbox=sandbox, permissions=permissions)
    return registry


def test_the_acp_agents_permission_request_goes_through_the_permission_manager(ws, agents_dir):
    asked = []
    perms = PermissionManager(PermissionLevel.DEFAULT, confirm_callback=lambda name, args: asked.append((name, args)) or True)
    out = acp_registry(ws, agents_dir, perms).execute("delegate_to_acp_agent", {"agent": "fake", "task": "permission"})
    assert out["text"] == "outcome=yes" and asked[0][0] == "acp_permission:fake:Run rm -rf build"
    refusing = PermissionManager(PermissionLevel.DEFAULT, confirm_callback=lambda n, a: False)
    assert acp_registry(ws, agents_dir, refusing).execute("delegate_to_acp_agent", {"agent": "fake", "task": "permission"})["text"] == "outcome=no"
    plan = PermissionManager(PermissionLevel.AUTO, plan_mode=True)
    assert acp_registry(ws, agents_dir, plan).execute("delegate_to_acp_agent", {"agent": "fake", "task": "permission"})["text"] == "outcome=no"
    nobody = acp_registry(ws, agents_dir, None)
    assert nobody.execute("delegate_to_acp_agent", {"agent": "fake", "task": "permission"})["text"] == "outcome=no"


def test_the_acp_agent_sees_only_the_variables_listed_for_it(ws, agents_dir, monkeypatch):
    monkeypatch.setenv("PASS_ME", "passed")
    monkeypatch.setenv("NOT_ME", "leaked")
    out = acp_registry(ws, agents_dir, None).execute("delegate_to_acp_agent", {"agent": "fake", "task": "env"})
    assert out["text"] == "passed,none"


def test_acp_failures_come_back_as_text_not_exceptions(ws, agents_dir):
    registry = acp_registry(ws, agents_dir, None)
    assert "failed" in registry.execute("delegate_to_acp_agent", {"agent": "fake", "task": "crash"})
    with pytest.raises(ToolArgumentError):                          # an agent that is not in the file cannot even be named
        registry.execute("delegate_to_acp_agent", {"agent": "nope", "task": "x"})
    assert "Give the task" in registry.execute("delegate_to_acp_agent", {"agent": "fake", "task": " "})


def test_the_a2a_tool_asks_the_listed_agent_with_the_token_from_the_environment(ws, agents_dir, monkeypatch):
    fake = FakeAgent()
    try:
        monkeypatch.setenv("REMOTE_TOKEN", "tok")
        write(agents_dir, "a2a_agents.json", {"remote": {"url": fake.url, "token_env": "REMOTE_TOKEN"}})
        registry, _ = create_standard_tool_registry(ws)
        register_extra_tools(registry, {"external_agents_dir": str(agents_dir)}, workspace=ws)
        out = registry.execute("ask_a2a_agent", {"agent": "remote", "message": "hello"})
        assert out["text"] == "The answer is 42." and out["state"] == "TASK_STATE_COMPLETED"
        assert fake.requests[-1][2]["Authorization"] == "Bearer tok"
        write(agents_dir, "a2a_agents.json", {"remote": {"url": "http://127.0.0.1:9"}})
        other, _ = create_standard_tool_registry(ws)
        register_extra_tools(other, {"external_agents_dir": str(agents_dir)}, workspace=ws)
        assert "failed" in other.execute("ask_a2a_agent", {"agent": "remote", "message": "hello"})
    finally:
        fake.server.shutdown()


def test_a_built_agent_has_the_extra_tools_and_a_model_can_run_code_through_the_permission_layer(ws, tmp_path):
    from harness.stack import AgentConfig, build_agent
    from test_agent_loop import call, say
    client = ScriptedClient(call("run_code", {"language": "python", "code": "print(6 * 7)"}), say("It printed 42."))
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), permission_mode="auto"), client=client)
    try:
        assert {"run_code", "spawn_subagent"} <= set(built.agent.tools.names())
        built.agent.run("what is 6 * 7?")
        tool_message = next(m for m in client.sent[1] if m["role"] == "tool")
        assert "42" in tool_message["content"]
    finally:
        built.close()
    denied = ScriptedClient(call("run_code", {"language": "python", "code": "print(1)"}), say("not allowed"))
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data2"), permission_mode="default"), client=denied)
    try:
        built.agent.run("run it")
        assert "Permission denied" in next(m for m in denied.sent[1] if m["role"] == "tool")["content"]   # nobody could confirm
    finally:
        built.close()
