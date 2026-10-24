import json
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from harness.integrations.wikibridge_client import WikiBridgeClient
from harness.mcp_client import MCPClient
from harness.tools.registry import ToolRegistry


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
