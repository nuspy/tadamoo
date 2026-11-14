"""One background job at a time (an installation can take an hour), with what the page shows: the step, the skill, the last lines of what the installers print.

A job runs `work(job)` on a thread. `job.say(line)` adds a line, `job.run(argv)` runs a program and streams its output into the lines (the installers print their
own progress), and a cancel stops the program that is running and the work after it. The last job stays readable until the next one starts."""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.importer.common import Problem

KEEP_LINES = 40


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, what: str, names: Sequence[str], echo: Optional[Callable[[str], None]] = None) -> None:
        self.echo = echo                       # where each line is also written (the shell prints them as they come)
        self.id = uuid.uuid4().hex[:10]
        self.what, self.names = what, list(names)
        self.state = "running"                 # running | done | failed | cancelled
        self.phase = ""                        # copy | models
        self.current = ""                      # the skill being worked on
        self.step, self.steps = 0, 0
        self.lines: List[str] = []
        self.started, self.ended = time.time(), 0.0
        self.error = ""
        self.result: Dict[str, Any] = {}
        self._cancel = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def say(self, line: str) -> None:
        line = " ".join(str(line).split())
        if line:
            with self._lock:
                self.lines = (self.lines + [line[:300]])[-KEEP_LINES:]
            if self.echo is not None:
                self.echo(line[:300])

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def run(self, argv: Sequence[str]) -> int:
        """Runs a program, its output going to the lines; a cancel ends it."""
        self.check()
        proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        self._proc = proc
        try:
            for line in proc.stdout or []:
                self.say(line)
            return proc.wait()
        finally:
            self._proc = None
            if proc.poll() is None:
                self._kill(proc)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.kill()
            proc.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc is not None:
            self._kill(proc)

    def view(self) -> Dict[str, Any]:
        with self._lock:
            return {"id": self.id, "what": self.what, "names": self.names, "state": self.state, "phase": self.phase, "current": self.current, "step": self.step, "steps": self.steps,
                    "lines": list(self.lines), "line": self.lines[-1] if self.lines else "", "started": self.started, "ended": self.ended, "error": self.error, "result": self.result}


class Jobs:
    def __init__(self) -> None:
        self._job: Optional[Job] = None
        self._lock = threading.Lock()

    def view(self) -> Optional[Dict[str, Any]]:
        job = self._job
        return job.view() if job is not None else None

    def running(self) -> bool:
        job = self._job
        return job is not None and job.state == "running"

    def start(self, what: str, names: Sequence[str], work: Callable[[Job], Dict[str, Any]], wait: bool = False) -> Job:
        with self._lock:
            if self.running():
                raise Problem("busy", "Another installation is running. Wait for it to end, or stop it.")
            job = self._job = Job(what, names)
        thread = threading.Thread(target=self._go, args=(job, work), name="skills-job", daemon=True)
        thread.start()
        if wait:
            thread.join()
        return job

    @staticmethod
    def _go(job: Job, work: Callable[[Job], Dict[str, Any]]) -> None:
        try:
            job.result = work(job) or {}
            job.state = "done"
        except Cancelled:
            job.state = "cancelled"
        except Problem as exc:
            job.state, job.error = "failed", str(exc)
        except Exception as exc:                                      # whatever went wrong is shown, in one line, and the job ends
            job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
        job.ended = time.time()

    def cancel(self) -> bool:
        job = self._job
        if job is None or job.state != "running":
            return False
        job.cancel()
        return True
