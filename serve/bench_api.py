"""The Benchmarks tab's side: the benchmarks with their status, preflight, runs, reports, rank preview and the two-step publishing, all through the
harness `BenchService`. Only the server's own page reaches it. Importing, updating and starting take minutes, so they run in a thread and the page
polls the overview; every other action answers at once. Benchmarks run only with the model the server has loaded; nothing here reaches the network
except the benchmark's own import (git) and the commands the preflight runs."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tadamoo.bench.service import BenchService
from tadamoo.lessons import LessonStore

ACTIONS = ("install", "update", "remove", "start", "stop", "resume", "ranking", "add_lesson", "prepare_publish", "begin_publish", "finish_publish")
THREADED = ("install", "update", "start")
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
BENCH_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,60}$")
TRIALS_MAX = 20
ATTEMPTS_MAX = 100000                 # the most failed attempts a task may use when the person sets a limit; 0 is no limit
POWER_RANGE = (50.0, 1500.0)
POWER_CACHE_S = 60.0
CSV_MAX = 1 << 20
PHRASE_MAX = 200
REASONING_LEVELS = ("low", "medium", "high")      # how long the model thinks; not given: the model's own default
TASK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
TASKS_MAX = 200                       # the most task names one run may name; none named: the whole subset
SETTINGS_KEYS = ("subset", "trials", "max_failed_attempts", "reasoning", "tasks")
ERROR_TYPES = {"BadConfirmation": "confirmation", "NotConfigured": "not_configured", "PackageChanged": "package_changed", "NotOffered": "not_offered"}


class BenchApiError(ValueError):
    def __init__(self, message: str, kind: str = "bench", status: int = 400) -> None:
        super().__init__(message)
        self.kind, self.status = kind, status


def _text(value: Any, name: str, pattern: "re.Pattern[str]") -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise BenchApiError(f"'{name}' is missing or not valid")
    return value


class BenchApi:
    """`power_source` reads the GPU power limit (None when unknown), `model_source` the model the server has loaded (None when none), `busy_source` why the
    GPU is in use ("" when it is not). A service can be passed in; otherwise one is built over `base_dir`."""

    def __init__(self, base_dir: "str | Path", power_source: Callable[[], Optional[float]], model_source: Callable[[], Optional[Dict[str, Any]]],
                 busy_source: Callable[[], str], lessons_dir: "str | Path", service: Optional[BenchService] = None, clock: Callable[[], float] = time.time) -> None:
        self.power_source, self.model_source, self.busy_source, self.clock = power_source, model_source, busy_source, clock
        self.lessons_dir = Path(lessons_dir)
        self.typed_power: Optional[float] = None
        self._power_cache: Optional[tuple] = None
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.service = service or BenchService(base_dir, self.power, lambda: self.model_source() or {}, gpu_busy=busy_source)

    # -- what the machine says --------------------------------------------------------------------------------------

    def measured_power(self) -> Optional[float]:
        now = self.clock()
        if self._power_cache is None or now - self._power_cache[0] > POWER_CACHE_S:
            try:
                value = self.power_source()
            except Exception:
                value = None
            self._power_cache = (now, value if isinstance(value, (int, float)) and value > 0 else None)
        return self._power_cache[1]

    def power(self) -> Optional[float]:
        """The measured limit; when it cannot be read, the one the person typed."""
        return self.measured_power() if self.measured_power() is not None else self.typed_power

    def overview(self) -> Dict[str, Any]:
        with self.lock:
            jobs = {k: dict(v) for k, v in self.jobs.items()}
        try:
            busy = self.busy_source() or ""
        except Exception as exc:
            busy = f"the GPU state could not be read ({type(exc).__name__})"
        return {"benchmarks": self.service.list_benchmarks(), "runs": self.service.runs(), "jobs": jobs, "model": self.model_source(),
                "power_limit_w": self.power(), "power_limit_measured": self.measured_power() is not None, "gpu_busy": busy}

    # -- reading ----------------------------------------------------------------------------------------------------

    def get(self, what: str, query: Dict[str, str]) -> Dict[str, Any]:
        if what == "overview":
            return self.overview()
        key = query.get("id", "")
        if what == "preflight":
            return self._ok(self.service.preflight(_text(key, "id", BENCH_ID)))
        run_id = _text(key, "id", RUN_ID)
        if what == "run":
            return self._ok(self.service.run_state(run_id))
        if what == "report":
            return self._ok(self.service.report(run_id))
        if what == "ranking":
            return self._ok(self.service.ranking(run_id))
        raise BenchApiError("unknown request", "not_found", 404)

    # -- acting -----------------------------------------------------------------------------------------------------

    def post(self, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        if action not in ACTIONS:
            raise BenchApiError("unknown action", "not_found", 404)
        if action in THREADED:
            return self._threaded(action, req)
        if action == "remove":
            bench = _text(req.get("id"), "id", BENCH_ID)
            running = [r for r in self.service.runs() if r["benchmark"] == bench and r["status"] in ("queued", "running")]
            if running or self._job_running(bench):
                raise BenchApiError("a run or an import of this benchmark is going on; stop it first", "busy", 409)
            return self._ok(self.service.remove(bench))
        if action == "stop":
            return self._ok(self.service.stop_run(_text(req.get("id"), "id", RUN_ID)))
        if action == "resume":
            return self._ok(self.service.resume_run(_text(req.get("id"), "id", RUN_ID)))
        if action == "ranking":
            text = req.get("leaderboard_text")
            if text is not None and (not isinstance(text, str) or len(text) > CSV_MAX):
                raise BenchApiError("the pasted results file must be text of at most 1 MB")
            return self._ok(self.service.ranking(_text(req.get("id"), "id", RUN_ID), text or None))
        if action == "add_lesson":
            return self._add_lesson(req)
        return self._publish(action, req)

    def _publish(self, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        if action == "finish_publish":
            token, phrase = req.get("token"), req.get("phrase")
            if not isinstance(token, str) or not token or len(token) > 200 or not isinstance(phrase, str) or len(phrase) > PHRASE_MAX:
                raise BenchApiError("send the token and the phrase you typed")
            return self._ok(self.service.finish_publish(token, phrase))
        run_id = _text(req.get("id"), "id", RUN_ID)
        if action == "prepare_publish":
            return self._ok(self.service.prepare_publish(run_id))
        destination = req.get("destination")
        if not isinstance(destination, str) or not destination:
            raise BenchApiError("name the destination")
        return self._ok(self.service.begin_publish(run_id, destination))

    def _add_lesson(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Keeps one candidate lesson of a run as a global lesson. Only on an explicit request that names the run and the candidate."""
        run_id, index = _text(req.get("id"), "id", RUN_ID), req.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise BenchApiError("send the number of the candidate lesson")
        report = self._ok(self.service.report(run_id))
        candidates: List[Dict[str, Any]] = report.get("candidate_lessons") or []
        if index >= len(candidates):
            raise BenchApiError("that candidate lesson is not there any more", "not_found", 404)
        result = LessonStore(self.lessons_dir, self.lessons_dir).add_candidate(candidates[index], scope="global")
        if not result.ok:
            raise BenchApiError(result.reason or "the lesson was not kept")
        return {"ok": True, "action": result.action, "id": result.id}

    @staticmethod
    def _ok(result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("ok") is False:
            raise BenchApiError(str(result.get("error") or "refused"), ERROR_TYPES.get(str(result.get("error_type")), "bench"))
        return result

    # -- the long ones, in a thread ---------------------------------------------------------------------------------

    def _job_running(self, key: str) -> bool:
        with self.lock:
            return self.jobs.get(key, {}).get("status") == "running"

    def _threaded(self, action: str, req: Dict[str, Any]) -> Dict[str, Any]:
        bench = _text(req.get("id"), "id", BENCH_ID)
        settings: Dict[str, Any] = {}
        if action == "start":
            settings = self._start_settings(bench, req)
        with self.lock:
            if any(j.get("status") == "running" for j in self.jobs.values()):
                raise BenchApiError("another import, update or start is still going; wait until it has finished", "busy", 409)
            self.jobs[bench] = {"action": action, "status": "running", "started": self.clock(), "finished": None, "error": "", "checks": [], "run_id": "", "meta": None}
        threading.Thread(target=self._job, args=(action, bench, settings), name=f"benchjob-{action}-{bench}", daemon=True).start()
        return {"ok": True, "started": True, "id": bench, "action": action}

    def _start_settings(self, bench: str, req: Dict[str, Any]) -> Dict[str, Any]:
        spec = next((b for b in self.service.list_benchmarks() if b["id"] == bench), None)
        if spec is None:
            raise BenchApiError("no such benchmark", "not_found", 404)
        if not self.model_source():
            raise BenchApiError("No model is loaded. Load the model in the Loader first: benchmarks run only with the model this server serves.", "no_model", 409)
        subset, trials = req.get("subset"), req.get("trials", 1)
        if not isinstance(subset, str) or subset not in {s["id"] for s in spec["subsets"]}:
            raise BenchApiError("choose one of the subsets of this benchmark")
        if not isinstance(trials, int) or isinstance(trials, bool) or not 1 <= trials <= TRIALS_MAX:
            raise BenchApiError(f"the number of trials is a whole number from 1 to {TRIALS_MAX}")
        attempts = req.get("max_failed_attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= ATTEMPTS_MAX:
            raise BenchApiError(f"the failed attempts allowed per task are a whole number from 0 (no limit) to {ATTEMPTS_MAX}")
        reasoning = req.get("reasoning", "")
        if reasoning not in ("", *REASONING_LEVELS):
            raise BenchApiError(f"the reasoning level is one of: {', '.join(REASONING_LEVELS)}, or left empty for the model's own")
        tasks = req.get("tasks", [])
        if not isinstance(tasks, list) or len(tasks) > TASKS_MAX or not all(isinstance(t, str) and TASK_NAME.match(t) for t in tasks):
            raise BenchApiError(f"the tasks are a list of up to {TASKS_MAX} task names (letters, digits, dots, dashes), or empty for the whole subset")
        typed = req.get("power_limit_w")
        if typed is not None:
            if isinstance(typed, bool) or not isinstance(typed, (int, float)) or not POWER_RANGE[0] <= typed <= POWER_RANGE[1]:
                raise BenchApiError("the power limit is a number of watts between 50 and 1500")
            self.typed_power = float(typed)
        if self.power() is None:
            raise BenchApiError("The GPU power limit could not be read. Type it (in watts): every result is labelled with it.", "power_limit_unknown", 409)
        extra = {k: v for k, v in req.items() if k not in ("id", "power_limit_w", *SETTINGS_KEYS)}
        if extra:
            raise BenchApiError("only the subset, the number of trials, the failed attempts allowed, the reasoning level, the tasks and the power limit can be set: a benchmark runs only with the served model")
        return {"subset": subset, "trials": trials, **({"max_failed_attempts": attempts} if attempts else {}), **({"reasoning": reasoning} if reasoning else {}), **({"tasks": list(dict.fromkeys(tasks))} if tasks else {})}

    def _job(self, action: str, bench: str, settings: Dict[str, Any]) -> None:
        try:
            if action == "install":
                result = self.service.install(bench)
            elif action == "update":
                result = self.service.update(bench)
            else:
                result = self.service.start_run(bench, settings)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self.jobs[bench].update({"status": "done" if result.get("ok") else "failed", "finished": self.clock(), "error": result.get("error", ""),
                                     "checks": [c for c in result.get("checks", []) if not c.get("ok")], "run_id": result.get("run_id", ""),
                                     "meta": result.get("meta")})

    def wait(self, timeout: float = 10.0) -> None:
        """Waits for the threaded jobs (tests)."""
        end = time.time() + timeout
        while time.time() < end and any(t.name.startswith("benchjob-") and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.01)
