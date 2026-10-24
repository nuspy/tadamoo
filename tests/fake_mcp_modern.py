"""A stateless-era (2026-07-28) MCP server over stdio for tests. Behaviour is chosen with FAKE_MCP_MODE:
""         plain
"dual"     also answers the legacy initialize handshake
"unsupported"  answers every modern request with UnsupportedProtocolVersion listing FAKE_MCP_SUPPORTED (comma separated)
"state"    the first tools/call is answered with an input_required result that only carries requestState
"elicit"   tools/call is answered with an input_required result that asks for an elicitation
"weird"    tools/call is answered with an unknown resultType
"slow"     tools/call never answers (to see the cancellation)
Every message received is logged to stderr as "recv <method> id=<id>" so a test can see what the client sent.
"""
import json
import os
import sys

MODE = os.environ.get("FAKE_MCP_MODE", "")
SUPPORTED = [v for v in os.environ.get("FAKE_MCP_SUPPORTED", "2026-07-28").split(",") if v]
META = "io.modelcontextprotocol/"
TOOLS = [
    {"name": "echo", "description": "Echoes text back", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "meta", "description": "Returns the metadata of the request", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Always reports an error", "inputSchema": {"type": "object", "properties": {}}},
]
asked = {"state": False}


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def reply(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete", **result}})


def error(rid, code, message, data=None):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message, **({"data": data} if data else {})}})


for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    sys.stderr.write(f"recv {method} id={rid}" + (f" state={params['requestState']}" if "requestState" in params else "")
                     + (f" cancels={params.get('requestId')}" if method == "notifications/cancelled" else "") + "\n")
    sys.stderr.flush()
    if rid is None:
        continue
    if method == "initialize":
        if MODE == "dual":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                                                           "serverInfo": {"name": "fake-dual", "version": "0"}}})
        else:
            error(rid, -32601, "initialize is not a method of this revision")
        continue
    meta = params.get("_meta") or {}
    if MODE == "unsupported" or (method != "initialize" and meta.get(META + "protocolVersion") not in SUPPORTED):
        if MODE == "dual" and "_meta" not in params:
            error(rid, -32602, "missing _meta")
        else:
            error(rid, -32022, "Unsupported protocol version", {"supported": SUPPORTED, "requested": meta.get(META + "protocolVersion")})
        continue
    if META + "clientCapabilities" not in meta:
        error(rid, -32602, "missing client capabilities")
        continue
    if method == "server/discover":
        send({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete", "supportedVersions": SUPPORTED, "capabilities": {"tools": {}},
                                                       "_meta": {META + "serverInfo": {"name": "fake-modern", "version": "2"}},
                                                       "instructions": "Use echo for echoing."}})
    elif method == "tools/list":
        cursor = params.get("cursor")
        reply(rid, {"tools": TOOLS[:2], "nextCursor": "p2"} if not cursor else {"tools": TOOLS[2:]})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if MODE == "slow":
            continue
        if MODE == "weird":
            send({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "surprise"}})
        elif MODE == "elicit":
            send({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "input_required", "inputRequests": {
                "who": {"method": "elicitation/create", "params": {"mode": "form", "message": "Who?", "requestedSchema": {"type": "object"}}}}}})
        elif MODE == "state" and not params.get("requestState"):
            send({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "input_required", "requestState": "opaque-state-1"}})
        elif name == "echo":
            reply(rid, {"content": [{"type": "text", "text": "echo: " + args.get("text", "") + (" (state " + params["requestState"] + ")" if params.get("requestState") else "")}]})
        elif name == "meta":
            reply(rid, {"content": [{"type": "text", "text": json.dumps(meta, sort_keys=True)}]})
        elif name == "fail":
            reply(rid, {"isError": True, "content": [{"type": "text", "text": "it broke"}]})
        else:
            error(rid, -32602, "unknown tool")
    else:
        error(rid, -32601, "no such method")
