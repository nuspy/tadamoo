"""A stand-in ACP agent on stdio for the client's tests. What the prompt says decides what it does:

hello       two message chunks and one tool call, then end_turn
permission  asks session/request_permission and reports the option it was given
env         reports PASS_ME and NOT_ME from its environment
fs          asks the client for fs/read_text_file and reports whether it was refused
cancel      starts, then waits for session/cancel and ends with stopReason cancelled
crash       writes to stderr and exits
hang        never answers
"""
import json
import os
import queue
import sys
import threading
import time

VERSION = int(sys.argv[sys.argv.index("--version") + 1]) if "--version" in sys.argv else 1
inbox = queue.Queue()
print("fake agent banner that is not JSON", flush=True)


def send(obj):
    print(json.dumps(obj), flush=True)


def reader():
    for line in sys.stdin:
        if line.strip():
            inbox.put(json.loads(line))
    inbox.put(None)


threading.Thread(target=reader, daemon=True).start()
next_id = 100


def ask(method, params):
    global next_id
    next_id += 1
    send({"jsonrpc": "2.0", "id": next_id, "method": method, "params": params})
    while True:
        msg = inbox.get()
        if msg is None:
            sys.exit(0)
        if msg.get("id") == next_id and "method" not in msg:
            return msg


def update(session, body):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session, "update": body}})


def say(session, text):
    update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


while True:
    msg = inbox.get()
    if msg is None:
        break
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": VERSION, "authMethods": [],
                                                       "agentCapabilities": {}, "agentInfo": {"name": "fake", "title": "Fake Agent"}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s-1"}})
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        sid = params["sessionId"]
        if text == "hello":
            say(sid, "Hi ")
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read notes.txt", "kind": "read", "status": "pending"})
            update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
            say(sid, "there")
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif text == "permission":
            answer = ask("session/request_permission", {
                "sessionId": sid,
                "toolCall": {"toolCallId": "t2", "title": "Run rm -rf build", "kind": "execute", "rawInput": {"command": "rm -rf build"}},
                "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"}, {"optionId": "no", "name": "Reject", "kind": "reject_once"}]})
            outcome = answer["result"]["outcome"]
            say(sid, "outcome=" + (outcome.get("optionId") or outcome["outcome"]))
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif text == "env":
            say(sid, os.environ.get("PASS_ME", "none") + "," + os.environ.get("NOT_ME", "none"))
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif text == "fs":
            answer = ask("fs/read_text_file", {"sessionId": sid, "path": "x"})
            say(sid, "fs refused" if "error" in answer else "fs allowed")
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif text == "cancel":
            say(sid, "working")
            while True:
                nxt = inbox.get()
                if nxt is None or nxt.get("method") == "session/cancel":
                    break
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "cancelled"}})
        elif text == "crash":
            print("fatal: the model key is missing", file=sys.stderr, flush=True)
            sys.exit(3)
        elif text == "hang":
            time.sleep(60)
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}})
