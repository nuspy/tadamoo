import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from harness.tools import create_standard_tool_registry, WorkspaceSandbox
from harness.agent_loop import AgentLoop, LoopDetector
from harness.headless import parse_args


def test_workspace_boundary_enforcement(tmp_path: Path):
    sandbox = WorkspaceSandbox(tmp_path)

    # Valid write inside workspace
    res = sandbox.write_file("sub/hello.txt", "Hello World")
    assert res["status"] == "success"
    assert (tmp_path / "sub" / "hello.txt").read_text(encoding="utf-8") == "Hello World"

    # Attempt path traversal outside workspace
    with pytest.raises(PermissionError):
        sandbox.read_file("../../outside.txt")

    with pytest.raises(PermissionError):
        sandbox.write_file("../forbidden.txt", "data")


def test_diff_generation_on_edits(tmp_path: Path):
    sandbox = WorkspaceSandbox(tmp_path)

    # Create file
    sandbox.write_file("code.py", "def add(a, b):\n    return a + b\n")
    assert len(sandbox.changes) == 1
    assert sandbox.changes[0]["kind"] == "added"
    assert "+def add" in sandbox.changes[0]["diff"]

    # Edit file
    sandbox.edit_file("code.py", "return a + b", "return a + b + 1")
    assert len(sandbox.changes) == 2
    assert sandbox.changes[1]["kind"] == "modified"
    assert "+    return a + b + 1" in sandbox.changes[1]["diff"]
    assert "-    return a + b" in sandbox.changes[1]["diff"]


def test_loop_detector():
    ld = LoopDetector(max_consecutive_repeats=2)
    assert not ld.check_and_record("read_file", {"path": "a.txt"})
    assert not ld.check_and_record("read_file", {"path": "a.txt"})
    # 3rd consecutive identical call triggers loop detection
    assert ld.check_and_record("read_file", {"path": "a.txt"})


def test_agent_loop_execution_with_mock_client(tmp_path: Path):
    mock_client = MagicMock()

    # Step 1: Assistant requests a tool call via Qwen XML format
    call_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "<tool_call>{\"name\": \"write_file\", \"arguments\": {\"path\": \"test.txt\", \"content\": \"Done!\"}}</tool_call>",
            }
        }]
    }
    # Step 2: Assistant finishes
    final_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "I have created the test file.",
            }
        }]
    }
    mock_client.chat_completion.side_effect = [call_response, final_response]

    registry, sandbox = create_standard_tool_registry(tmp_path)
    agent = AgentLoop(client=mock_client, tool_registry=registry, max_turns=5)

    turns = agent.run("Create test file")
    assert len(turns) == 2
    assert (tmp_path / "test.txt").read_text(encoding="utf-8") == "Done!"
    assert turns[1].assistant_content == "I have created the test file."


def test_headless_cli_args():
    args = parse_args(["-p", "test task", "--workspace", "my_ws", "--json"])
    assert args.prompt == "test task"
    assert args.workspace == "my_ws"
    assert args.json is True
