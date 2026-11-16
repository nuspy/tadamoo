"""A stand-in for the agent-desktop program, for the tests (tests/test_computer_agent_desktop.py): it prints JSON envelopes from a file and records every call.

FAKE_AD_STATE is a folder with responses.json: {command: envelope or [envelopes, used one by one] or {"raw": text} or {"sleep": seconds, "then": envelope}}. A command with no
entry answers ok with a verified delivery. calls.jsonl gets one line per call: the arguments, what came on standard input, and AGENT_DESKTOP_HOME."""
import json
import os
import sys
import time

state = os.environ["FAKE_AD_STATE"]
argv = sys.argv[1:]
stdin = sys.stdin.read() if "--stdin" in argv else None
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "stdin": stdin, "home": os.environ.get("AGENT_DESKTOP_HOME")}) + "\n")
command = next((a for a in argv if not a.startswith("-")), "")
path = os.path.join(state, "responses.json")
responses = json.load(open(path, encoding="utf-8"))
answer = responses.get(command)
if isinstance(answer, list):
    answer = answer.pop(0) if len(answer) > 1 else answer[0]
    json.dump(responses, open(path, "w", encoding="utf-8"))
if answer is None:
    answer = {"version": "2.6", "ok": True, "command": command, "data": {"disposition": {"delivery": "delivered_verified", "retry": "unsafe"}}}
if "sleep" in answer:
    time.sleep(answer["sleep"])
    answer = answer.get("then") or {"version": "2.6", "ok": True, "command": command, "data": {}}
if "write_png" in answer and argv and argv[-1].endswith(".png"):                  # screenshot PATH: a small PNG
    import struct, zlib
    png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", answer["write_png"][0], answer["write_png"][1], 8, 2, 0, 0, 0) + b"\0\0\0\0"
    open(argv[-1], "wb").write(png)
if "raw" in answer:
    sys.stdout.write(answer["raw"])
    sys.exit(0)
sys.stdout.write(json.dumps(answer))
sys.exit(0 if answer.get("ok", True) else 1)
