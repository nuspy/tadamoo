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
    (H / "inflight.json").unlink()                                          # the real process only tells the model; nothing blocks
try:
    parked = json.loads((H / "parked.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    parked = []
emit({"event": "ready", "session": args.session_id, "restored": len(data["messages"]), "pid": os.getpid(), "blocked": blocked, "starts": starts,
      "parked": parked})

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
        if "place" in cmd:                                                  # the position the manager handed over with this message
            (H / "place.json").write_text(json.dumps(cmd["place"]), encoding="utf-8")
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
        if text == "pending":
            item = {"id": "p1", "turn": cmd.get("id"), "source": {"kind": "permission", "named": "people"}, "mode": "confirm", "kind": "permission",
                    "tool": "phone_call", "question": "Call Anna?", "options": [{"label": "Call", "value": "confirm"}, {"label": "No", "value": "cancel"}]}
            (H / "parked.json").write_text(json.dumps([item]), encoding="utf-8")
            emit({"event": "pending", **item})
            busy["v"] = False
            emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "final", "final": "went on"})
            continue
        if text == "code":
            emit({"event": "ask", "id": "c1", "mode": "confirm", "kind": "code", "free": True, "question": "Type the code for bank.", "facts": {"who": "bank"}, "options": []})
            continue                                                       # waits for the code
        if text == "capability":
            emit({"event": "ask", "id": "k1", "mode": "confirm", "kind": "capability_choice", "free_prefix": "other:", "question": "Install which?", "facts": {"candidates": []},
                  "options": [{"label": "Install 1", "value": "install:1"}, {"label": "Other", "value": "other"}, {"label": "No", "value": "cancel"}]})
            continue                                                       # waits for the choice
        if text == "question":
            emit({"event": "ask", "id": "q1", "mode": "question", "question": "Which day?", "options": [{"label": "Sat", "value": "Saturday"}]})
            busy["v"] = False
            emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "asked", "final": ""})
            continue
        if text == "card":
            emit({"event": "ask", "id": "k2", "mode": "question", "kind": "questions", "intro": "", "notes": [], "question": "Which database?", "options": [],
                  "questions": [{"id": "q1", "text": "Which database?", "kind": "single", "required": True, "allow_other": True,
                                 "options": [{"id": "o1", "label": "PostgreSQL", "recommended": True}, {"id": "o2", "label": "SQLite"}]}]})
            busy["v"] = False
            emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "asked", "final": ""})
            continue
        if text == "assume":
            emit({"event": "auto_answered", "id": "z1", "turn": cmd.get("id"), "kind": "question", "value": "o1", "answer": "PostgreSQL", "chosen": "recommended",
                  "available": True, "source": {"kind": "question", "named": None, "needs_data": False}, "question": "Which database?"})
            emit({"event": "auto_answered", "id": "z2", "turn": cmd.get("id"), "kind": "question", "value": "", "answer": "(no answer)", "chosen": "parked",
                  "available": False, "source": {"kind": "question", "named": "people", "needs_data": False}, "question": "Call the vet?"})
            busy["v"] = False
            emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "final", "final": "all done"})
            continue
        if text == "chips":
            busy["v"] = False
            emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "final", "final": "an answer"})
            emit({"event": "suggestions", "turn": cmd.get("id"), "items": [{"label": "More", "prompt": "Tell me more"}]})
            continue
        if text == "more":
            busy["v"] = False
            emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "final", "final": "first part", "more": True})
            continue
        if text == "rule":
            emit({"event": "rule_added", "rule": "shell_command:pytest *"})
            emit({"event": "rule_added", "rule": "shell_command:pytest *"})
        if text == "rule-global":
            emit({"event": "rule_added", "rule": "shell_command:python *", "scope": "global"})
        if text == "two-off":
            emit({"event": "rule_of_two_off", "scope": "chat"})
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
    elif name == "skill":
        data["messages"].append({"role": "user", "content": cmd.get("display", "")})
        emit({"event": "accepted", "turn": cmd.get("id")})
        final = "skill %s %s" % (cmd.get("name"), json.dumps(cmd.get("values"), sort_keys=True, ensure_ascii=False))
        data["messages"].append({"role": "assistant", "content": final})
        data["turns"] += 1
        save(data)
        emit({"event": "done", "turn": cmd.get("id"), "stop_reason": "final", "final": final})
    elif name == "mode":
        emit({"event": "mode", "mode": cmd.get("mode")})
    elif name == "role":
        emit({"event": "role", "role": cmd.get("role")})
    elif name == "checks":
        emit({"event": "checks", "safe": len(cmd.get("safe", [])), "checks": len(cmd.get("checks", []))})
    elif name == "effort":
        emit({"event": "effort", "level": cmd.get("level")})
    elif name == "intake":
        emit({"event": "intake_mode", "mode": cmd.get("mode")})
    elif name == "settings":
        emit({"event": "settings", **{k: cmd[k] for k in ("max_turns", "end_turn_bias") if k in cmd}})
    elif name == "command":
        emit({"event": "command", "id": cmd.get("id"), "name": cmd.get("name"), "ok": True, "data": {"args": cmd.get("args")}})
    elif name in ("promote", "drop"):
        emit({"event": "routed" if name == "promote" else "dropped", "turn": cmd.get("id"), "how": "urgent"})
    elif name == "answer" and cmd.get("id") == "p1":
        (H / "parked.json").unlink()
        data["messages"].append({"role": "user", "content": "[harness] answered p1: %s" % cmd.get("value")})
        save(data)
        emit({"event": "pending_done", "id": "p1", "value": cmd.get("value")})
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
