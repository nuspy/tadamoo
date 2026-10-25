"""The command line, the Chat's process and the editor's agent are built the same way and take the same options."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import agent_loop as al
from harness.headless import config_from_args, parse_args, run_headless
from harness.session import parse_args as session_args
from harness.stack import AgentConfig, agent_settings, build_agent
from harness.tools.registry import ToolRegistry
from test_agent_loop import ScriptedClient, call, say

SHARED = ["--workspace", "W", "--permission-mode", "auto", "--allow", "shell_command:pytest*", "--deny", "shell_command:rm*",
          "--secret-env", "TOKEN=example.com", "--skills", "S", "--payments-dir", "P", "--browser", "--browser-headless", "--browser-private",
          "--browser-profile", "B", "--vault-dir", "V", "--capabilities", "--lsp", "--wiki", "--wiki-project", "proj", "--phone",
          "--on-behalf-of", "Mario", "--context-patterns", '{"summary": {"on": false}}', "--context-threshold", "0.5", "--end-turn-bias", "0.2",
          "--attachments-dir", "A", "--outputs-dir", "O", "--hooks", "H.json", "--workspace-hooks", "--after-plan", "auto",
          "--tool-timeout", "40", "--tool-timeout", "shell_command=0", "--no-memory", "--no-checkpoints", "--max-turns", "7"]


def test_headless_and_the_chat_process_set_the_same_agent_from_the_same_options():
    from_cli = agent_settings(parse_args(["-p", "task", *SHARED]))
    from_chat = agent_settings(session_args(SHARED))
    assert from_cli == from_chat
    for key, value in [("browser", True), ("vault_dir", "V"), ("payments_dir", "P"), ("capabilities", True), ("lsp", True), ("wiki", True),
                       ("phone", True), ("council", False), ("hooks_file", "H.json"), ("workspace_hooks", True), ("after_plan", "auto"),
                       ("tool_timeout", 40.0), ("tool_timeouts", {"shell_command": 0.0}), ("use_memory", False), ("max_turns", 7),
                       ("secret_hosts", {"TOKEN": ["example.com"]}), ("browser_profile", "B")]:
        assert from_cli[key] == value, key
    cfg = config_from_args(parse_args(["-p", "task", *SHARED]))
    assert isinstance(cfg, AgentConfig) and cfg.vault_dir == "V" and cfg.lsp and cfg.capabilities and cfg.browser


def test_the_default_number_of_turns_stays_as_each_way_had_it():
    assert parse_args(["-p", "x"]).max_turns == 10 and session_args([]).max_turns == 15


def test_a_bad_tool_timeout_is_a_usage_error():
    with pytest.raises(SystemExit):
        agent_settings(parse_args(["-p", "x", "--tool-timeout", "soon"]))


def test_the_timeouts_set_on_the_command_line_reach_the_registry(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, tool_timeout=30, tool_timeouts={"read_file": 5, "shell_command": 0})
    built = build_agent(cfg, client=object())
    try:
        reg = built.agent.tools
        assert reg.default_timeout == 30 and reg.timeout_for("read_file") == 5 and reg.timeout_for("shell_command") is None
        assert reg.timeout_for("write_file") == 30
    finally:
        built.close()


def test_a_headless_run_finds_skills_and_servers_the_way_the_chat_does(tmp_path):
    ws, home = tmp_path / "ws", tmp_path / "home"
    ws.mkdir()
    (home / ".claude" / "skills" / "pdf-tools").mkdir(parents=True)
    (home / ".claude" / "skills" / "pdf-tools" / "SKILL.md").write_text("---\nname: pdf-tools\ndescription: Extract text and tables from a PDF document.\n---\nSteps.\n", encoding="utf-8")
    cfg = AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, report=False, capabilities=True,
                      capabilities_dir=str(tmp_path / "caps"), capabilities_home=str(home), permission_mode="auto")
    result = run_headless(cfg, "find a pdf skill", client=ScriptedClient(call("find_capability", {"query": "pdf"}), say("found it")))
    assert result["status"] == "success" and result["turns_count"] == 2
    audit = [json.loads(line) for line in Path(result["audit_log"]).read_text(encoding="utf-8").splitlines()]
    assert any(r["details"].get("tool") == "find_capability" and r["details"].get("outcome") == "ok" for r in audit)


def test_a_headless_plan_is_approved_through_the_same_question_as_a_risky_action(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    asked = []

    def confirm(tool, arguments):
        asked.append((tool, arguments))
        return True

    def cfg(**kw):
        return AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, report=False, permission_mode="plan", **kw)

    steps = lambda: ScriptedClient(call("present_plan", {"plan": "1. write a.txt"}, "p"), call("write_file", {"path": "a.txt", "content": "ok"}, "w"), say("done"))
    result = run_headless(cfg(confirm=confirm), "go", client=steps())
    assert result["status"] == "success" and (ws / "a.txt").read_text(encoding="utf-8") == "ok"
    assert asked == [("present_plan", {"plan": "1. write a.txt", "mode": "default", "outside_content": False})]
    (ws / "a.txt").unlink()
    result = run_headless(cfg(), "go", client=steps())                      # nobody to ask: it stays a plan
    assert not (ws / "a.txt").exists() and result["permission_denials"]["total"] == 1


def test_a_payment_is_put_to_the_person_in_a_headless_run(tmp_path):
    from test_payments_wiring import PAY, set_up
    set_up(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    asked = []

    def run(answer):
        cfg = AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, use_checkpoints=False, report=False, permission_mode="auto",
                          vault_dir=str(tmp_path / "vault"), payments_dir=str(tmp_path / "payments"), confirm=lambda tool, args: asked.append((tool, args)) or answer)
        client = ScriptedClient(call("request_payment", PAY), say("ok"))
        run_headless(cfg, "pay", client=client)
        return [m for m in client.sent[-1] if m["role"] == "tool"][0]["content"]

    refused = run(False)
    assert asked[0][0] == "request_payment" and asked[0][1]["amount"] == "30.00 EUR" and asked[0][1]["recipient"] == "Hotel Cervino"
    assert "pay_with" not in refused
    assert "pay_with" in run(True) and len(asked) == 2


def test_mcp_tools_are_offered_one_by_one_while_few_and_behind_a_search_when_many(tmp_path):
    from harness.mcp_client import MCPClient, MCPHub, hub_from_servers
    fake = str(Path(__file__).resolve().parent / "fake_mcp_server.py")
    hub = hub_from_servers([{"name": "fake", "command": sys.executable, "args": [fake], "env": []}], str(tmp_path))
    try:
        assert not hub.errors and "fake" in hub.catalog
        one = ToolRegistry()
        assert hub.register(one) == "eager" and {"fake__echo", "fake__weather"} <= set(one.names()) and "mcp_call" not in one.names()
        many = ToolRegistry()
        assert hub.register(many, eager_limit=2) == "deferred" and many.names() == ["mcp_call", "mcp_search"]
        found = many.execute("mcp_search", {"query": "weather forecast"})
        assert found[0]["tool"] == "weather"
        assert many.execute("mcp_call", {"server": "fake", "tool": "echo", "arguments": {"text": "x"}}) == "echo: x"
    finally:
        hub.close()
    assert MCPHub().register(ToolRegistry()) == "none"


def test_names_and_kinds_of_the_editors_servers_are_made_safe(tmp_path):
    from harness.mcp_client import hub_from_servers
    hub = hub_from_servers([{"name": "my server!", "command": "no_such_program_xyz"}, {"name": "my server!", "command": "no_such_program_xyz"},
                            {"command": "no_such_program_xyz"}, "not a server", {"name": "web", "type": "http", "url": "ftp://x"}], str(tmp_path))
    assert set(hub.errors) == {"my_server_", "my_server__2", "server3", "web"}
