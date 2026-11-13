"""The long jobs of the knowledge archives: reading a big document takes minutes, so the request that adds it returns a job at once and the page asks
how it goes. A job runs in a thread of its own, reports its stage and its page, can be cancelled, and leaves the archive as it was when it fails."""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from tadamoo.rag.errors import RagError

KEPT = 40                          # finished jobs that are remembered
Work = Callable[[Callable[[Dict[str, Any]], None], threading.Event], Dict[str, Any]]


class Jobs:
    def __init__(self) -> None:
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._cancel: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, label: str, work: Work, archive: str = "") -> Dict[str, Any]:
        """Runs `work(progress, cancel)` in the background; `work` returns the result of the job (a dict) or raises RagError."""
        jid = secrets.token_hex(5)
        cancel = threading.Event()
        job = {"id": jid, "label": label, "archive": archive, "state": "running", "stage": "", "page": 0, "pages": 0, "calls": 0, "tokens": 0, "device": "", "data": {}, "result": None, "error": "",
               "started": time.time(), "ended": 0.0}

        def progress(event: Dict[str, Any]) -> None:
            with self._lock:
                if event.get("event") == "stage":
                    job["stage"] = str(event.get("stage") or "")
                elif event.get("event") == "data":
                    job["data"] = event.get("data") or {}
                else:
                    for key in ("page", "pages", "calls", "tokens"):
                        if event.get(key) is not None:                                  # a count of 0 starts a new count
                            job[key] = int(event[key])
                    if event.get("device") is not None:
                        job["device"] = str(event["device"])

        def run() -> None:
            try:
                result = work(progress, cancel)
                with self._lock:
                    job.update(state="done", result=result, ended=time.time())
            except RagError as e:
                with self._lock:
                    job.update(state={"cancelled": "cancelled", "paused": "paused"}.get(e.code, "failed"), error=str(e), ended=time.time())
            except Exception as e:                           # a reader's own failure is told, never lost
                with self._lock:
                    job.update(state="failed", error=f"{type(e).__name__}: {str(e)[:200]}", ended=time.time())

        thread = threading.Thread(target=run, name=f"rag-job-{jid}", daemon=True)
        with self._lock:
            self._jobs[jid], self._cancel[jid], self._threads[jid] = job, cancel, thread
            done = [k for k, j in self._jobs.items() if j["state"] != "running"]
            for k in done[:-KEPT]:
                self._jobs.pop(k, None)
                self._cancel.pop(k, None)
                self._threads.pop(k, None)
        thread.start()
        return self.get(jid)

    def get(self, jid: Any) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(jid) if isinstance(jid, str) else None
            if job is None:
                raise RagError("there is no such job", "not_found")
            return dict(job)

    def list(self, archive: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(j) for j in self._jobs.values() if archive is None or j["archive"] == archive]

    def cancel(self, jid: Any) -> Dict[str, Any]:
        self.get(jid)
        self._cancel[jid].set()
        return self.get(jid)

    def wait(self, jid: str, seconds: float = 60.0) -> Dict[str, Any]:
        """The job once it has ended (for tests and for callers that must have the result)."""
        thread = self._threads.get(jid)
        if thread is not None:
            thread.join(seconds)
        return self.get(jid)
