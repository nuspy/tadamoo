"""A small MCP server with an app (MCP Apps, io.modelcontextprotocol/ui), for the tests and as a demo (docs/mcp-apps.md).

It speaks MCP over stdio with the standard library only. The tool `survey` is tied to the page `ui://demo/survey` (a form and a chart); the page calls the tools
`compute_series` and `save_answers`, which only the app may call (`visibility: ["app"]`), and tells the model what the person chose. When the client does not offer
the extension the tool is the same tool without a page, as the specification asks of a server.

Try it: put `{"mcpServers": {"app-demo": {"command": "python", "args": ["<path>/tools/mcp-app-demo/server.py"]}}}` in a `.mcp.json` of a project, import it
(Settings, Imported), turn it on, and ask the assistant to open the survey. Set MCP_APP_DEMO_LOG to a file to see what the server was asked."""
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXT = "io.modelcontextprotocol/ui"
MIME = "text/html;profile=mcp-app"
URI = "ui://demo/survey"
LOG = os.environ.get("MCP_APP_DEMO_LOG", "")
CLIENT = {"ui": False}
SAVED = []


def tools():
    ui = {"ui": {"resourceUri": URI, "visibility": ["model", "app"]}} if CLIENT["ui"] else {}
    app_only = {"ui": {"resourceUri": URI, "visibility": ["app"]}}
    out = [{"name": "survey", "title": "Survey", "description": "Opens a small survey with a chart for the person to answer.",
            "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["title"]},
            "annotations": {"readOnlyHint": True}, **({"_meta": ui} if ui else {})},
           {"name": "plain_note", "description": "A tool with no app: returns a short note.", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}]
    if CLIENT["ui"]:
        out += [{"name": "compute_series", "description": "The points of the chart (only the app calls it).", "annotations": {"readOnlyHint": True},
                 "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}}, "_meta": app_only},
                {"name": "save_answers", "description": "Saves the person's answer (only the app calls it).",
                 "inputSchema": {"type": "object", "properties": {"choice": {"type": "string"}}, "required": ["choice"]}, "_meta": app_only}]
    return out


def series(n):
    n = max(3, min(int(n or 8), 30))
    return [round(10 + 8 * math.sin(i * 0.7) + i * 0.8, 2) for i in range(n)]


def call(name, args):
    if name == "survey":
        data = {"title": args.get("title", "Survey"), "options": args.get("options") or ["Yes", "No"], "series": series(8)}
        return {"content": [{"type": "text", "text": f"Survey '{data['title']}' is open with the options {', '.join(data['options'])}."}], "structuredContent": data}
    if name == "compute_series":
        data = {"series": series(args.get("n"))}
        return {"content": [{"type": "text", "text": f"{len(data['series'])} points"}], "structuredContent": data}
    if name == "save_answers":
        SAVED.append(args.get("choice"))
        return {"content": [{"type": "text", "text": f"saved {args.get('choice')} ({len(SAVED)} so far)"}]}
    if name == "plain_note":
        return {"content": [{"type": "text", "text": "a note"}]}
    return {"content": [{"type": "text", "text": f"no tool {name}"}], "isError": True}


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        msg = json.loads(line)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if LOG:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"method": method, "params": params}) + "\n")
        if rid is None:
            continue
        if method == "initialize":
            offered = ((params.get("capabilities") or {}).get("extensions") or {}).get(EXT) or {}
            CLIENT["ui"] = MIME in (offered.get("mimeTypes") or [])
            caps = {"tools": {"listChanged": False}, "resources": {}, **({"extensions": {EXT: {}}} if CLIENT["ui"] else {})}
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-11-25", "capabilities": caps, "serverInfo": {"name": "app-demo", "version": "1.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools()}})
        elif method == "tools/call":
            send({"jsonrpc": "2.0", "id": rid, "result": call(params.get("name"), params.get("arguments") or {})})
        elif method == "resources/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"resources": [{"uri": URI, "name": "survey", "description": "The survey page", "mimeType": MIME,
                                                                         "_meta": {"ui": {"prefersBorder": True}}}]}})
        elif method == "resources/read" and params.get("uri") == URI:
            html = (HERE / "view.html").read_text(encoding="utf-8")
            send({"jsonrpc": "2.0", "id": rid, "result": {"contents": [{"uri": URI, "mimeType": MIME, "text": html, "_meta": {"ui": {"csp": {}, "prefersBorder": True}}}]}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
