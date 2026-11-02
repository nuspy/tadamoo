"""ClawBench (TIGER-AI-Lab): a browser agent does everyday tasks on live websites inside a container; a request interceptor catches the final request and
a judge reads its body.

One trial is one `clawbench-run` of one case with one harness. Our harness is added to the imported ClawBench as a harness registry entry named `basalt`
(`clawbench_assets/`): the files go into the imported tree before the first trial, together with a copy of our `harness/` package and the few `serve/`
modules it imports, and ClawBench builds the image and starts the container. Inside, `basalt_driver.py` runs our headless agent against the container's
browser through `--browser-cdp-url`, so the page, the recorder and the interceptor are ClawBench's own. The model is the served local one, reached from the
container as `host.docker.internal`; the judge, when asked for, is the same local model reached from the host.

Settings read: `subset` (smoke, lite, v1, v2), `trials` (1; ClawBench scores one run per task), `judge` ('local': the served model judges the intercepted request
and the score is provisional; 'none': ClawBench's judge-free mode, where a pass is the interception), `harness` ('basalt', 'null' or 'random-click'; smoke
runs the two baselines), `tasks` (explicit case folder names), `limit` (the first N cases). Timeouts are the case's own and are never changed.

Smoke: the `null` harness (takes no action) and the `random-click` harness (clicks at random) on the first two Lite cases, judge-free. They prove Docker, the
image build, the browser, the recorder, the interceptor and the way results are read; the expected outcome is that nothing is intercepted. The model
server must be up, because ClawBench checks the model API before it starts a case."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from harness.bench.runner import Key, TrialContext, TrialResult

from ._common import clean_env, clip, model_key, read_lines, run_record, source_of, uv_argv, write_events
from .containerkit import local_endpoint
from .proc import LongRunner, long_runner

ID = "clawbench"
OWN_HARNESS = "basalt"
BASELINES = ("null", "random-click")
HARNESSES = (OWN_HARNESS,) + BASELINES
SUITES = {"smoke": "v1-lite", "lite": "v1-lite", "v1": "v1", "v2": "v2"}
SMOKE_CASES = 2
CASE_ALLOWANCE_S = 1800.0           # image build, the judge and the container's own start and stop, on top of the case's time limit
ASSETS = Path(__file__).resolve().parent / "clawbench_assets"
REPO_ROOT = Path(__file__).resolve().parents[3]
PAYLOAD = ("harness", "serve")
HARNESS_DIR = Path("src") / "clawbench" / "runtime" / "harnesses"
BEGIN, END = "  # basalt-begin", "  # basalt-end"
NOT_THE_MODEL = {"infra_failure", "api_or_credit", "task_data", "build_instruction"}
STOP_NAMES = {"time_limit_exceeded": "timeout", "agent_idle": "gave_up", "vnc_disconnected": "gave_up", "agent_exited": "final", "eval_matched": "final"}
EVENT_TYPES = {"tool_call", "tool_result", "plan", "stop", "compaction", "assistant", "error"}


# -- the cases ----------------------------------------------------------------------------------------------------------

def case_folder(source: Path, subset: str) -> Path:
    return Path(source) / "test-cases" / SUITES[subset]


def select_cases(source: Path, subset: str, settings: Optional[Mapping[str, Any]] = None) -> List[str]:
    """The case folder names of a subset, in name order (the numbers in front fix it); `smoke` is the first two of Lite."""
    if subset not in SUITES:
        raise ValueError(f"unknown subset {subset!r}; the subsets are {', '.join(SUITES)}")
    settings = settings or {}
    folder = case_folder(source, subset)
    names = sorted(p.name for p in folder.glob("*") if (p / "task.json").is_file()) if folder.is_dir() else []
    if not names:
        raise FileNotFoundError(f"no cases (folders with a task.json) found in {folder}; import the benchmark first")
    explicit = settings.get("tasks")
    if explicit:
        missing = [n for n in explicit if n not in names]
        if missing:
            raise ValueError(f"no such ClawBench case in {SUITES[subset]}: {', '.join(missing)}")
        return list(explicit)
    if subset == "smoke":
        return names[:SMOKE_CASES]
    limit = int(settings.get("limit") or 0)
    return names[:limit] if limit > 0 else names


def harnesses_for(subset: str, settings: Mapping[str, Any]) -> Tuple[str, ...]:
    chosen = settings.get("harness")
    if chosen:
        if chosen not in HARNESSES:
            raise ValueError(f"the harness is one of {', '.join(HARNESSES)}, not {chosen!r}")
        return (str(chosen),)
    return BASELINES if subset == "smoke" else (OWN_HARNESS,)


# -- adding our harness to the imported ClawBench -----------------------------------------------------------------------

def _ignore(directory: str, names: List[str]) -> List[str]:
    skip = [n for n in names if n == "__pycache__" or n.endswith(".pyc") or n == ".pytest_cache"]
    if Path(directory).name == "serve":                              # the web pages and the server's tests are not used by the harness
        skip += [n for n in names if n in ("web", "tests")]
    return skip


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def patch_registry(yaml_path: Path, entry: str) -> None:
    """Puts the harness entry in ClawBench's harnesses.yaml between two marker comments (replacing an earlier copy of it), so doing it twice changes nothing."""
    text = yaml_path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    block = f"{BEGIN}\n{entry.strip(chr(10))}\n{END}\n"
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        text = head + block + rest.split(END, 1)[1].lstrip("\n")
    else:
        text = text.rstrip("\n") + "\n" + block
    yaml_path.write_bytes(text.encode("utf-8"))
    try:
        import yaml
    except ModuleNotFoundError:
        return
    names = [h.get("name") for h in (yaml.safe_load(text) or {}).get("harnesses", [])]
    if OWN_HARNESS not in names:
        raise RuntimeError("ClawBench's harness registry does not list the basalt harness after it was added")


