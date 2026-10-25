"""A tiny MCP server over stdio that offers one search tool; it answers with the query and the key it was started with."""
import json
import os
import sys

TOOLS = [
    {"name": "fetch_docs", "description": "Not a search", "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
    {"name": "web_search", "description": "Searches the web",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["query"]}},
]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                                                       "serverInfo": {"name": "fake-search", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        args = msg["params"].get("arguments") or {}
        text = f"results for {args.get('query')} limit={args.get('max_results')} key={os.environ.get('FAKE_SEARCH_KEY')}"
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
