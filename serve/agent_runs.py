"""Starts the agent from the server's pages, watches it and stops it.

The agent is the separate process `python -m harness`; this only launches it with the choices made on the page,
keeps its output in a log, and records how it ended. One run at a time: the model serves one conversation at a time
and two agents in one folder would undo each other. A run that is still going when the server ends is killed with it.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from serve import ROOT
from serve.approvals import ApprovalBoard
from serve.tasks import OPEN_STATES, TaskJournal, _write_json
from serve.util.winjob import contain

MODES = ("plan", "read_only", "default", "auto")
KEEP_RUNS = 50
GOAL_CHARS = 4000
SHOWN_CHARS = 300
LOG_TAIL_BYTES = 65_536
MAX_ALLOW = 10
RUNS_FILE = "agent_runs.json"
SAFE_ENV_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT", "USERPROFILE", "HOME", "APPDATA",
                 "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "LANG", "LC_ALL", "TERM", "BASALT_DATA"}


class RunError(ValueError):
    """A run that cannot be started or stopped, with a message fit for the person."""


class RunBusy(RunError):
    pass


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _final_json(text: str) -> Dict[str, Any]:
    """The result the agent prints when it ends: the last top-level JSON object of its output, or {}."""
    pos = text.rfind("\n{")
    for start in ([pos + 1] if pos >= 0 else []) + ([0] if text.lstrip().startswith("{") else []):
        try:
            data = json.loads(text[start:])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            continue
    return {}


class AgentRunner:
    def __init__(self, directory: str | Path, endpoint: str, api_key: str = "", tasks: Optional[TaskJournal] = None,
                 board: Optional[ApprovalBoard] = None, python: Optional[str] = None, root: Path = ROOT) -> None:
        self.directory = Path(directory)
        self.endpoint = endpoint
        self.api_key = api_key
        self.tasks = tasks
        self.board = board
        self.python = python or sys.executable
        self.root = Path(root)
        self._lock = threading.RLock()
        self._procs: Dict[str, "subprocess.Popen[bytes]"] = {}
        self.runs: List[Dict[str, Any]] = self._load()

    # -- records ---------------------------------------------------------------------------------------

    def _load(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads((self.directory / RUNS_FILE).read_text(encoding="utf-8"))
            runs = data.get("runs") if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            runs = None
        runs = [r for r in (runs or []) if isinstance(r, dict) and r.get("id")]
        for run in runs:
            if run.get("status") == "running":              # the server ended while it ran, and took it along
                run["status"], run["ended"] = "interrupted", run.get("ended") or time.time()
        return runs[-KEEP_RUNS:]

    def _save(self) -> None:
        _write_json(self.directory / RUNS_FILE, {"schema": "1.0", "runs": self.runs[-KEEP_RUNS:]})

    def _log_path(self, run_id: str) -> Path:
        return self.directory / "runs" / f"{run_id}.log"

    def view(self) -> Dict[str, Any]:
        with self._lock:
            runs = [dict(r) for r in reversed(self.runs)]
            return {"runs": runs, "active": next((r["id"] for r in runs if r["status"] == "running"), None)}

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return next((dict(r) for r in self.runs if r["id"] == run_id), None)

    def log(self, run_id: str, lines: int = 300) -> Dict[str, Any]:
        if self.get(run_id) is None:
            raise RunError("That run is not in the list any more.")
        path = self._log_path(run_id)
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                f.seek(max(0, size - LOG_TAIL_BYTES))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return {"text": "", "truncated": False}
        tail = text.splitlines()[-max(1, min(int(lines), 2000)):]
        return {"text": "\n".join(tail), "truncated": size > LOG_TAIL_BYTES or len(tail) < len(text.splitlines())}

    # -- starting and stopping -------------------------------------------------------------------------

    @staticmethod
    def check_workspace(workspace: Any) -> Path:
        if not isinstance(workspace, str) or not workspace.strip():
            raise RunError("Choose the folder the agent works in.")
        path = Path(workspace.strip()).expanduser()
        if not path.is_absolute():
            raise RunError("Give the full path of the folder, for example D:\\projects\\site.")
        if not path.is_dir():
            raise RunError(f"That folder does not exist: {path}")
        path = path.resolve()
        if len(path.parts) <= 1:
            raise RunError("Choose a folder, not a whole drive.")
        return path

    def start(self, workspace: Any, goal: Any, mode: Any = "default", allow: Any = None, max_turns: Any = 15,
              confirm_auto: bool = False) -> Dict[str, Any]:
        path = self.check_workspace(workspace)
        if not isinstance(goal, str) or not goal.strip():
            raise RunError("Write what the agent should do.")
        if len(goal) > GOAL_CHARS:
            raise RunError(f"The task is too long ({len(goal)} characters; at most {GOAL_CHARS}).")
        if mode not in MODES:
            raise RunError("The mode must be one of: " + ", ".join(MODES) + ".")
        if mode == "auto" and confirm_auto is not True:
            raise RunError("Automatic mode runs commands without asking. Confirm that you want that, in a folder you can lose.")
        rules = allow if isinstance(allow, list) else []
        if len(rules) > MAX_ALLOW or any(not isinstance(r, str) or not r.strip() or len(r) > 200 or "\n" in r for r in rules):
            raise RunError(f"Allow rules are lines of text, at most {MAX_ALLOW}.")
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 50:
            raise RunError("The number of steps must be between 1 and 50.")

        with self._lock:
            if any(r["status"] == "running" for r in self.runs):
                raise RunBusy("An agent is already running. Stop it or wait for it to finish.")
            run_id = secrets.token_hex(3)
            log_path = self._log_path(run_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            command = [self.python, "-m", "harness", f"--prompt={goal.strip()}", "--workspace", str(path), "--endpoint", self.endpoint,
                       "--permission-mode", mode, "--max-turns", str(max_turns), "--json", "--run-id", run_id, "--ask-server"]
            for rule in rules:
                command += ["--allow", rule.strip()]
            env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_KEYS}
            env.update({"PYTHONUTF8": "1", "PYTHONPATH": str(self.root)})
            if self.api_key:
                env["BASALT_API_KEY"] = self.api_key
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
            with open(log_path, "wb") as log:
                try:
                    proc = subprocess.Popen(command, cwd=str(self.root), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                            stderr=subprocess.STDOUT, creationflags=flags, start_new_session=(os.name != "nt"))
                except OSError as exc:
                    raise RunError(f"The agent could not be started: {exc.strerror or exc}") from None
            contain(proc)
            run = {"id": run_id, "workspace": str(path), "goal": goal.strip()[:SHOWN_CHARS], "mode": mode, "status": "running",
                   "started": time.time(), "ended": None, "exit_code": None, "summary": "", "stop_reason": "", "pid": proc.pid,
                   "allow": [r.strip() for r in rules]}
            self.runs.append(run)
            self._procs[run_id] = proc
            self._save()
        threading.Thread(target=self._watch, args=(run_id, proc), daemon=True).start()
        return dict(run)

    def stop(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            run = next((r for r in self.runs if r["id"] == run_id), None)
            proc = self._procs.get(run_id)
            if run is None:
                raise RunError("That run is not in the list any more.")
            if run["status"] != "running" or proc is None:
                raise RunError("That run is not running.")
            run["stop_requested"] = True
        _kill_tree(proc)
        return self.get(run_id) or {}

    def _watch(self, run_id: str, proc: "subprocess.Popen[bytes]") -> None:
        code = proc.wait()
        try:
            text = self._log_path(run_id).read_bytes()[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
        except OSError:
            text = ""
        result = _final_json(text)
        with self._lock:
            run = next((r for r in self.runs if r["id"] == run_id), None)
            self._procs.pop(run_id, None)
            if run is None:
                return
            stopped = bool(run.pop("stop_requested", False))
            run["ended"], run["exit_code"] = time.time(), code
            run["stop_reason"] = str(result.get("stop_reason") or "")
            run["summary"] = str(result.get("final_output") or result.get("error") or "")[:SHOWN_CHARS]
            if stopped:
                run["status"] = "stopped"
            elif code == 0:
                run["status"] = "done"
            else:
                run["status"] = "failed"
                run["summary"] = run["summary"] or _last_line(text)
            self._save()
        if self.board:
            self.board.refuse_all(run_id)
        self._close_task(run_id, stopped)

    def _close_task(self, run_id: str, stopped: bool) -> None:
        """The agent reports its own end; when it could not (it was killed or crashed) the task is closed here."""
        if self.tasks is None:
            return
        task_id = f"agent-{run_id}"
        task = next((t for t in self.tasks.load().get("tasks", []) if t.get("id") == task_id), None)
        if task is not None and task.get("status") in OPEN_STATES:
            self.tasks.record(task_id, "closed" if stopped else "failed",
                              "Stopped from the page." if stopped else "The agent ended without saying how.")


def _last_line(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1][:SHOWN_CHARS] if lines else ""
