"""Runs a personal skill's script and answers its calls.

The script runs in a process of its own (the interpreter in isolated mode, a scrubbed environment, on Windows inside a job object with a
memory and process cap) and talks over its pipes in lines of JSON. What it asks for - a tool, the model, a question to the person - is done
here by the functions the caller gives, so it goes through the same permissions, hooks and audit record as anything the agent does. Stop
(the cancel event) and the time limit end the process and everything it started."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.sandbox import CommandJob, CREATE_SUSPENDED, kill_tree, safe_environment

ENTRY = Path(__file__).with_name("runtime") / "entry.py"
PREFIX = "@@basalt@@ "
DEFAULT_TIMEOUT = 1800.0
MAX_OUTPUT = 200_000
MAX_MEMORY = 2 << 30
MAX_PROCESSES = 16


@dataclass
class RunResult:
    ok: bool
    output: str = ""                                 # what main returned
    printed: str = ""                                # what the script printed
    error: str = ""
    seconds: float = 0.0
    calls: List[Dict[str, Any]] = field(default_factory=list)       # the tools it called: name, whether it worked


@dataclass
class Handlers:
    """What answers the script: `tool(name, arguments) -> (ok, text)`, `ai(prompt, inputs, cache) -> text` (raises on failure),
    `ask(question, options) -> text` and `say(text)`."""
    tool: Callable[[str, Dict[str, Any]], Tuple[bool, str]]
    ai: Callable[[str, Dict[str, Any], bool], str]
    ask: Callable[[str, List[str]], str]
    say: Callable[[str], None] = lambda text: None


def run(script: Path, mode: str, values: Dict[str, Any], handlers: Handlers, cancel: Optional[threading.Event] = None,
        timeout: float = DEFAULT_TIMEOUT, cwd: Optional[Path] = None) -> RunResult:
    started = time.monotonic()
    context = Path(tempfile.mkstemp(prefix="skill-", suffix=".json")[1])
    try:
        context.write_text(json.dumps({"mode": mode, "params": values}, ensure_ascii=False), encoding="utf-8")
        job = CommandJob(MAX_MEMORY, MAX_PROCESSES)
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | (CREATE_SUSPENDED if job.active else 0)
        env = safe_environment({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        try:
            proc = subprocess.Popen([sys.executable, "-I", "-X", "utf8", str(ENTRY), str(script), str(context)], cwd=str(cwd or script.parent),
                                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", bufsize=1, **({"creationflags": flags} if flags else {"start_new_session": True}))
        except OSError as exc:
            job.close()
            return RunResult(False, error=f"the script could not be started: {exc}", seconds=time.monotonic() - started)
        if job.active:
            job.attach(proc)
        stderr: List[str] = []
        threading.Thread(target=lambda: stderr.append(proc.stderr.read(20000)), daemon=True).start()
        ended = {"why": ""}
        done = threading.Event()

        def watch() -> None:
            while not done.wait(0.2):
                if cancel is not None and cancel.is_set():
                    ended["why"] = "stopped"
                elif time.monotonic() - started > timeout:
                    ended["why"] = "timeout"
                else:
                    continue
                job.kill()
                kill_tree(proc)
                return

        threading.Thread(target=watch, daemon=True).start()
        printed: List[str] = []
        result: Dict[str, Any] = {}
        calls: List[Dict[str, Any]] = []
        try:
            for line in proc.stdout:
                if not line.startswith(PREFIX):
                    continue
                try:
                    message = json.loads(line[len(PREFIX):])
                except ValueError:
                    continue
                kind = message.get("type")
                if kind == "print":
                    printed.append(str(message.get("text", "")))
                elif kind == "say":
                    handlers.say(str(message.get("text", "")))
                elif kind in ("result", "error"):
                    result = message
                elif kind in ("tool", "ai", "ask"):
                    reply = _answer(kind, message, handlers, calls)
                    try:
                        proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
                        proc.stdin.flush()
                    except OSError:
                        break
        finally:
            done.set()
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
            job.close()
        seconds = round(time.monotonic() - started, 2)
        text = "\n".join(printed)[:MAX_OUTPUT]
        if ended["why"] == "stopped":
            return RunResult(False, printed=text, error="stopped", seconds=seconds, calls=calls)
        if ended["why"] == "timeout":
            return RunResult(False, printed=text, error=f"it took more than {timeout:g} seconds and was ended", seconds=seconds, calls=calls)
        if result.get("type") == "result":
            return RunResult(True, output=str(result.get("value", ""))[:MAX_OUTPUT], printed=text, seconds=seconds, calls=calls)
        message = result.get("message") or (stderr[0].strip().splitlines()[-1] if stderr and stderr[0].strip() else f"the script ended without a result (code {proc.returncode})")
        return RunResult(False, printed=text, error=str(message), seconds=seconds, calls=calls)
    finally:
        try:
            context.unlink()
        except OSError:
            pass


def _answer(kind: str, message: Dict[str, Any], handlers: Handlers, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    reply: Dict[str, Any] = {"id": message.get("id"), "ok": True}
    try:
        if kind == "tool":
            name, arguments = str(message.get("name", "")), message.get("arguments") or {}
            ok, text = handlers.tool(name, arguments if isinstance(arguments, dict) else {})
            calls.append({"name": name, "ok": ok})
            if ok:
                reply["value"] = text
            else:
                reply.update(ok=False, error=text)
        elif kind == "ai":
            reply["value"] = handlers.ai(str(message.get("prompt", "")), message.get("inputs") or {}, bool(message.get("cache", True)))
        else:
            reply["value"] = handlers.ask(str(message.get("question", "")), [str(o) for o in message.get("options") or []])
    except Exception as exc:                                    # what went wrong is told to the script, which may go on without it
        reply.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        reply.pop("value", None)
    return reply
