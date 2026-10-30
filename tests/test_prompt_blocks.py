"""The optional working rules (`--instruction-blocks`): off unless asked for, and worded for the tools the conversation has."""
import pytest

from harness.prompt_blocks import RULES, instruction_blocks
from harness.stack import AgentConfig, build_agent

ALL = ["read_file", "list_directory", "shell_command", "browse_url", "search_web", "write_file"]


def test_each_rule_is_a_short_line_and_the_set_covers_the_six_habits():
    text = instruction_blocks(ALL)
    lines = text.splitlines()
    assert len(lines) == len(RULES) == 6 and all(ln.startswith("- ") and len(ln) < 260 for ln in lines)
    for word in ("do it now", "change one thing", "checked now", "tool result", "Never invent", "promise"):
        assert word in text


def test_a_rule_that_names_tools_names_only_the_ones_that_exist():
    full = instruction_blocks(ALL)
    assert "`search_web` or `browse_url` or `read_file` or `list_directory` or `shell_command`" in full
    some = instruction_blocks(["read_file", "write_file"])
    assert "`read_file`" in some and "search_web" not in some and "browse_url" not in some and "shell_command" not in some


def test_a_rule_whose_tools_are_all_missing_is_left_out():
    none = instruction_blocks(["write_file"])
    assert "checked now" not in none and len(none.splitlines()) == len(RULES) - 1
    for name in ("search_web", "browse_url", "read_file", "list_directory", "shell_command"):
        assert name not in none


def test_no_rule_names_a_tool_outside_its_list():
    named = {t for _, tools in RULES for t in tools}
    text = instruction_blocks(sorted(named | {"write_file"}))
    assert all(f"`{t}`" in text for t in named)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def test_the_blocks_are_off_by_default(ws, tmp_path):
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False), client=object())
    try:
        assert "# Working rules" not in built.agent.messages[0]["content"] and "discipline" not in built.builder.blocks
    finally:
        built.close()


def test_the_blocks_go_into_the_system_prompt_right_after_the_core_with_the_tools_the_session_has(ws, tmp_path):
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "d"), use_memory=False, instruction_blocks=True), client=object())
    try:
        system = built.agent.messages[0]["content"]
        assert system.index("# Working rules") < system.index("# Environment")
        rules = system.split("# Working rules\n")[1].split("\n\n#")[0]
        assert "`read_file`" in rules and "`shell_command`" in rules
        have = set(built.agent.tools.names())
        for tool in ("search_web", "browse_url", "read_file", "list_directory", "shell_command"):
            assert (f"`{tool}`" in rules) == (tool in have)
    finally:
        built.close()
