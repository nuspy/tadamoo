"""A language server that speaks just enough LSP for the tests: a diagnostic for every line holding BAD, a definition at the top of the
file, two references, and a request of its own to the client. FAKE_PULL=1 answers diagnostics on request instead of publishing them."""
import json
import os
import sys

PULL = os.environ.get("FAKE_PULL") == "1"
docs = {}


def send(msg):
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length))


def problems(text):
    return [{"range": {"start": {"line": i, "character": line.index("BAD")}, "end": {"line": i, "character": line.index("BAD") + 3}},
             "severity": 1, "message": "bad word", "code": "E1"} for i, line in enumerate(text.split("\n")) if "BAD" in line]


while True:
    msg = read()
    if msg is None:
        break
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        caps = {"textDocumentSync": 1, "definitionProvider": True, "referencesProvider": True}
        if PULL:
            caps["diagnosticProvider"] = {"interFileDependencies": False, "workspaceDiagnostics": False}
        send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": caps}})
    elif method == "initialized":
        send({"jsonrpc": "2.0", "id": 9001, "method": "workspace/configuration", "params": {"items": [{"section": "x"}]}})
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        td = params["textDocument"]
        text = td["text"] if method == "textDocument/didOpen" else params["contentChanges"][0]["text"]
        docs[td["uri"]] = text
        if not PULL:
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": td["uri"], "diagnostics": problems(text)}})
    elif method == "textDocument/diagnostic":
        send({"jsonrpc": "2.0", "id": rid, "result": {"kind": "full", "items": problems(docs.get(params["textDocument"]["uri"], ""))}})
    elif method == "textDocument/definition":
        send({"jsonrpc": "2.0", "id": rid, "result": {"uri": params["textDocument"]["uri"], "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}})
    elif method == "textDocument/references":
        uri = params["textDocument"]["uri"]
        r = {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 6}}
        send({"jsonrpc": "2.0", "id": rid, "result": [{"uri": uri, "range": r}, {"uri": uri, "range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 1}}}]})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif method == "exit":
        break
    elif method == "boom":
        os._exit(3)
