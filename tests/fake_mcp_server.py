"""A tiny MCP server over stdio for tests. Behaviour is chosen with environment variables:
FAKE_MCP_VERSION  protocol version to answer with (default 2025-11-25)
FAKE_MCP_MODE     "" | "noisy" (floods stderr) | "ping" (asks the client a ping with a colliding id) | "die" (exits on tools/call)
"""
import json
import os
import sys
import time

VERSION = os.environ.get("FAKE_MCP_VERSION", "2025-11-25")
MODE = os.environ.get("FAKE_MCP_MODE", "")
TOOLS = [
    {"name": "echo", "description": "Echoes text back", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "fail", "description": "Always reports an error", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "image", "description": "Returns an image", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Takes a while", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "weather", "description": "Weather forecast for a city\x07\x1b[31m with control characters", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "bad name!", "description": "Name with characters that are not allowed", "inputSchema": {"type": "object", "properties": {}}},
]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if MODE == "noisy" and method == "tools/list":
        for i in range(3000):
            sys.stderr.write("noise " * 50 + "\n")
        sys.stderr.flush()
    if rid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": VERSION, "capabilities": {"tools": {}},
                                                       "serverInfo": {"name": "fake", "version": "0"}}})
    elif method == "tools/list":
        cursor = (msg.get("params") or {}).get("cursor")
        if not cursor:
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[:3], "nextCursor": "page2"}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[3:]}})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if MODE == "die":
            sys.exit(3)
        if MODE == "ping":
            send({"jsonrpc": "2.0", "id": rid, "method": "ping"})           # same id as the pending request
            time.sleep(0.2)
        if name == "echo":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "echo: " + args.get("text", "")}]}})
        elif name == "fail":
            send({"jsonrpc": "2.0", "id": rid, "result": {"isError": True, "content": [{"type": "text", "text": "it broke"}]}})
        elif name == "image":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "image", "mimeType": "image/png", "data": "AAAA"}]}})
        elif name == "slow":
            time.sleep(2)
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "slow done"}]}})
        elif name == "weather":
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "Sunny in " + args["city"]}]}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown tool"}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}})
