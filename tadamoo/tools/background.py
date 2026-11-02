"""Commands that run on after the call that started them: `shell_command` with `background: true` returns a job at once, `job_status`,
`job_log` and `job_kill` follow it, and the harness tells the model in the next tool result when a job ends.

A job is one command run by the same shell tool in a thread of its own, so every check of that tool applies to it. The output is kept while it runs: `job_log` reads what is
there, and `sink` (the conversation's chat) sees it as it is written."""
from __future__ import annotations

import codecs
import itertools
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from tadamoo.sandbox import cancel_scope

TAIL_CHARS = 4000
LIVE_CHARS = 200_000                    # the most of a job's output kept while it runs
FLUSH_S = 0.25                          # output reaches the sink at most this often
KEPT = 50                               # finished jobs remembered per conversation


class Job:
    def __init__(self, job_id: str, command: str) -> None:
        self.id, self.command = job_id, command
        self.started = time.monotonic()
        self.ended: Optional[float] = None
        self.cancel = threading.Event()
        self.result: Optional[Dict[str, Any]] = None
        self.told = False
        self.live = ""                    # what the job has written so far, both streams, in the order it came
        self._pending: List[Any] = []
        self._flushed = 0.0
        self._decoders: Dict[str, Any] = {}

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
        self.sink: Optional[Callable[[Dict[str, Any]], None]] = None      # sees {"job", "command", "state"} and {"job", "stream", "text"} as they happen
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
            self._say({"job": job.id, "command": command[:200], "state": "running"})
            try:
                with cancel_scope(job.cancel, tap=lambda stream, data: self._tap(job, stream, data)):
                    job.result = self._run(command, shell)
            except Exception as exc:  # a job that fails to run is a result, not an error for the loop
                job.result = {"exit_code": -1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "success": False}
            job.ended = time.monotonic()
            self._flush(job, force=True)
            self._say({"job": job.id, "command": command[:200], "state": job.state, "exit_code": job.result.get("exit_code"), "seconds": job.seconds()})

        threading.Thread(target=work, name=f"basalt-{job.id}", daemon=True).start()
        return job

    def _say(self, event: Dict[str, Any]) -> None:
        if self.sink is not None:
            try:
                self.sink(event)
            except Exception:  # a sink that fails must not stop the job
                pass

    def _tap(self, job: Job, stream: str, data: bytes) -> None:
        decoder = job._decoders.setdefault(stream, codecs.getincrementaldecoder("utf-8")("replace"))
        text = decoder.decode(data)
        if not text:
            return
        job.live = (job.live + text)[-LIVE_CHARS:]
        job._pending.append((stream, text))
        self._flush(job)

    def _flush(self, job: Job, force: bool = False) -> None:
        now = time.monotonic()
        if not job._pending or not (force or now - job._flushed >= FLUSH_S):
            return
        pending, job._pending, job._flushed = job._pending, [], now
        for stream in ("out", "err"):
            text = "".join(t for s, t in pending if s == stream)
            if text:
                self._say({"job": job.id, "stream": stream, "text": text})

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
    from tadamoo.tools.registry import EffectClass

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
            return {**view(found), "note": "still running: this is the output so far", "output": found.live[-max(200, min(int(tail), 50_000)):]}
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
