"""A stand-in for `python -m harness session` that speaks the same lines of JSON, for the manager's tests.

What the text of a message makes it do: "die" ends the process abruptly (the first --die-times starts only), "hang" stops its
heartbeat and sleeps, "block" says the turn is blocked, "irreversible" leaves a note of an action in flight and ends
abruptly, "tool NAME EFFECT [JSON]" runs a tool call of that name and effect and answers, "write FILE TEXT" writes a file in the folder it works in
(--workdir), anything else is answered with an echo."""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--session-id", default="")
ap.add_argument("--history-dir", required=True)
ap.add_argument("--die-times", type=int, default=1)
ap.add_argument("--heartbeat", type=float, default=0.1)
ap.add_argument("--workdir", default="")
args, _ = ap.parse_known_args()
H = Path(args.history_dir)
H.mkdir(parents=True, exist_ok=True)
out_lock = threading.Lock()


def emit(obj):
    with out_lock:
        print(json.dumps(obj, ensure_ascii=False), flush=True)


def load():
    try:
        return json.loads((H / "history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"messages": [], "turns": 0}


def save(data):
    (H / "history.json").write_text(json.dumps(data), encoding="utf-8")


starts_file = H / "starts"
starts = int(starts_file.read_text() or 0) + 1 if starts_file.exists() else 1
starts_file.write_text(str(starts))
data = load()
blocked = None
if (H / "inflight.json").exists():
    blocked = {"reason": "interrupted_irreversible", "detail": "pay may have run", "options": ["resume", "close"], "tool": "pay"}
    (H / "inflight.json").unlink()
emit({"event": "ready", "session": args.session_id, "restored": len(data["messages"]), "pid": os.getpid(), "blocked": blocked, "starts": starts})

beating = threading.Event()
beating.set()
busy = {"v": False}


def beat():
    while True:
        time.sleep(args.heartbeat)
        if beating.is_set():
            emit({"event": "heartbeat", "busy": busy["v"], "idle_s": 0.0})


threading.Thread(target=beat, daemon=True).start()

for line in sys.stdin:
    cmd = json.loads(line)
    name = cmd.get("cmd")
    if name == "send":
        text = cmd["text"]
        data["messages"].append({"role": "user", "content": text})
        save(data)
        emit({"event": "accepted", "turn": cmd.get("id")})
        busy["v"] = True
        if text == "die" and starts <= args.die_times:
            os._exit(3)
        if text == "hang":
            beating.clear()
            time.sleep(60)
        if text == "irreversible":
            (H / "inflight.json").write_text(json.dumps({"tool": "pay"}))
            os._exit(3)
        if text == "ask":
            emit({"event": "ask", "id": "a1", "mode": "confirm", "kind": "plan_archive", "question": "Archive it?", "facts": {"plan": "X"},
                  "options": [{"label": "Confirm", "value": "confirm"}, {"label": "Cancel", "value": "cancel"}]})
            continue                                                       # waits for the answer
        if text == "code":
            emit({"event": "ask", "id": "c1", "mode": "confirm", "kind": "code", "free": True, "question": "Type the code for bank.", "facts": {"who": "bank"}, "options": []})
            continue                                                       # waits for the code
        if text == "question":
            emit({"event": "ask", "id": "q1", "mode": "question", "question": "Which day?", "options": [{"label": "Sat", "value": "Saturday"}]})
            busy["v"] = False
            emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "asked", "final": ""})
            continue
        if text == "more":
            busy["v"] = False
            emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "final", "final": "first part", "more": True})
            continue
        if text == "rule":
            emit({"event": "rule_added", "rule": "shell_command:pytest *"})
            emit({"event": "rule_added", "rule": "shell_command:pytest *"})
        if text == "queue-me":
            emit({"event": "queued", "turn": cmd.get("id"), "text": text})
            continue
        if text == "block":
            emit({"event": "blocked", "turn": cmd.get("id"), "reason": "refused", "detail": "no", "options": ["resume", "close"]})
            busy["v"] = False
            continue
        if text.startswith("tool "):
            _, tool_name, tool_effect, *rest = text.split(" ", 3)
            tool_args = json.loads(rest[0]) if rest and rest[0].startswith("{") else {}
            call_id = "c%d" % len(data["messages"])
            emit({"event": "agent", "turn": cmd.get("id"), "data": {"type": "tool_start", "call_id": call_id, "name": tool_name, "arguments": tool_args, "effect": tool_effect}})
            output = "Step %s started (ok)." % tool_args.get("node_id") if tool_name == "plan_step_start" else "ok"
            data["messages"].append({"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(tool_args)}}]})
            data["messages"].append({"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": output})
            emit({"event": "agent", "turn": cmd.get("id"), "data": {"type": "tool_end", "call_id": call_id, "name": tool_name, "status": "completed", "output": output}})
        if text.startswith("write ") and args.workdir:
            _, file_name, *body = text.split(" ", 2)
            (Path(args.workdir) / file_name).write_text(body[0] if body else "", encoding="utf-8")
        data["messages"].append({"role": "assistant", "content": "echo: " + text})
        data["turns"] += 1
        save(data)
        busy["v"] = False
        emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "final", "final": "echo: " + text})
    elif name in ("promote", "drop"):
        emit({"event": "routed" if name == "promote" else "dropped", "turn": cmd.get("id"), "how": "urgent"})
    elif name == "answer":
        busy["v"] = False
        emit({"event": "done", "turn": "answered", "stop_reason": "final", "final": "answered: " + str(cmd.get("value"))})
    elif name == "resume":
        last = data["messages"][-1]["content"] if data["messages"] else ""
        if last == "die" and starts <= args.die_times:                      # the same message ends this process again
            os._exit(3)
        if last == "ask":                                                    # it asks the same question again and waits
            continue
        data["turns"] += 1
        save(data)
        emit({"event": "done", "turn": "resumed", "stop_reason": "final", "final": "resumed after %d messages" % len(data["messages"])})
    elif name == "close":
        emit({"event": "closed"})
        break
