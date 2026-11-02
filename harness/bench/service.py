"""The one object the server and the Settings tab use to work with benchmarks. Every method returns plain JSON-friendly values and reports a refusal as
`{"ok": False, "error": text}` instead of raising. Benchmarks run only with the served local model; runs execute in a background thread, one at a time."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

from . import analysis, importer, localmodel, preflight, publish, ranking, registry
from .importer import BenchImportError, Runner, default_runner
from .publish import PublishDesk, PublishError, Transport
from .registry import BenchSpec
from .runner import ADAPTERS, RUNS, AdapterNotWritten, BenchRun, get_adapter, read_json, request_stop

RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")
JUDGE_MODES = ("local", "none")                  # the local model stands in for a cloud judge, or the benchmark's judge-free mode is used


def _fail(text: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "error": text, **extra}


class BenchService:
    def __init__(self, base_dir: Union[str, Path], power_limit_source: Callable[[], Optional[float]], model_info_source: Callable[[], Mapping[str, Any]], *,
                 specs: Optional[Mapping[str, BenchSpec]] = None, adapters: Optional[Mapping[str, Callable[[], Any]]] = None, runner: Runner = default_runner,
                 probes: Optional[preflight.Probes] = None, gpu_busy: Optional[Callable[[], str]] = None, transports: Optional[Mapping[str, Transport]] = None,
                 clock: Callable[[], float] = time.time, root: Optional[Path] = None) -> None:
        self.base_dir = importer.ensure_outside_repo(Path(base_dir))
        self.power_limit_source, self.model_info_source = power_limit_source, model_info_source
        self.specs = dict(specs) if specs is not None else registry.SPECS
        self._adapters = adapters
        self.runner, self.clock, self.root = runner, clock, root
        self.probes = probes or preflight.Probes(runner=runner)
        if gpu_busy is not None:
            self.probes.gpu_busy = gpu_busy
        self.desk = PublishDesk(transports, self.specs, clock)
        self.threads: Dict[str, threading.Thread] = {}
        self.runs_live: Dict[str, BenchRun] = {}
        self.lock = threading.Lock()

    # -- helpers ----------------------------------------------------------------------------------------------------

    def _spec(self, spec_id: str) -> BenchSpec:
        if spec_id not in self.specs:
            raise KeyError(f"no benchmark called {spec_id!r}")
        return self.specs[spec_id]

    def _adapter(self, spec_id: str) -> Any:
        return get_adapter(spec_id, ADAPTERS if self._adapters is None else self._adapters, lazy=self._adapters is None)

    def _adapter_state(self, spec_id: str) -> Dict[str, Any]:
        try:
            self._adapter(spec_id)
            return {"available": True, "reason": ""}
        except AdapterNotWritten as exc:
            return {"available": False, "reason": str(exc)}

    def _run_dir(self, run_id: str) -> Optional[Path]:
        if not RUN_ID.match(run_id or ""):
            return None
        d = self.base_dir / RUNS / run_id
        return d if (d / "settings.json").is_file() else None

    def _active(self) -> Optional[str]:
        with self.lock:
            for rid, t in self.threads.items():
                if t.is_alive():
                    return rid
        return None

    # -- the benchmarks ---------------------------------------------------------------------------------------------

    def list_benchmarks(self) -> List[Dict[str, Any]]:
        return [{**s.to_dict(), "comparable_with_board": s.comparable_with_board, "status": importer.status(s, self.base_dir), "adapter": self._adapter_state(s.id)}
                for s in self.specs.values()]

    def install(self, spec_id: str) -> Dict[str, Any]:
        """Fetches the benchmark at the head of its default branch. Slow: the caller runs it off the request thread."""
        try:
            return {"ok": True, "meta": importer.import_benchmark(self._spec(spec_id), self.base_dir, self.runner, self.clock)}
        except (KeyError, BenchImportError) as exc:
            return _fail(str(exc.args[0]) if isinstance(exc, KeyError) else str(exc))

    def update(self, spec_id: str) -> Dict[str, Any]:
        try:
            return {"ok": True, "meta": importer.update(self._spec(spec_id), self.base_dir, self.runner, self.clock)}
        except (KeyError, BenchImportError) as exc:
            return _fail(str(exc.args[0]) if isinstance(exc, KeyError) else str(exc))

    def remove(self, spec_id: str) -> Dict[str, Any]:
        try:
            return {"ok": True, "removed": importer.remove(self._spec(spec_id), self.base_dir)}
        except (KeyError, BenchImportError) as exc:
            return _fail(str(exc.args[0]) if isinstance(exc, KeyError) else str(exc))

    def preflight(self, spec_id: str) -> Dict[str, Any]:
        try:
            spec = self._spec(spec_id)
        except KeyError as exc:
            return _fail(exc.args[0])
        checks = preflight.run_checks(spec, self.base_dir, self.probes)
        installed = importer.status(spec, self.base_dir)["installed"]
        checks.insert(0, preflight.check("installed", installed, "the benchmark is imported" if installed else "the benchmark has not been imported yet", "Press Import for this benchmark first."))
        checks.append(preflight.check("adapter", self._adapter_state(spec_id)["available"], "the adapter is written" if self._adapter_state(spec_id)["available"] else self._adapter_state(spec_id)["reason"],
                                      "Not possible yet: the adapter for this benchmark has to be written first."))
        return {"ok": True, "id": spec_id, "ready": preflight.ready(checks), "checks": checks}

    # -- runs -------------------------------------------------------------------------------------------------------

    def start_run(self, spec_id: str, settings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        settings = dict(settings or {})
        try:
            spec = self._spec(spec_id)
        except KeyError as exc:
            return _fail(exc.args[0])
        model = localmodel.from_info(self.model_info_source() or {})
        if not (model.name or model.file) or not model.endpoint:
            return _fail("The served model is not known (name, file and endpoint are needed), so the run cannot be labelled")
        why = localmodel.refusal(settings, model)
        if why:
            return _fail(why)
        judge = settings.setdefault("judge", "local")
        if judge not in JUDGE_MODES:
            return _fail(f"judge '{judge}' is not allowed; use 'local' (the served model stands in) or 'none' (the benchmark's judge-free mode)")
        power = self.power_limit_source()
        if power is None:
            return _fail("The GPU power limit is unknown; it must be recorded with every result")
        pre = self.preflight(spec_id)
        if not pre["ok"] or not pre["ready"]:
            return _fail("Not ready to start: " + "; ".join(c["detail"] for c in pre.get("checks", []) if c["required"] and not c["ok"]), checks=pre.get("checks", []))
        busy = self._active()
        if busy:
            return _fail(f"Run {busy} is still going; only one benchmark process runs at a time")
        meta = importer.read_meta(self.base_dir, spec) or {}
        run = BenchRun(self.base_dir, spec, settings, adapter=self._adapter(spec_id), model=model.to_dict(), power_limit_w=float(power),
                       imported_commit=meta.get("commit", "unknown"), clock=self.clock, root=self.root)
        return self._launch(run)

    def resume_run(self, run_id: str) -> Dict[str, Any]:
        d = self._run_dir(run_id)
        if d is None:
            return _fail("No such run")
        record = read_json(d / "settings.json") or {}
        try:
            spec = self._spec(record.get("benchmark", ""))
        except KeyError as exc:
            return _fail(exc.args[0])
        busy = self._active()
        if busy:
            return _fail(f"Run {busy} is still going; only one benchmark process runs at a time")
        gpu = preflight.gpu_check(self.probes.gpu_busy)
        if not gpu["ok"]:
            return _fail(gpu["detail"], checks=[gpu])
        try:
            adapter = self._adapter(spec.id)
        except AdapterNotWritten as exc:
            return _fail(str(exc))
        return self._launch(BenchRun(self.base_dir, spec, {}, adapter=adapter, run_id=run_id, clock=self.clock, root=self.root))

    def _launch(self, run: BenchRun) -> Dict[str, Any]:
        with self.lock:
            self.runs_live[run.run_id] = run
            self.threads[run.run_id] = run.start_background()
        return {"ok": True, "run_id": run.run_id}

    def wait(self, run_id: str, timeout: Optional[float] = None) -> None:
        """Waits for a run's thread (tests and shutdown)."""
        t = self.threads.get(run_id)
        if t is not None:
            t.join(timeout)

    def runs(self) -> List[Dict[str, Any]]:
        out = []
        for d in sorted((self.base_dir / RUNS).glob("*")) if (self.base_dir / RUNS).is_dir() else []:
            record = read_json(d / "settings.json")
            if record is None:
                continue
            st = read_json(d / "state.json") or {}
            out.append({"run_id": d.name, "benchmark": record.get("benchmark"), "status": st.get("status", "unknown"), "created": record.get("created"),
                        "done": st.get("done", 0), "total": st.get("total", 0), "provisional": record.get("provisional", False)})
        return sorted(out, key=lambda r: str(r["created"]), reverse=True)

    def run_state(self, run_id: str) -> Dict[str, Any]:
        d = self._run_dir(run_id)
        if d is None:
            return _fail("No such run")
        record = read_json(d / "settings.json") or {}
        st = read_json(d / "state.json") or {}
        t = self.threads.get(run_id)
        return {"ok": True, "run_id": run_id, **st, "benchmark": record.get("benchmark"), "settings": record.get("settings"), "model": record.get("model"),
                "power_limit_w": record.get("power_limit_w"), "benchmark_commit": record.get("benchmark_commit"), "provisional": record.get("provisional", False),
                "provisional_label": record.get("provisional_label", ""), "running": bool(t and t.is_alive())}

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        d = self._run_dir(run_id)
        if d is None:
            return _fail("No such run")
        t = self.threads.get(run_id)
        request_stop(d)
        return {"ok": True, "stopping": bool(t and t.is_alive())}

    # -- results ----------------------------------------------------------------------------------------------------

    def report(self, run_id: str) -> Dict[str, Any]:
        d = self._run_dir(run_id)
        if d is None:
            return _fail("No such run")
        record = read_json(d / "settings.json") or {}
        try:
            adapter = self._adapter(record.get("benchmark", ""))
        except AdapterNotWritten:
            adapter = None
        return {"ok": True, **analysis.write_report(d, adapter)}

    def ranking(self, run_id: str, leaderboard_text: Optional[str] = None) -> Dict[str, Any]:
        """The preview of the place before publishing. `leaderboard_text` is the downloaded results file (the service reaches no network)."""
        d = self._run_dir(run_id)
        if d is None:
            return _fail("No such run")
        record = read_json(d / "settings.json") or {}
        results = read_json(d / "results.json")
        if results is None:
            return _fail("The run has not finished")
        spec = self._spec(record["benchmark"])
        metrics = results.get("metrics") or {}
        if spec.primary_metric not in metrics:
            return _fail(f"The results hold no '{spec.primary_metric}' metric")
        rep = analysis.report(d)
        entries = ranking.load_csv(leaderboard_text) if leaderboard_text else []
        valid = rep["trials"] - rep["invalid"]
        return {"ok": True, **ranking.simulate(metrics, entries, spec.primary_metric, official_judge=not record.get("provisional"),
                                              successes=rep["passed"], trials=valid)}

    # -- publishing -------------------------------------------------------------------------------------------------

    def _publish_kind(self, run_id: str) -> Union[str, Dict[str, Any]]:
        d = self._run_dir(run_id)
        if d is None:
            return _fail("No such run")
        return self._spec((read_json(d / "settings.json") or {})["benchmark"]).leaderboard

    def prepare_publish(self, run_id: str) -> Dict[str, Any]:
        kind = self._publish_kind(run_id)
        if isinstance(kind, dict):
            return kind
        try:
            return {"ok": True, **publish.prepare_package(self._run_dir(run_id), kind, self.specs)}
        except PublishError as exc:
            return _fail(str(exc))

    def begin_publish(self, run_id: str, destination: Union[str, Mapping[str, Any]]) -> Dict[str, Any]:
        d = self._run_dir(run_id)
        if d is None:
            return _fail("No such run")
        try:
            return {"ok": True, **self.desk.begin_publish(d, destination)}
        except PublishError as exc:
            return _fail(str(exc))

    def finish_publish(self, token: str, phrase: str) -> Dict[str, Any]:
        try:
            return {"ok": True, **self.desk.finish_publish(token, phrase)}
        except PublishError as exc:
            return _fail(str(exc), error_type=type(exc).__name__)
