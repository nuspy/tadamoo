"""A benchmark run: its own folder `<base>/runs/<run id>/` with settings.json, state.json, trials.jsonl (the experiments' journal and fingerprint, so an
interrupted run resumes), a stop flag and the files each trial leaves. The adapters (one per benchmark) plan the trials and run one at a time.

Trajectory files an adapter writes are JSON lines of events (see analysis.py)."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from tadamoo.eval import resume
from tadamoo.eval.resume import Journal, ResumeMismatch

from .registry import PROVISIONAL_LABEL, BenchSpec

RUNS = "runs"
SETTINGS, STATE, RESULTS, STOP = "settings.json", "state.json", "results.json", "stop.flag"
STATUSES = ("queued", "running", "done", "failed", "stopped")

Key = Tuple[Any, ...]


class AdapterNotWritten(Exception):
    """The benchmark is known but no adapter for it exists yet."""


@dataclass
class TrialResult:
    passed: Optional[bool] = None               # None: the trial could not be judged (the environment failed); it is redone on resume
    score: Optional[float] = None
    seconds: float = 0.0
    tokens: int = 0
    trajectory_path: str = ""                   # relative to the run folder, or absolute
    verifier_output_path: str = ""
    error: str = ""
    facts: Dict[str, Any] = field(default_factory=dict)         # what the adapter knows about a failure (for example judge_disagrees)

    @property
    def valid(self) -> bool:
        return self.passed is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TrialResult":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class TrialContext:
    run_dir: Path
    trial_dir: Path
    bench_dir: Path
    settings: Dict[str, Any]
    should_stop: Callable[[], bool]

    def relative(self, path: Path) -> str:
        try:
            return str(Path(path).relative_to(self.run_dir)).replace("\\", "/")
        except ValueError:
            return str(path)


class Adapter(Protocol):
    id: str

    def plan(self, settings: Mapping[str, Any], bench_dir: Path) -> List[Key]: ...
    def run_trial(self, key: Key, ctx: TrialContext) -> TrialResult: ...
    def score(self, results: Mapping[Key, TrialResult]) -> Dict[str, Any]: ...
    def failure_facts(self, result: TrialResult) -> Dict[str, Any]: ...


# -- adapters -----------------------------------------------------------------------------------------------------------

ADAPTERS: Dict[str, Callable[[], Adapter]] = {}          # benchmark id -> factory; an entry here wins over the module of the same benchmark


def get_adapter(spec_id: str, adapters: Optional[Mapping[str, Callable[[], Adapter]]] = None, *, lazy: bool = False) -> Adapter:
    """The adapter of a benchmark from `adapters` (default ADAPTERS). With `lazy`, a benchmark not in the table is looked for in the adapter modules
    (tadamoo/bench/adapters), imported only now."""
    table = ADAPTERS if adapters is None else adapters
    if spec_id not in table and lazy:
        from . import adapters as modules
        try:
            return modules.load(spec_id)()
        except modules.AdapterModuleMissing:
            pass
    if spec_id not in table:
        raise AdapterNotWritten(f"The adapter for {spec_id} is not written yet")
    return table[spec_id]()


class FakeAdapter:
    """A benchmark with no real work, for tests. `outcomes` maps a trial key to a dict (`passed`, `error`, `events`, `verifier`, `seconds`, `tokens`,
    `facts`); a key without an entry passes. Events and verifier text are written as the files a real adapter would leave."""

    def __init__(self, id: str = "fake", tasks: Sequence[str] = ("t1", "t2"), outcomes: Optional[Mapping[Key, Mapping[str, Any]]] = None,
                 metric: str = "resolution_rate", on_trial: Optional[Callable[[Key], None]] = None) -> None:
        self.id, self.tasks, self.outcomes, self.metric, self.on_trial = id, tuple(tasks), dict(outcomes or {}), metric, on_trial
        self.ran: List[Key] = []

    def plan(self, settings: Mapping[str, Any], bench_dir: Path) -> List[Key]:
        tasks = settings.get("tasks") or self.tasks
        return [(t, rep) for t in tasks for rep in range(1, int(settings.get("trials", 1)) + 1)]

    def run_trial(self, key: Key, ctx: TrialContext) -> TrialResult:
        self.ran.append(tuple(key))
        if self.on_trial:
            self.on_trial(tuple(key))
        o = dict(self.outcomes.get(tuple(key), {"passed": True}))
        ctx.trial_dir.mkdir(parents=True, exist_ok=True)
        res = TrialResult(passed=o.get("passed"), score=o.get("score"), seconds=float(o.get("seconds", 1.0)), tokens=int(o.get("tokens", 100)),
                          error=o.get("error", ""), facts=dict(o.get("facts", {})))
        if o.get("events") is not None:
            p = ctx.trial_dir / "trajectory.jsonl"
            p.write_text("\n".join(json.dumps(e) for e in o["events"]), encoding="utf-8")
            res.trajectory_path = ctx.relative(p)
        if o.get("verifier") is not None:
            p = ctx.trial_dir / "verifier.txt"
            p.write_text(o["verifier"], encoding="utf-8")
            res.verifier_output_path = ctx.relative(p)
        return res

    def score(self, results: Mapping[Key, TrialResult]) -> Dict[str, Any]:
        valid = [r for r in results.values() if r.valid]
        won = sum(1 for r in valid if r.passed)
        return {self.metric: won / len(valid) if valid else 0.0, "resolved": won, "of": len(valid), "invalid": len(results) - len(valid)}

    def failure_facts(self, result: TrialResult) -> Dict[str, Any]:
        return {"error": result.error, **result.facts}


# -- the run ------------------------------------------------------------------------------------------------------------

def key_text(key: Key) -> str:
    return "/".join(str(k) for k in key)


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:100]


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    for attempt in range(5):                       # a reader may hold the file for a moment on Windows
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def request_stop(run_dir: Path) -> None:
    Path(run_dir).joinpath(STOP).write_text("stop", encoding="utf-8")


class BenchRun:
    """One run. A new run writes its settings; a run whose folder exists (`run_id`) reads them back, so a resume uses what the first start recorded."""

    def __init__(self, base_dir: Path, spec: BenchSpec, settings: Mapping[str, Any], *, adapter: Adapter, model: Optional[Mapping[str, Any]] = None,
                 power_limit_w: Optional[float] = None, imported_commit: str = "unknown", run_id: Optional[str] = None,
                 clock: Callable[[], float] = time.time, root: Optional[Path] = None) -> None:
        self.base_dir, self.spec, self.adapter, self.clock, self.root = Path(base_dir), spec, adapter, clock, root
        self.bench_dir = self.base_dir / spec.id
        self.lock = threading.Lock()
        self.run_id = run_id or self._new_id()
        self.dir = self.base_dir / RUNS / self.run_id
        stored = read_json(self.dir / SETTINGS) if run_id else None
        if stored is not None:
            self.record = stored
        else:
            self.dir.mkdir(parents=True, exist_ok=True)
            provisional = not spec.comparable_with_board
            label = PROVISIONAL_LABEL if provisional else ""
            if float(settings.get("time_scale") or 1.0) > 1.0:           # more time than the task allows: not the board's rule
                provisional, label = True, f"time limit relaxed x{float(settings['time_scale']):g}: not comparable with the board"
            self.record = {"run_id": self.run_id, "benchmark": spec.id, "benchmark_commit": imported_commit, "model": dict(model or {}),
                           "power_limit_w": power_limit_w, "settings": dict(settings), "harness": resume.git_state(root or Path(__file__).resolve().parents[2]),
                           "provisional": provisional, "provisional_label": label,
                           "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(clock()))}
            write_json(self.dir / SETTINGS, self.record)
            self._state(status="queued", done=0, total=0, passed=0, failed=0, invalid=0, tokens=0, seconds=0.0, current="", error="")

    def _new_id(self) -> str:
        return f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.clock()))}-{self.spec.id}-{uuid.uuid4().hex[:4]}"

    # -- state and stop ---------------------------------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        return read_json(self.dir / STATE) or {}

    def _state(self, **changes: Any) -> Dict[str, Any]:
        with self.lock:
            now = read_json(self.dir / STATE) or {}
            now.update(changes)
            write_json(self.dir / STATE, now)
            return now

    def request_stop(self) -> None:
        request_stop(self.dir)

    def stop_requested(self) -> bool:
        return (self.dir / STOP).exists()

    def fingerprint(self, keys: Sequence[Key]) -> Dict[str, Any]:
        r = self.record
        return resume.make_fingerprint(experiment=f"bench:{r['benchmark']}", k=int(r["settings"].get("trials", 1)), arms=[{"name": "run", "settings": r["settings"]}],
                                       tasks=[key_text(k) for k in keys], sampling={}, model_file=(r["model"] or {}).get("file"), root=self.root,
                                       environment={"benchmark_commit": r["benchmark_commit"], "power_limit_w": r["power_limit_w"], "model": r["model"],
                                                    "provisional": r["provisional"]})

    # -- running ----------------------------------------------------------------------------------------------------

    def execute(self) -> Dict[str, Any]:
        """Runs (or resumes) the whole run and returns the final state. Never raises: a failure is recorded in the state."""
        (self.dir / STOP).unlink(missing_ok=True)
        self._state(status="running", error="", started=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.clock())))
        try:
            keys = [tuple(k) for k in self.adapter.plan(self.record["settings"], self.bench_dir)]
            journal = Journal(self.dir, self.fingerprint(keys))
        except ResumeMismatch as exc:
            return self._state(status="failed", error=str(exc))
        except Exception as exc:
            return self._state(status="failed", error=f"{type(exc).__name__}: {exc}")
        results: Dict[Key, TrialResult] = {}
        self._state(total=len(keys))
        stopped = False
        for key in keys:
            if self.stop_requested():
                stopped = True
                break
            cached = journal.get(key)
            if cached is not None:
                results[key] = TrialResult.from_dict(cached)
            else:
                self._state(current=key_text(key))
                results[key] = self._trial(key, journal)
            self._count(results)
        self._state(current="")
        if stopped:
            return self._state(status="stopped")
        try:
            metrics = self.adapter.score(results)
        except Exception as exc:
            return self._state(status="failed", error=f"scoring failed: {type(exc).__name__}: {exc}")
        write_json(self.dir / RESULTS, {"metrics": metrics, "provisional": self.record["provisional"], "provisional_label": self.record["provisional_label"],
                                         "finished": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.clock()))})
        return self._state(status="done")

    def _trial(self, key: Key, journal: Journal) -> TrialResult:
        ctx = TrialContext(self.dir, self.dir / "trials" / slug(key_text(key)), self.bench_dir, dict(self.record["settings"]), self.stop_requested)
        t0 = time.monotonic()
        try:
            result = self.adapter.run_trial(key, ctx)
        except Exception as exc:
            result = TrialResult(error=f"adapter crashed: {type(exc).__name__}: {exc}")
        if not result.seconds:
            result.seconds = time.monotonic() - t0
        journal.add(key, result.to_dict(), result.valid)
        return result

    def _count(self, results: Mapping[Key, TrialResult]) -> None:
        rs = list(results.values())
        self._state(done=len(rs), passed=sum(1 for r in rs if r.passed), failed=sum(1 for r in rs if r.passed is False), invalid=sum(1 for r in rs if not r.valid),
                    tokens=sum(r.tokens for r in rs), seconds=round(sum(r.seconds for r in rs), 3))

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.execute, name=f"bench-{self.run_id}", daemon=True)
        t.start()
        return t


def read_trials(run_dir: Path) -> Dict[Key, Tuple[bool, TrialResult]]:
    """The latest line of each trial in a run folder: key -> (valid, result). The fingerprint line is skipped."""
    out: Dict[Key, Tuple[bool, TrialResult]] = {}
    try:
        lines = (Path(run_dir) / resume.FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines[1:]:
        try:
            row = json.loads(line)
            out[tuple(row["key"])] = (bool(row.get("valid")), TrialResult.from_dict(row["trial"]))
        except (ValueError, KeyError, TypeError):
            continue
    return out
