"""Terminal-Bench 2.0 through Harbor.

One trial is one `harbor run` of one task with one attempt (`-k 1`), so the run's journal keeps the unit the person resumes by. Harbor builds the task's
container, runs the agent, runs the task's own hidden tests and writes `result.json` and the verifier's files; this adapter reads them. The agent is
`terminal_bench_agent.BasaltAgent`, a Harbor `BaseAgent` that Harbor imports by its path: it runs our headless agent on the host with its tools routed to
`environment.exec`. The smoke subset uses Harbor's own `oracle` agent (it runs the task's reference solution), which proves Docker, Harbor and the verifier
without the model.

Settings read: `subset` (smoke, easy5, dev, full), `trials` (5 for `full` when not given), `tasks` (explicit task names), `agent` ('basalt', or 'oracle';
the smoke subset defaults to 'oracle'), `agent_config` (AgentConfig fields to change; see containerkit.FORBIDDEN_FIELDS), `harbor_command` (the program,
a list), `environment` (Harbor's `--env`, default docker). Timeouts are the task's own and are never changed."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from harness.bench.runner import SETTINGS, Key, TrialContext, TrialResult, read_json

from .containerkit import agent_overrides, guidance_of, local_endpoint
from .proc import STALLED, LongRunner, dump_stacks, long_runner

ID = "terminal-bench-2"
AGENT_PATH = "harness.bench.adapters.terminal_bench_agent:BasaltAgent"
SMOKE_TASK = "fix-git"
DIFFICULTY = {"easy": 0, "medium": 1, "hard": 2}
SUBSET_SIZE = {"easy5": 5, "dev": 10}
FULL_TRIALS = 5
PULL_LIMIT_S = 4 * 3600.0             # a pull that stalls ends here; the registry client retries on its own long before
START_STALL_S = 30.0                # Harbor has written nothing and made no job folder this long after its start (a healthy one does it within two seconds): it did not start, and is started again
START_TRIES = 2
REPORT_MARGIN_S = 60.0              # the agent is told its time less this, so that its last report is written before the task's limit ends it
EXTRA_WAIT_S = 900.0                 # image pull and build, and Harbor's own start and stop, on top of the task's agent and verifier limits
REPO_ROOT = Path(__file__).resolve().parents[3]
CLOUD_KEYS = re.compile(r"(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE_API|OPENROUTER|AZURE_OPENAI|XAI|MISTRAL|COHERE|GROQ|TOGETHER|DEEPSEEK|PERPLEXITY|HF_TOKEN)", re.I)
# exceptions of a trial whose reward still measures the agent (harbor/models/trial/error_policy.py: AGENT_EXHAUSTED_BUDGET and AGENT_REFUSED_TASK)
AGENT_SIDE = frozenset({"AgentTimeoutError", "ContextWindowExceededError", "OutputTokenExceededError", "ContextLengthExceededError", "OutputLengthExceededError",
                        "AgentSafetyRefusalError", "ContentPolicyViolationError"})


def source_of(bench_dir: Path) -> Path:
    s = Path(bench_dir) / "source"
    return s if s.is_dir() else Path(bench_dir)


# -- tasks --------------------------------------------------------------------------------------------------------------

def read_task_meta(task_dir: Path) -> Dict[str, Any]:
    """difficulty, agent and verifier time limits and the expert time estimate of a task, from its task.toml."""
    text = (Path(task_dir) / "task.toml").read_text(encoding="utf-8")
    data: Dict[str, Any] = {}
    try:
        try:
            import tomllib as toml_reader
        except ModuleNotFoundError:
            import tomli as toml_reader                         # type: ignore[no-redef]
        data = toml_reader.loads(text)
    except Exception:
        data = _small_toml(text)
    meta, agent, verifier, env = (data.get(k) or {} for k in ("metadata", "agent", "verifier", "environment"))
    return {"name": Path(task_dir).name, "difficulty": str(meta.get("difficulty") or "medium"), "expert_min": float(meta.get("expert_time_estimate_min") or 1e9),
            "agent_timeout": float(agent.get("timeout_sec") or 900.0), "verifier_timeout": float(verifier.get("timeout_sec") or 900.0),
            "build_timeout": float(env.get("build_timeout_sec") or 600.0), "docker_image": str(env.get("docker_image") or "")}


def _small_toml(text: str) -> Dict[str, Any]:
    """The few `key = value` lines of the task.toml sections that matter, when no TOML reader is installed (Python 3.10 has none)."""
    out: Dict[str, Dict[str, Any]] = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        head = re.match(r"^\[\[?([A-Za-z0-9_.]+)\]?\]$", line)
        if head:
            section = head.group(1)
            continue
        pair = re.match(r'^([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$', line)
        if pair and section in ("metadata", "agent", "verifier", "environment"):
            raw = pair.group(2).strip().strip('"')
            try:
                value: Any = float(raw)
            except ValueError:
                value = raw
            out.setdefault(section, {})[pair.group(1)] = value
    return out


def all_tasks(source: Path) -> List[Dict[str, Any]]:
    return [read_task_meta(p.parent) for p in sorted(Path(source).glob("*/task.toml"))]


def select_tasks(source: Path, subset: str, settings: Optional[Mapping[str, Any]] = None) -> List[str]:
    """The task names of a subset. The order is fixed: easy before medium before hard, shortest expert estimate first, then the name; `easy5` and `dev` are
    the first 5 and the first 10 of it, so the same subset is the same tasks on every machine. `smoke` is one easy task."""
    explicit = (settings or {}).get("tasks")
    tasks = sorted(all_tasks(source), key=lambda t: (DIFFICULTY.get(t["difficulty"], 3), t["expert_min"], t["name"]))
    names = [t["name"] for t in tasks]
    if explicit:
        missing = [n for n in explicit if n not in names]
        if missing:
            raise ValueError(f"no such Terminal-Bench task: {', '.join(missing)}")
        return list(explicit)
    if not names:
        raise FileNotFoundError(f"no task folders (with a task.toml) found in {source}; import the benchmark first")
    if subset == "smoke":
        return [SMOKE_TASK if SMOKE_TASK in names else next((t["name"] for t in tasks if t["difficulty"] == "easy"), names[0])]
    if subset in SUBSET_SIZE:
        return names[:SUBSET_SIZE[subset]]
    if subset == "full":
        return sorted(names)
    raise ValueError(f"unknown subset {subset!r}")


# -- Harbor -------------------------------------------------------------------------------------------------------------

def harbor_argv(settings: Mapping[str, Any], which: Callable[[str], Optional[str]] = shutil.which) -> List[str]:
    """The program that starts Harbor: the setting, else `harbor` on the PATH, else uv's one-off tool run on Python 3.12 (Harbor needs 3.12 or newer)."""
    given = settings.get("harbor_command")
    if given:
        return shlex.split(given) if isinstance(given, str) else [str(x) for x in given]
    found = which("harbor")
    if found:
        return [found]
    return [which("uv") or "uv", "tool", "run", "--python", "3.12", "harbor"]


