"""A stand-in for the agent-browser program, for the tests (tests/test_browser_ab.py): it prints JSON answers from a file and records every call and every command of a batch.

FAKE_AB_STATE is a folder with responses.json: {"command words": data, ...}; the key is the command and its first argument(s) (`open`, `snapshot`, `get url`, `get attr @e3 type`, `get attr type`,
`dialog status`, `tab`, `click @e7`...), the longest one that matches is used. A value is the `data` of the answer, a list of them (used one by one), or {"__error__": {"error": text, "code": code}}.
A command with no entry answers with empty data. `batch` reads its commands from standard input (a JSON list of lists) and answers a list of {index, success, result|error}.
calls.jsonl gets one line per process (the arguments, the standard input) and one per command inside a batch ({"batch_command": [...]}); HOME is recorded too.
"""
import json
import os
import sys

state = os.environ["FAKE_AB_STATE"]
argv = sys.argv[1:]
VALUE = {"--config", "--session", "--idle-timeout", "--max-output", "--cdp", "--executable-path", "--profile", "--download-path", "--args"}
words, i = [], 0
while i < len(argv):
    if argv[i] in VALUE:
        i += 2
    elif argv[i] in ("--json", "--headed", "--omit-command", "--bail", "--full"):
        i += 1
    else:
        words.append(argv[i])
        i += 1
stdin = sys.stdin.read() if "batch" in words[:1] or "--stdin" in words else None
log = open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8")
log.write(json.dumps({"argv": argv, "stdin": stdin, "home": os.environ.get("HOME")}) + "\n")
path = os.path.join(state, "responses.json")
responses = json.load(open(path, encoding="utf-8"))


def answer(cmd):
    """The data (or the error) for one command: the longest key that is a prefix of its words."""
    for n in range(len(cmd), 0, -1):
        key = " ".join(cmd[:n])
        if key in responses:
            value = responses[key]
            if isinstance(value, list):
                value = value.pop(0) if len(value) > 1 else value[0]
                json.dump(responses, open(path, "w", encoding="utf-8"))
            return value
    return {}


def one(cmd):
    got = answer(cmd)
    if isinstance(got, dict) and "__error__" in got:
        return False, got["__error__"]
    return True, got


if words and words[0] == "batch":
    commands = json.loads(stdin or "[]")
    out = []
    for n, cmd in enumerate(commands):
        log.write(json.dumps({"batch_command": cmd}) + "\n")
        ok, got = one([str(c) for c in cmd])
        out.append({"index": n, "success": ok, **({"result": got, "error": None} if ok else {"result": None, "error": got["error"], "code": got.get("code", "")})})
        if not ok and "--bail" in argv:
            break
    sys.stdout.write(json.dumps(out))
    sys.exit(0)
if words and words[0] == "eval":
    log.write(json.dumps({"eval": stdin}) + "\n")
ok, got = one(words)
if not ok:
    sys.stdout.write(json.dumps({"success": False, "data": None, "error": got["error"], "code": got.get("code", "")}))
    sys.exit(1)
if "write_png" in (got if isinstance(got, dict) else {}) and words and words[0] == "screenshot":
    open(words[-1], "wb").write(b"\x89PNG\r\n\x1a\nfake")
sys.stdout.write(json.dumps({"success": True, "data": got, "error": None}))
