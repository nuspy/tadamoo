"""Commands that run on after the call that started them: `shell_command` with `background: true` returns a job at once, `job_status`,
`job_log` and `job_kill` follow it, and the harness tells the model in the next tool result when a job ends.

A job is one command run by the same shell tool in a thread of its own, so every check of that tool applies to it. Its output is
whole once it ends (or is ended with `job_kill`)."""
from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from harness.sandbox import cancel_scope

TAIL_CHARS = 4000
KEPT = 50                               # finished jobs remembered per conversation


class Job:
    def __init__(self, job_id: str, command: str) -> None:
        self.id, self.command = job_id, command
        self.started = time.monotonic()
        self.ended: Optional[float] = None
        self.cancel = threading.Event()
        self.result: Optional[Dict[str, Any]] = None
        self.told = False

    @property
    def state(self) -> str:
        if self.ended is None:
            return "running"
        return "killed" if self.cancel.is_set() else "finished"

    def seconds(self) -> float:
        return round((self.ended or time.monotonic()) - self.started, 1)


class Jobs:
    def __init__(self, run: Callable[[str, str], Dict[str, Any]]) -> None:
        """`run(command, shell)` runs a command to its end and returns the shell tool's result."""
        self._run = run
        self._jobs: Dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, command: str, shell: str = "cmd") -> Job:
        job = Job(f"job{next(self._ids)}", command)
        with self._lock:
            self._jobs[job.id] = job
            for old in [j for j in list(self._jobs.values()) if j.ended is not None and j.told][:-KEPT]:
                self._jobs.pop(old.id, None)

        def work() -> None:
            try:
                with cancel_scope(job.cancel):
                    job.result = self._run(command, shell)
            except Exception as exc:  # a job that fails to run is a result, not an error for the loop
                job.result = {"exit_code": -1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "success": False}
            job.ended = time.monotonic()

        threading.Thread(target=work, name=f"basalt-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(str(job_id))
        if job is None:
            raise KeyError(f"There is no job '{job_id}'. Known: {', '.join(self._jobs) or 'none'}")
        return job

    def all(self) -> List[Job]:
        return list(self._jobs.values())

    def news(self) -> str:
        """One line for each job that ended since the last call, or ''."""
        lines = []
        for job in self.all():
            if job.ended is not None and not job.told:
                job.told = True
                code = (job.result or {}).get("exit_code")
                lines.append(f"[harness] background job {job.id} {job.state} after {job.seconds()} s (exit code {code}): {job.command[:80]}. Read it with job_log.")
        return "\n".join(lines)


def register_job_tools(registry: Any, jobs: Jobs) -> None:
    from harness.tools.registry import EffectClass

    def view(job: Job) -> Dict[str, Any]:
        out: Dict[str, Any] = {"job": job.id, "state": job.state, "seconds": job.seconds(), "command": job.command[:200]}
        if job.result is not None:
            out["exit_code"] = job.result.get("exit_code")
        return out

    def job_status(job: str = "") -> Any:
        if job:
            return view(jobs.get(job))
        return {"jobs": [view(j) for j in jobs.all()]}

    def job_log(job: str, tail: int = TAIL_CHARS) -> Any:
        found = jobs.get(job)
        if found.result is None:
            return {**view(found), "note": "still running: its output is whole when it ends"}
        text = (found.result.get("stdout") or "") + (("\n[stderr]\n" + found.result["stderr"]) if found.result.get("stderr") else "")
        tail = max(200, min(int(tail), 50_000))
        return {**view(found), "output": text[-tail:], "cut": len(text) > tail}

    def job_kill(job: str) -> Any:
        found = jobs.get(job)
        if found.ended is None:
            found.cancel.set()
        return view(found)

    registry.register("job_status", "Shows one background job (give its id) or all of them: running, finished or killed, how long, and the exit code.",
                      {"type": "object", "properties": {"job": {"type": "string", "description": "Job id; empty for all"}}}, EffectClass.PURE, job_status)
    registry.register("job_log", "Reads the output of a background job: the last part of it, once the job has ended.",
                      {"type": "object", "properties": {"job": {"type": "string"}, "tail": {"type": "integer", "description": f"Most characters from the end (default {TAIL_CHARS})"}},
                       "required": ["job"]}, EffectClass.PURE, job_log)
    registry.register("job_kill", "Ends a background job and everything it started.",
                      {"type": "object", "properties": {"job": {"type": "string"}}, "required": ["job"]}, EffectClass.REVERSIBLE, job_kill)
