import json
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from harness.acp_agent import ACPAgentServer
from harness.integrations.wikibridge_client import WikiBridgeClient
from harness.mcp_client import MCPClient
from harness.tools.registry import ToolRegistry


def test_acp_agent_initialize_and_session(tmp_path: Path):
    server = ACPAgentServer(workspace_dir=str(tmp_path))

    # 1. Initialize
    req_init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    res_str = server.run_one(req_init)
    assert res_str is not None
    res = json.loads(res_str)
    assert res["id"] == 1
    assert res["result"]["protocolVersion"] == "1.0"
    assert res["result"]["agentInfo"]["name"] == "BasaltAgent"

    # 2. Session New
    req_new = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(tmp_path)}})
    res_new = json.loads(server.run_one(req_new))
    assert res_new["id"] == 2
    session_id = res_new["result"]["sessionId"]
    assert session_id in server.sessions

    # 3. Session Prompt with mock
    mock_client = MagicMock()
    mock_client.chat_completion.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Hello from agent!"}}]
    }
    server.client = mock_client

    req_prompt = json.dumps({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": "Say hello"},
    })
    res_prompt = json.loads(server.run_one(req_prompt))
    assert res_prompt["id"] == 3
    assert res_prompt["result"]["content"] == "Hello from agent!"


def test_wikibridge_client_offline_graceful():
    # Wikibridge on an unused port returns false/empty without exceptions
    client = WikiBridgeClient(base_url="http://127.0.0.1:59999", timeout=0.1)
    assert not client.is_available()
    assert client.list_projects() == []
    assert client.search("default", "test query") == []
    assert client.get_document("default", "test.md") is None


def test_mcp_client_register_into_registry():
    client = MCPClient(["dummy"])
    # Mock list_tools response
    client.list_tools = MagicMock(return_value=[
        {
            "name": "fetch_weather",
            "description": "Fetches current weather",
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ])
    client.call_tool = MagicMock(return_value="Sunny 25C")

    registry = ToolRegistry()
    client.register_into(registry, prefix="mcp_")

    tool = registry.get("mcp_fetch_weather")
    assert tool is not None
    assert tool.description == "Fetches current weather"

    result = registry.execute("mcp_fetch_weather", {"city": "Rome"})
    assert result == "Sunny 25C"
    client.call_tool.assert_called_once_with("fetch_weather", {"city": "Rome"})