def install_harness(source: Path, repo_root: Path = REPO_ROOT, assets: Path = ASSETS) -> Path:
    """Copies the harness registry entry, its scripts and the payload (our `harness/` package and the `serve/` modules it imports) into the imported
    ClawBench. Returns the harness's folder. Safe to run again: what was copied before is replaced."""
    harnesses = Path(source) / HARNESS_DIR
    if not (harnesses / "harnesses.yaml").is_file():
        raise FileNotFoundError(f"{harnesses / 'harnesses.yaml'} is missing; this is not a ClawBench checkout")
    dest = harnesses / OWN_HARNESS
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for f in sorted((assets / OWN_HARNESS).glob("*")):
        if f.is_file():
            (dest / f.name).write_bytes(_lf(f.read_bytes()))         # the scripts run on Linux: line ends must be LF whatever the checkout here is
    for name in PAYLOAD:
        shutil.copytree(Path(repo_root) / name, dest / "payload" / name, ignore=_ignore)
    patch_registry(harnesses / "harnesses.yaml", (assets / "harnesses.entry.yaml").read_text(encoding="utf-8"))
    return dest


def models_yaml(model: Mapping[str, Any], gateway: str = "host.docker.internal") -> str:
    """ClawBench's models/models.yaml for the served model: one entry the container uses (through the host gateway) and one the judge on this machine
    uses. JSON is YAML. The key is a placeholder the engine does not check."""
    endpoint = local_endpoint(str(model.get("endpoint") or ""), model)
    port = urlparse(endpoint).port
    host_side = f"{endpoint}/v1"
    container_side = f"http://{gateway}:{port}/v1" if port else f"http://{gateway}/v1"
    key = model_key(model)
    return json.dumps({key: {"api_key": "local", "base_url": container_side, "api_type": "openai-completions"},
                       key + "-judge": {"api_key": "local", "base_url": host_side, "api_type": "openai-completions"}}, indent=2) + "\n"


# -- reading what ClawBench left ----------------------------------------------------------------------------------------

def find_run(out_dir: Path) -> Optional[Path]:
    """The folder of the newest run under the output folder: the one with a run-meta.json."""
    metas = sorted(Path(out_dir).rglob("run-meta.json"), key=lambda p: p.stat().st_mtime)
    return metas[-1].parent if metas else None


def _json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def trajectory_events(run: Path, stop_reason: str) -> List[Dict[str, Any]]:
    """The events of a run in the format of analysis.py: what our harness wrote, or the browser's actions as ClawBench recorded them, and an ending."""
    data = run / "data"
    events = [e for e in read_lines(data / "agent-messages.jsonl") if e.get("type") in EVENT_TYPES]
    if not events:
        events = [{"type": "tool_call", "name": str(a.get("type") or "action"), "args": {"url": str(a.get("url") or "")[:200]}, "decision": "executed"}
                  for a in read_lines(data / "actions.jsonl")]
    if not any(e.get("type") == "stop" for e in events):
        events.append({"type": "stop", "reason": STOP_NAMES.get(stop_reason, stop_reason or "final")})
    return events