def trial_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment of the Harbor process: this repository importable (the agent module is imported by Harbor), Harbor's usage statistics off, and no
    key of any cloud model provider, so nothing can reach one by accident."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not CLOUD_KEYS.search(k)}
    paths = [str(REPO_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    env.update({"PYTHONPATH": os.pathsep.join(paths), "HARBOR_TELEMETRY": "off", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def find_trial_dir(jobs_dir: Path, job_name: str) -> Optional[Path]:
    """The folder of the one trial of a job: the sub-folder of the job with a result.json that names a task."""
    job = Path(jobs_dir) / job_name
    for sub in sorted(p for p in job.glob("*") if p.is_dir()):
        data = read_json(sub / "result.json")
        if data and data.get("task_name"):
            return sub
    return None


def reward_of(data: Mapping[str, Any]) -> Optional[float]:
    rewards = ((data.get("verifier_result") or {}).get("rewards")) or {}
    if "reward" in rewards:
        return float(rewards["reward"])
    return float(next(iter(rewards.values()))) if rewards else None


def _clip(text: str, n: int = 400) -> str:
    return " ".join(str(text).split())[:n]


def trial_result(data: Optional[Mapping[str, Any]], trial_dir: Optional[Path], ctx: TrialContext, process_note: str = "") -> TrialResult:
    """Harbor's result of one trial as ours. A reward counts only when Harbor says it measures the agent: no exception, or one in AGENT_SIDE (a timeout or a
    context overflow of the agent). Anything else (the container did not start, the verifier crashed) leaves the trial unjudged, so it is run again on resume."""
    if data is None:
        return TrialResult(error=f"harbor left no trial result: {process_note or 'no output'}", facts={"class": "environment_problem"})
    exc = data.get("exception_info") or {}
    kind, message = str(exc.get("exception_type") or ""), str(exc.get("exception_message") or "")
    reward = reward_of(data)
    agent = data.get("agent_result") or {}
    tokens = int(agent.get("n_input_tokens") or 0) + int(agent.get("n_output_tokens") or 0)
    res = TrialResult(tokens=tokens, score=reward, facts={"reward": reward, "exception": kind})
    if trial_dir is not None:
        for attr, rel in (("trajectory_path", "agent/trajectory.jsonl"), ("verifier_output_path", "verifier/test-stdout.txt")):
            f = Path(trial_dir) / rel
            if f.is_file():
                setattr(res, attr, ctx.relative(f))
    if kind:
        res.error = f"{kind}: {_clip(message)}"
    if reward is not None and (not kind or kind in AGENT_SIDE):
        res.passed = reward >= 1.0
        return res
    if kind and kind not in AGENT_SIDE:
        res.facts["class"] = "environment_problem"
        return res
    res.error = res.error or "the verifier gave no reward"
    res.facts["class"] = "environment_problem"
    return res


# -- the adapter --------------------------------------------------------------------------------------------------------

class TerminalBenchAdapter:
    id = ID
    SETTINGS = {"subset": ("smoke", "easy5", "dev", "full"), "trials": "k attempts per task (5 for full)", "agent": ("basalt", "oracle")}

    def __init__(self, runner: LongRunner = long_runner, which: Callable[[str], Optional[str]] = shutil.which,
                 environ: Optional[Mapping[str, str]] = None) -> None:
        self.runner, self.which, self.environ = runner, which, environ

    def plan(self, settings: Mapping[str, Any], bench_dir: Path) -> List[Key]:
        subset = str(settings.get("subset") or "smoke")
        trials = int(settings.get("trials") or (FULL_TRIALS if subset == "full" else 1))
        guidance_of(settings)                                        # a name that is not guidance is refused before the first trial
        return [(task, rep) for task in select_tasks(source_of(bench_dir), subset, settings) for rep in range(1, trials + 1)]

    def _agent(self, settings: Mapping[str, Any]) -> str:
        chosen = str(settings.get("agent") or ("oracle" if settings.get("subset") == "smoke" else "basalt"))
        if chosen not in ("basalt", "oracle"):
            raise ValueError(f"the agent is 'basalt' or 'oracle', not {chosen!r}")
        return chosen

    def command(self, key: Key, ctx: TrialContext, task_dir: Path, endpoint: str) -> Tuple[List[str], Path, str]:
        """(argv of the Harbor run, the jobs folder, the job name) of one trial."""
        settings = ctx.settings
        agent = self._agent(settings)
        jobs, job = ctx.trial_dir / "jobs", "job"
        argv = harbor_argv(settings, self.which) + ["run", "-p", str(task_dir), "-k", "1", "-o", str(jobs), "--job-name", job, "--yes",
                                                       "-e", str(settings.get("environment") or "docker")]
        if agent == "oracle":
            argv += ["-a", "oracle"]
        else:
            agent_file = ctx.trial_dir / "agent-settings.json"
            budget = max(REPORT_MARGIN_S, read_task_meta(task_dir)["agent_timeout"] - REPORT_MARGIN_S)
            agent_file.write_text(json.dumps({"endpoint": endpoint, "overrides": agent_overrides(settings), "guidance": guidance_of(settings), "arm": "harness",
                                              "max_seconds": budget}), encoding="utf-8")
            argv += ["-a", AGENT_PATH, "--ak", f"settings_file={agent_file.as_posix()}"]
        return argv, jobs, job

    def fetch_image(self, image: str, ctx: TrialContext) -> None:
        """Pulls the task's prebuilt image when this PC does not have it yet, before Harbor's start-up timer begins. A failed pull is left to Harbor,
        which then reports the environment problem itself."""
        docker = self.which("docker")
        if not image or not docker or ctx.should_stop():
            return
        env = trial_env(self.environ)
        if self.runner([docker, "image", "inspect", image], cwd=ctx.trial_dir, env=env, timeout=120.0, should_stop=ctx.should_stop).returncode == 0:
            return
        self.runner([docker, "pull", image], cwd=ctx.trial_dir, env=env, timeout=PULL_LIMIT_S, should_stop=ctx.should_stop, log_path=ctx.trial_dir / "pull.log")

    def run_trial(self, key: Key, ctx: TrialContext) -> TrialResult:
        task = key[0]
        task_dir = source_of(ctx.bench_dir) / str(task)
        if not (task_dir / "task.toml").is_file():
            return TrialResult(error=f"the task folder {task_dir} is missing; import the benchmark again", facts={"class": "environment_problem"})
        record = read_json(ctx.run_dir / SETTINGS) or {}
        endpoint = local_endpoint(str((record.get("model") or {}).get("endpoint") or ctx.settings.get("endpoint") or ""), record.get("model"))
        ctx.trial_dir.mkdir(parents=True, exist_ok=True)
        meta = read_task_meta(task_dir)
        self.fetch_image(meta["docker_image"], ctx)
        argv, jobs, job = self.command(key, ctx, task_dir, endpoint)
        limit = meta["agent_timeout"] + meta["verifier_timeout"] + meta["build_timeout"] + EXTRA_WAIT_S
        log = ctx.trial_dir / "harbor.log"
        started = lambda: jobs.exists() or (log.exists() and log.stat().st_size > 0)
        for attempt in range(START_TRIES):
            done = self.runner(argv, cwd=ctx.trial_dir, env=trial_env(self.environ), timeout=limit, should_stop=ctx.should_stop, log_path=log, stall_after=START_STALL_S,
                               alive=started, on_stall=lambda pid, n=attempt + 1: dump_stacks(pid, ctx.trial_dir / f"stall-{n}.txt"))
            if done.returncode != STALLED:
                break
        if done.returncode == STALLED:
            return TrialResult(error=f"harbor did not start in {START_TRIES} tries (where it stood is in stall-1.txt and stall-2.txt)", facts={"class": "environment_problem", "stalled": START_TRIES})
        if ctx.should_stop():
            return TrialResult(error="stopped on request before the trial finished", facts={"class": "environment_problem"})
        trial_dir = find_trial_dir(jobs, job)
        data = read_json(trial_dir / "result.json") if trial_dir else None
        note = _clip(((done.stdout or "") + " " + (done.stderr or ""))[-600:]) if done.returncode != 0 or data is None else ""
        return trial_result(data, trial_dir, ctx, f"exit code {done.returncode}: {note}" if note else "")

    def score(self, results: Mapping[Key, TrialResult]) -> Dict[str, Any]:
        valid = [(k, r) for k, r in results.items() if r.valid]
        won = sum(1 for _k, r in valid if r.passed)
        per_task: Dict[str, List[bool]] = {}
        for k, r in valid:
            per_task.setdefault(str(k[0]), []).append(bool(r.passed))
        return {"resolution_rate": won / len(valid) if valid else 0.0, "resolved": won, "of": len(valid), "invalid": len(results) - len(valid),
                "tasks": len(per_task), "tasks_solved_any": sum(1 for v in per_task.values() if any(v)),
                "mean_task_rate": sum(sum(v) / len(v) for v in per_task.values()) / len(per_task) if per_task else 0.0}

    def failure_facts(self, result: TrialResult) -> Dict[str, Any]:
        return {"error": result.error, **result.facts}


ADAPTER = TerminalBenchAdapter