def verifier_text(meta: Mapping[str, Any], interception: Mapping[str, Any], judge: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> str:
    """What the scoring said, in plain lines: the stop reason, the intercepted request, the judge's reason and what the agent last said."""
    lines = [f"stop reason: {interception.get('stop_reason') or 'none'}", f"stop description: {interception.get('stop_description') or ''}".rstrip(),
             f"intercepted: {bool(interception.get('intercepted'))}"]
    req = interception.get("request") or {}
    if req:
        lines.append(f"request: {req.get('method')} {req.get('url')}")
        if req.get("body"):
            lines.append("body: " + clip(json.dumps(req["body"], ensure_ascii=False) if not isinstance(req["body"], str) else req["body"], 1500))
    if judge:
        lines.append(f"judge: match={judge.get('match')} {clip(judge.get('reason') or judge.get('error') or '', 500)}")
    if meta.get("failure_category"):
        lines.append(f"failure category: {meta['failure_category']}")
    if meta.get("failure_reason"):
        lines.append(f"failure reason: {clip(meta['failure_reason'], 500)}")
    last = next((e.get("text") for e in reversed(events) if e.get("type") == "assistant" and e.get("text")), "")
    if last:
        lines.append("the agent last said: " + clip(last, 600))
    return "\n".join(l for l in lines if l.strip())


def pipeline_fault(meta: Mapping[str, Any], harness: str) -> str:
    """Why a baseline run proves nothing about the pipeline: a file the recorder should have written is missing, or the random-click harness recorded no action
    (the recorder is not attached to the browser). Empty for a run that is fine. The flags a do-nothing run raises on purpose (no model call, no action) are fine."""
    if harness not in BASELINES:
        return ""
    missing = [f for f in meta.get("infra_flags") or [] if str(f).startswith("missing:")]
    if missing:
        return "the recorder left out " + ", ".join(str(f)[8:] for f in missing)
    if harness == "random-click" and not int((meta.get("run_metrics") or {}).get("actions") or 0):
        return "the random-click baseline recorded no action: the recorder is not attached to the browser"
    return ""


def trial_result(run: Optional[Path], harness: str, judge_mode: str, ctx: TrialContext, process_note: str = "") -> TrialResult:
    """ClawBench's output of one case as ours. A case counts only when the model had its chance: a container, browser, model-API or task-data failure leaves it
    unjudged, so it is run again on resume. For a baseline harness the infrastructure flags a do-nothing run always raises are not failures."""
    if run is None:
        return TrialResult(error=f"clawbench left no run-meta.json: {process_note or 'no output'}", facts={"class": "environment_problem"})
    meta = _json(run / "run-meta.json")
    data = run / "data"
    interception = _json(data / "interception.json")
    judge = _json(run / "judge.json")
    intercepted = bool(meta.get("intercepted", interception.get("intercepted")))
    stop_reason = str(interception.get("stop_reason") or (meta.get("run_metrics") or {}).get("stop_reason") or "")
    events = trajectory_events(run, stop_reason)
    traj = ctx.trial_dir / "trajectory.jsonl"
    write_events(traj, events)
    verifier = ctx.trial_dir / "verifier.txt"
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text(verifier_text(meta, interception, judge, events), encoding="utf-8")
    usage = meta.get("usage") or {}
    baseline = harness in BASELINES
    res = TrialResult(seconds=float(meta.get("duration_seconds") or 0.0), tokens=int(usage.get("total_tokens") or 0), trajectory_path=ctx.relative(traj),
                      verifier_output_path=ctx.relative(verifier),
                      facts={"harness": harness, "intercepted": intercepted, "stop_reason": stop_reason, "judge": "none" if baseline else judge_mode,
                             "failure_category": meta.get("failure_category"), "infra_flags": meta.get("infra_flags") or [],
                             "output_dir": ctx.relative(run), "judge_match": judge.get("match") if judge else None, "baseline": baseline})
    category = str(meta.get("failure_category") or "")
    if not baseline and (category in NOT_THE_MODEL or category.startswith("browser_runtime") or stop_reason in ("basalt_failed", "chrome_cdp_timeout")):
        res.error = f"{category or stop_reason}: {clip(meta.get('failure_reason') or interception.get('stop_description') or '')}"
        res.facts["class"] = "environment_problem"
        return res
    if not (data / "interception.json").is_file():
        res.error = "clawbench left no interception.json"
        res.facts["class"] = "environment_problem"
        return res
    broken = pipeline_fault(meta, harness)
    if broken:
        res.error = broken
        res.facts["class"] = "environment_problem"
        return res
    if baseline or judge_mode == "none" or not intercepted:
        res.passed = intercepted
    else:
        match = judge.get("match") if judge else None
        if match is None:                                            # the judge never answered: that says nothing about the agent
            res.error = f"the judge gave no verdict: {clip(judge.get('reason') or judge.get('error') or 'no judge.json')}"
            res.facts["class"] = "environment_problem"
            return res
        res.passed = bool(match)
        res.facts["judge_disagrees"] = not match
    res.score = 1.0 if res.passed else 0.0
    if not res.passed and not res.error:
        res.error = "" if intercepted else f"not intercepted ({stop_reason or 'no stop reason'})"
    return res


# -- the adapter --------------------------------------------------------------------------------------------------------

class ClawBenchAdapter:
    id = ID
    SETTINGS = {"subset": tuple(SUITES), "trials": "runs per case (1)", "judge": ("local", "none"), "harness": HARNESSES}

    def __init__(self, runner: LongRunner = long_runner, which: Callable[[str], Optional[str]] = shutil.which, environ: Optional[Mapping[str, str]] = None,
                 repo_root: Path = REPO_ROOT) -> None:
        self.runner, self.which, self.environ, self.repo_root = runner, which, environ, Path(repo_root)
        self._prepared: Optional[Tuple[Path, str]] = None
        self._built: set = set()

    def plan(self, settings: Mapping[str, Any], bench_dir: Path) -> List[Key]:
        subset = str(settings.get("subset") or "lite")
        trials = int(settings.get("trials") or 1)
        cases = select_cases(source_of(bench_dir), subset, settings)
        return [(h, case, rep) for h in harnesses_for(subset, settings) for case in cases for rep in range(1, trials + 1)]

    # preparing the imported tree once per process ---------------------------------------------------------------------

    def prepare(self, ctx: TrialContext) -> Tuple[Path, str]:
        """Installs our harness in the imported ClawBench and writes the model configuration. Returns (the checkout, the model's key)."""
        source = source_of(ctx.bench_dir)
        model = dict((run_record(ctx).get("model")) or {})
        key = model_key(model)
        if self._prepared == (source, key):
            return source, key
        install_harness(source, self.repo_root)
        workspace = Path(ctx.bench_dir) / "workspace"
        (workspace / "models").mkdir(parents=True, exist_ok=True)
        podman = (self.environ if self.environ is not None else os.environ).get("CONTAINER_ENGINE", "").strip().lower() == "podman"
        (workspace / "models" / "models.yaml").write_text(models_yaml(model, "host.containers.internal" if podman else "host.docker.internal"), encoding="utf-8")
        shipped_env = source / ".env"                                # the benchmark's own disposable-mail account (a key and a domain of the project), read from the workspace
        if not shipped_env.is_file():
            raise RuntimeError(f"{shipped_env} is missing: ClawBench's disposable e-mail accounts need the key and domain in it; import the benchmark again")
        shutil.copyfile(shipped_env, workspace / ".env")
        self._prepared = (source, key)
        return source, key

    def command(self, key: Key, ctx: TrialContext, source: Path, model: str, out_dir: Path) -> List[str]:
        harness, case = str(key[0]), str(key[1])
        subset = str(ctx.settings.get("subset") or "lite")
        judge_mode = str(ctx.settings.get("judge") or "local")
        argv = uv_argv(self.which) + ["run", "--project", str(source), "clawbench-run", str(case_folder(source, subset) / case), model,
                                      "--harness", harness, "--output-dir", str(out_dir), "--no-upload", "--browser-runtime", "local"]
        if harness in self._built:
            argv.append("--no-build")
        if harness in BASELINES or judge_mode == "none":
            argv.append("--no-judge")
        else:
            argv += ["--judge", model + "-judge"]
        return argv

    def run_trial(self, key: Key, ctx: TrialContext) -> TrialResult:
        harness, case = str(key[0]), str(key[1])
        subset = str(ctx.settings.get("subset") or "lite")
        judge_mode = str(ctx.settings.get("judge") or "local")
        if judge_mode not in ("local", "none"):
            return TrialResult(error=f"judge '{judge_mode}' is not allowed; use 'local' or 'none'", facts={"class": "environment_problem"})
        source = source_of(ctx.bench_dir)
        task_file = case_folder(source, subset) / case / "task.json"
        if not task_file.is_file():
            return TrialResult(error=f"the case folder {task_file.parent} is missing; import the benchmark again", facts={"class": "environment_problem"})
        try:
            source, model = self.prepare(ctx)
        except (OSError, ValueError, RuntimeError) as exc:
            return TrialResult(error=f"could not prepare ClawBench: {exc}", facts={"class": "environment_problem"})
        ctx.trial_dir.mkdir(parents=True, exist_ok=True)
        out_dir = ctx.trial_dir / "out"
        shutil.rmtree(out_dir, ignore_errors=True)                  # an earlier attempt of this trial must not be read as this one
        limit = float((_json(task_file).get("time_limit") or 30)) * 60 + CASE_ALLOWANCE_S
        env = clean_env(self.environ, {"CLAWBENCH_WORKSPACE": str(Path(ctx.bench_dir) / "workspace")})
        done = self.runner(self.command(key, ctx, source, model, out_dir), cwd=source, env=env, timeout=limit, should_stop=ctx.should_stop,
                           log_path=ctx.trial_dir / "clawbench.log")
        if done.returncode in (124, 130):
            self.cleanup(harness, case, env)
        if ctx.should_stop():
            return TrialResult(error="stopped on request before the case finished", facts={"class": "environment_problem"})
        run = find_run(out_dir)
        if run is not None:
            self._built.add(harness)
        note = clip(((done.stdout or "") + " " + (done.stderr or ""))[-600:]) if done.returncode not in (0, 1, 3) or run is None else ""
        return trial_result(run, harness, judge_mode, ctx, f"exit code {done.returncode}: {note}" if note else "")

    def cleanup(self, harness: str, case: str, env: Mapping[str, str]) -> None:
        """A container left behind when the process was killed (a stop or a timeout kills ClawBench before its own clean-up runs)."""
        engine = (self.environ or {}).get("CONTAINER_ENGINE") or "docker"
        listed = self.runner([engine, "ps", "-aq", "--filter", f"name=clawbench-{harness}-{case}-"], env=dict(env), timeout=60)
        ids = re.findall(r"[0-9a-f]{6,64}", listed.stdout or "")
        if ids:
            self.runner([engine, "rm", "-f", *ids], env=dict(env), timeout=120)

    # scoring ----------------------------------------------------------------------------------------------------------

    def score(self, results: Mapping[Key, TrialResult]) -> Dict[str, Any]:
        valid = [(k, r) for k, r in results.items() if r.valid]
        groups: Dict[str, List[TrialResult]] = {}
        for k, r in valid:
            groups.setdefault(str(k[0]), []).append(r)
        real = groups.get(OWN_HARNESS) or [r for k, r in valid if k[0] not in BASELINES]
        shown = real or [r for _k, r in valid]
        n = len(shown)
        intercepted = sum(1 for r in shown if r.facts.get("intercepted"))
        passed = sum(1 for r in shown if r.passed)
        judge = next((r.facts.get("judge") for r in shown if r.facts.get("judge")), "local")
        out: Dict[str, Any] = {"reward_rate": passed / n if n else 0.0, "intercepted_rate": intercepted / n if n else 0.0, "of": n, "resolved": passed,
                               "intercepted": intercepted, "invalid": len(results) - len(valid), "judge": judge,
                               "reward_basis": "interception only (judge-free mode)" if judge == "none" else "interception and the local model as judge"}
        out["by_harness"] = {h: {"of": len(rs), "intercepted": sum(1 for r in rs if r.facts.get("intercepted")), "passed": sum(1 for r in rs if r.passed)}
                             for h, rs in sorted(groups.items())}
        base = [r for k, r in valid if k[0] in BASELINES]
        if base:
            null = [r for k, r in valid if k[0] == "null"]
            out["smoke_ok"] = bool(base) and not any(r.facts.get("intercepted") for r in null) and len(valid) == len(results)
        out["notes"] = ["intercepted_rate does not depend on the judge and is comparable with the board's Intercepted column.",
                        "reward_rate uses the local model as judge (the board's judge is a cloud model): provisional, not comparable with the board."
                        if judge != "none" else "judge-free run: reward_rate here is the interception rate.",
                        "Live sites change and may block bots; paywalled cases use a pattern that never matches and cannot pass."]
        return out

    def failure_facts(self, result: TrialResult) -> Dict[str, Any]:
        return {"error": result.error, **result.facts}


ADAPTER = ClawBenchAdapter
