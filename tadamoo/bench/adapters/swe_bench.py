"""SWE-bench Verified: an agent in the issue's repository container, graded by the official harness.

For one trial (one instance, one repetition):
  1. the instance's Docker image is started as a container (the repository is at /testbed, as root, as in the official evaluation);
  2. an agent works on the issue text in that container through `docker exec`: either our headless agent (arm `harness`) or a bare loop with a single bash
     tool on the same model (arm `baseline`), so the effect of the harness shows as the difference between two runs;
  3. the agent's change is read back as a `git diff` and written to a predictions file;
  4. the official harness grades it in its own fresh container: `swebench eval <instance file> -p <predictions> ...` applies the patch, runs the instance's
     tests and writes report.json; `resolved` there is the result. An empty patch is unresolved, as in the official accounting.

The arm is chosen by the setting `arm`, or by a `-baseline` ending on the subset (`dev-baseline`), which the Settings form can select today.
Other settings: `subset` (smoke, dev, random100, full), `trials`, `instances` (explicit ids), `seed` (the shuffle behind dev and random100), `agent_timeout_s`
(default 1800), `max_steps` (model calls, default 150, the same for both arms), `command_timeout_s`, `eval_timeout_s`, `pull_images` (default true),
`agent_config` (AgentConfig fields for the harness arm), `swebench_command` (the program, a list).

The dataset is read from the imported benchmark folder: a .jsonl or .json file, else the parquet file(s) of the Hugging Face repository."""
from __future__ import annotations

import json
import random
import shlex
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from tadamoo.bench import importer
from tadamoo.bench.runner import SETTINGS, Key, TrialContext, TrialResult, read_json, slug

from .containerkit import (AgentOutcome, ExecOut, TrajectoryRecorder, agent_overrides, is_server_fault, local_endpoint, run_bash_baseline, run_harness_agent,
                           watch_stop)
from .proc import LongRunner, long_runner

ID = "swe-bench-verified"
WORKDIR = "/testbed"
ARMS = ("harness", "baseline")
SUBSET_SIZE = {"smoke": 2, "dev": 20, "random100": 100}
DEFAULT_SEED = 20261101
AGENT_TIMEOUT_S = 1800.0
EVAL_TIMEOUT_S = 1800
MAX_STEPS = 150
COMMAND_S = 120.0
PULL_TIMEOUT_S = 3600.0
DIFFICULTY = {"<15 min fix": 0, "15 min - 1 hour": 1, "1-4 hours": 2, ">4 hours": 3}
LFS_POINTER = b"version https://git-lfs"
NOT_DATA = ("dataset_info.json", "croissant.json", "state.json", "package.json")
ACTIVATE = "if [ -f /opt/miniconda3/bin/activate ]; then source /opt/miniconda3/bin/activate >/dev/null 2>&1; conda activate testbed >/dev/null 2>&1; fi; "
PROMPT = """<uploaded_files>
{workdir}
</uploaded_files>
I've uploaded a code repository in the directory {workdir}. Consider the following issue description:

<issue_description>
{problem}
</issue_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <issue_description> are met?
I've already taken care of all changes to any of the test files described in the <issue_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!
Your task is to make the minimal changes to non-test files in the {workdir} directory to ensure the <issue_description> is satisfied.
First look at the code, then reproduce the problem, change the code, and run the relevant tests again."""
NOTE = ("The system is the instance's Linux container, with the project's Python environment active. shell_command runs a bash command in it, and read_file, write_file, "
        "edit_file, list_directory and search_code work on the container's files; the repository is in {workdir} and a relative path is relative to it. Nobody can answer "
        "questions, so work on your own. Your change is read back as a git diff of {workdir} and graded by the project's hidden tests, so leave the fix in the files.")


def source_of(bench_dir: Path) -> Path:
    s = Path(bench_dir) / "source"
    return s if s.is_dir() else Path(bench_dir)


# -- the dataset --------------------------------------------------------------------------------------------------------

PARQUET_SCRIPT = ("import sys, json, pyarrow.parquet as pq\n"
                  "rows = []\n"
                  "for p in sys.argv[2:]:\n"
                  "    rows += pq.read_table(p).to_pylist()\n"
                  "open(sys.argv[1], 'w', encoding='utf-8').write('\\n'.join(json.dumps(r, default=str) for r in rows) + '\\n')\n")


def find_dataset_files(source: Path) -> List[Path]:
    """The data file(s) of the imported dataset repository: .jsonl, else .json, else parquet."""
    files = [p for p in sorted(Path(source).rglob("*")) if p.is_file() and ".git" not in p.relative_to(source).parts and p.name not in NOT_DATA]
    for suffix in (".jsonl", ".json", ".parquet"):
        found = [p for p in files if p.suffix == suffix]
        test_split = [p for p in found if "test" in p.name.lower()]
        if found:
            return test_split or found
    return []


def _jsonl_or_json(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return list(data.values()) if isinstance(data, dict) else list(data)


def load_instances(source: Path, runner: Callable[..., Any] = importer.default_runner, which: Callable[[str], Optional[str]] = shutil.which,
                   cache: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The instances (dicts with instance_id, problem_statement, ...) from the dataset in the imported folder."""
    files = find_dataset_files(source)
    if not files:
        raise FileNotFoundError(f"no dataset file (.jsonl, .json or .parquet) found in {source}; import the benchmark first")
    if files[0].suffix != ".parquet":
        return _jsonl_or_json(files[0])
    for f in files:
        if f.read_bytes()[:len(LFS_POINTER)] == LFS_POINTER:
            raise FileNotFoundError(f"{f.name} is a Git LFS pointer, not the data: install git-lfs and import the benchmark again")
    try:
        import pyarrow.parquet as pq                                  # type: ignore[import-not-found]
        return [row for f in files for row in pq.read_table(f).to_pylist()]
    except ImportError:
        pass
    target = cache or (Path(source).parent / "instances.jsonl")
    if not target.is_file():
        script = Path(source).parent / "_parquet_to_jsonl.py"
        script.write_text(PARQUET_SCRIPT, encoding="utf-8")
        done = runner([which("uv") or "uv", "run", "--with", "pyarrow", "python", str(script), str(target), *[str(f) for f in files]], None, 600)
        if done.returncode != 0 or not target.is_file():
            raise RuntimeError(f"reading the parquet dataset needs pyarrow (pip install pyarrow, or uv): {(done.stderr or done.stdout)[-300:]}")
    return _jsonl_or_json(target)


def shuffled_ids(instances: Sequence[Mapping[str, Any]], seed: int) -> List[str]:
    ids = sorted(str(i["instance_id"]) for i in instances)
    random.Random(seed).shuffle(ids)
    return ids


def select_instances(instances: Sequence[Mapping[str, Any]], subset: str, settings: Optional[Mapping[str, Any]] = None) -> List[str]:
    """The instance ids of a subset. dev and random100 are the first 20 and the first 100 of one seeded shuffle of the sorted ids (so dev is inside random100);
    smoke is the two easiest instances (by the dataset's difficulty, then the shuffle); full is all of them in id order."""
    s = settings or {}
    ids = sorted(str(i["instance_id"]) for i in instances)
    if s.get("instances"):
        missing = [i for i in s["instances"] if i not in ids]
        if missing:
            raise ValueError(f"no such SWE-bench instance: {', '.join(missing)}")
        return list(s["instances"])
    base = subset[:-len("-baseline")] if subset.endswith("-baseline") else subset
    order = shuffled_ids(instances, int(s.get("seed") or DEFAULT_SEED))
    if base == "full":
        return ids
    if base == "smoke":
        by_id = {str(i["instance_id"]): i for i in instances}
        rank = {iid: DIFFICULTY.get(str(by_id[iid].get("difficulty") or ""), 1) for iid in order}
        return sorted(order, key=lambda iid: rank[iid])[:SUBSET_SIZE["smoke"]]
    if base in SUBSET_SIZE:
        return order[:SUBSET_SIZE[base]]
    raise ValueError(f"unknown subset {subset!r}")


def arm_of(settings: Mapping[str, Any]) -> str:
    arm = settings.get("arm") or ("baseline" if str(settings.get("subset") or "").endswith("-baseline") else "harness")
    if arm not in ARMS:
        raise ValueError(f"the arm is 'harness' or 'baseline', not {arm!r}")
    return str(arm)


def image_of(instance: Mapping[str, Any]) -> str:
    """The instance's Docker image: the dataset's own `image` field, else the published name (`swebench/sweb.eval.x86_64.<id with __ as _1776_>:latest`)."""
    if instance.get("image"):
        return str(instance["image"])
    return "swebench/sweb.eval.x86_64." + str(instance["instance_id"]).replace("__", "_1776_").lower() + ":latest"


# -- Docker, through its command line -----------------------------------------------------------------------------------

class DockerContainer:
    """One container started from an image, reached with `docker exec`. `runner` is the importer's runner (args, cwd, timeout, env) -> Result."""

    def __init__(self, image: str, name: str, runner: Callable[..., Any] = importer.default_runner) -> None:
        self.image, self.name, self.runner, self.started = image, name, runner, False

    def have_image(self) -> bool:
        return self.runner(["docker", "image", "inspect", self.image], None, 60).returncode == 0

    def pull(self) -> None:
        done = self.runner(["docker", "pull", self.image], None, PULL_TIMEOUT_S)
        if done.returncode != 0:
            raise RuntimeError(f"docker pull {self.image} failed: {(done.stderr or done.stdout).strip()[-300:]}")

    def start(self) -> None:
        done = self.runner(["docker", "run", "-d", "--name", self.name, "-w", WORKDIR, "--user", "root", "--cap-add", "SYS_ADMIN", self.image, "tail", "-f", "/dev/null"],
                           None, 600)
        if done.returncode != 0:
            raise RuntimeError(f"docker run {self.image} failed: {(done.stderr or done.stdout).strip()[-300:]}")
        self.started = True

    def exec(self, command: str, timeout: Optional[float] = None) -> ExecOut:
        done = self.runner(["docker", "exec", "-w", WORKDIR, self.name, "bash", "-c", ACTIVATE + command], None, timeout)
        if done.returncode == 124 and "timed out" in (done.stderr or ""):
            return ExecOut(124, done.stdout or "", done.stderr or "", True)
        return ExecOut(done.returncode, done.stdout or "", done.stderr or "")

    def remove(self) -> None:
        self.runner(["docker", "rm", "-f", self.name], None, 120)
        self.started = False


def git_patch(exec_fn: Callable[[str, Optional[float]], ExecOut]) -> str:
    """The change in the repository (the working folder of `exec_fn`) as a patch: everything tracked or new (not ignored), staged, without binary files
    (`git apply` cannot take them)."""
    exec_fn("git add -A", 120)
    stat = exec_fn("git diff --cached --numstat", 120)
    binary = [line.split("\t", 2)[2] for line in stat.stdout.splitlines() if line.startswith("-\t-\t") and line.count("\t") >= 2]
    skip = "".join(" " + shlex.quote(f":(exclude){p}") for p in binary)
    out = exec_fn("git diff --cached --no-color -- ." + skip, 120)
    return out.stdout if out.rc == 0 else ""


# -- the adapter --------------------------------------------------------------------------------------------------------

def eval_argv(settings: Mapping[str, Any], which: Callable[[str], Optional[str]] = shutil.which) -> List[str]:
    """The program of the official harness: the setting, else `swebench` on the PATH, else uv's one-off tool run."""
    given = settings.get("swebench_command")
    if given:
        return shlex.split(given) if isinstance(given, str) else [str(x) for x in given]
    found = which("swebench")
    return [found] if found else [which("uv") or "uv", "tool", "run", "--from", "swebench", "swebench"]


def read_report(eval_dir: Path, instance_id: str) -> Optional[Dict[str, Any]]:
    for f in sorted((Path(eval_dir) / "logs" / "evaluation").glob(f"*/*/{instance_id}/report.json")):
        data = read_json(f)
        if data and instance_id in data:
            return data[instance_id]
    return None


def instance_logs(eval_dir: Path, instance_id: str) -> Tuple[Optional[Path], Optional[Path]]:
    """(test_output.txt, run_instance.log) of the official evaluation of an instance, where they exist."""
    base = sorted((Path(eval_dir) / "logs" / "evaluation").glob(f"*/*/{instance_id}"))
    if not base:
        return None, None
    tests, log = base[0] / "test_output.txt", base[0] / "run_instance.log"
    return (tests if tests.is_file() else None), (log if log.is_file() else None)


def _tail(path: Optional[Path], n: int = 3000) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-n:] if path else ""
    except OSError:
        return ""


class SweBenchAdapter:
    id = ID
    SETTINGS = {"subset": ("smoke", "dev", "random100", "full", "smoke-baseline", "dev-baseline", "random100-baseline", "full-baseline"), "arm": ARMS,
                "trials": "repetitions per instance"}

    def __init__(self, runner: Callable[..., Any] = importer.default_runner, eval_runner: LongRunner = long_runner, which: Callable[[str], Optional[str]] = shutil.which,
                 client_factory: Optional[Callable[[str], Any]] = None, container_factory: Optional[Callable[[str, str], Any]] = None) -> None:
        self.runner, self.eval_runner, self.which = runner, eval_runner, which
        self.client_factory, self.container_factory = client_factory, container_factory
        self._instances: Optional[Dict[str, Dict[str, Any]]] = None
        self._lock = threading.Lock()

    # -- planning ---------------------------------------------------------------------------------------------------

    def instances(self, bench_dir: Path) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if self._instances is None:
                self._instances = {str(i["instance_id"]): i for i in load_instances(source_of(bench_dir), self.runner, self.which)}
            return self._instances

    def plan(self, settings: Mapping[str, Any], bench_dir: Path) -> List[Key]:
        arm_of(settings)
        by_id = self.instances(bench_dir)
        trials = int(settings.get("trials") or 1)
        return [(iid, rep) for iid in select_instances(list(by_id.values()), str(settings.get("subset") or "smoke"), settings) for rep in range(1, trials + 1)]

    # -- one trial --------------------------------------------------------------------------------------------------

    def _client(self, endpoint: str) -> Any:
        if self.client_factory:
            return self.client_factory(endpoint)
        from tadamoo.client import BasaltClient
        return BasaltClient(base_url=endpoint)

    def run_trial(self, key: Key, ctx: TrialContext) -> TrialResult:
        iid, rep = str(key[0]), int(key[1])
        s = ctx.settings
        arm = arm_of(s)
        instance = self.instances(ctx.bench_dir).get(iid)
        if instance is None:
            return TrialResult(error=f"instance {iid} is not in the imported dataset", facts={"class": "environment_problem"})
        record = read_json(ctx.run_dir / SETTINGS) or {}
        endpoint = local_endpoint(str((record.get("model") or {}).get("endpoint") or s.get("endpoint") or ""), record.get("model"))
        ctx.trial_dir.mkdir(parents=True, exist_ok=True)
        trajectory = TrajectoryRecorder(ctx.trial_dir / "trajectory.jsonl")
        patch, outcome, fault = "", AgentOutcome(), ""
        make = self.container_factory or (lambda image, name: DockerContainer(image, name, self.runner))
        container = make(image_of(instance), f"basalt-swe-{slug(iid).lower()[:60]}-{uuid.uuid4().hex[:6]}")
        done, cancel = threading.Event(), threading.Event()
        watch_stop(ctx.should_stop, cancel, done)
        try:
            if not container.have_image():
                if s.get("pull_images", True) is False:
                    return TrialResult(error=f"the image {container.image} is not on this PC (pull_images is off)", facts={"class": "environment_problem"})
                container.pull()
            container.start()
            outcome = self._agent(arm, instance, container, ctx, trajectory, endpoint, cancel)
            if is_server_fault(outcome):
                fault = outcome.error
            else:
                patch = git_patch(container.exec)
        except Exception as exc:
            fault = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()
            trajectory.close()
            try:
                container.remove()
            except Exception:
                pass
        res = TrialResult(tokens=outcome.tokens, seconds=outcome.seconds, trajectory_path=ctx.relative(ctx.trial_dir / "trajectory.jsonl"),
                          facts={"arm": arm, "stop_reason": outcome.stop_reason, "turns": outcome.turns, "agent_seconds": round(outcome.seconds, 1)})
        if fault:
            res.error, res.facts["class"] = fault, "environment_problem"
            return res
        if outcome.stop_reason == "cancelled":
            res.error, res.facts["class"] = "stopped on request before the trial finished", "environment_problem"
            return res
        (ctx.trial_dir / "model.patch").write_text(patch, encoding="utf-8")
        res.facts["patch_chars"] = len(patch)
        return self._grade(res, instance, iid, rep, arm, patch, outcome, ctx)

    def _agent(self, arm: str, instance: Mapping[str, Any], container: Any, ctx: TrialContext, trajectory: TrajectoryRecorder, endpoint: str,
               cancel: threading.Event) -> AgentOutcome:
        s = ctx.settings
        prompt = PROMPT.format(workdir=WORKDIR, problem=instance.get("problem_statement") or "")
        seconds = float(s.get("agent_timeout_s") or AGENT_TIMEOUT_S)
        steps = int(s.get("max_steps") or MAX_STEPS)
        client = self._client(endpoint)
        if arm == "baseline":
            return run_bash_baseline(prompt, container.exec, client=client, trajectory=trajectory, max_steps=steps, max_seconds=seconds, cancel=cancel,
                                     command_timeout=float(s.get("command_timeout_s") or COMMAND_S))
        return run_harness_agent(prompt, container.exec, endpoint=endpoint, work_dir=ctx.trial_dir / "host", trajectory=trajectory, environment_note=NOTE.format(workdir=WORKDIR),
                                 client=client, cancel=cancel, max_seconds=seconds, max_turns=steps, overrides=agent_overrides(s), cwd_label=WORKDIR)

    def _grade(self, res: TrialResult, instance: Mapping[str, Any], iid: str, rep: int, arm: str, patch: str, outcome: AgentOutcome, ctx: TrialContext) -> TrialResult:
        eval_dir = ctx.trial_dir / "eval"
        eval_dir.mkdir(parents=True, exist_ok=True)
        if not patch.strip():                                          # the official harness leaves an empty patch out and counts it unresolved
            note = eval_dir / "no-patch.txt"
            note.write_text("The agent left no change in the repository, so there is nothing to grade; the instance counts as unresolved.\n", encoding="utf-8")
            res.passed, res.verifier_output_path, res.facts["empty_patch"] = False, ctx.relative(note), True
            return res
        (eval_dir / "instance.json").write_text(json.dumps([instance], default=str), encoding="utf-8")
        model = f"basalt-{arm}"
        (eval_dir / "predictions.jsonl").write_text(json.dumps({"instance_id": iid, "model_name_or_path": model, "model_patch": patch}) + "\n", encoding="utf-8")
        timeout = int(ctx.settings.get("eval_timeout_s") or EVAL_TIMEOUT_S)
        argv = eval_argv(ctx.settings, self.which) + ["eval", "instance.json", "-p", "predictions.jsonl", "-r", f"{model}-{rep}", "-i", iid, "-j", "1", "-t", str(timeout)]
        got = self.eval_runner(argv, cwd=eval_dir, env=None, timeout=timeout + 3600, should_stop=ctx.should_stop, log_path=eval_dir / "swebench.log")
        tests, log = instance_logs(eval_dir, iid)
        shown = tests or log or (eval_dir / "swebench.log")
        res.verifier_output_path = ctx.relative(shown) if shown.is_file() else ""
        report = read_report(eval_dir, iid)
        if report is not None:
            if report.get("infra_failure"):
                res.error, res.facts["class"] = f"infra failure in the evaluation: {report.get('infra_failure_reason', 'unknown')}", "environment_problem"
                return res
            res.passed, res.score = bool(report.get("resolved")), 1.0 if report.get("resolved") else 0.0
            res.facts["patch_applied"] = bool(report.get("patch_successfully_applied"))
            return res
        text = _tail(log)
        if ">>>>> Patch Apply Failed" in text:                         # the patch itself did not apply: a failure of the agent's change
            res.passed, res.score, res.facts["patch_applied"] = False, 0.0, False
            return res
        res.error = f"swebench eval left no report (exit {got.returncode}): {' '.join((got.stdout or got.stderr or '')[-300:].split())}"
        res.facts["class"] = "environment_problem"
        return res

    # -- results ----------------------------------------------------------------------------------------------------

    def score(self, results: Mapping[Key, TrialResult]) -> Dict[str, Any]:
        valid = [r for r in results.values() if r.valid]
        won = sum(1 for r in valid if r.passed)
        empty = sum(1 for r in valid if r.facts.get("empty_patch"))
        arms = sorted({str(r.facts.get("arm")) for r in results.values() if r.facts.get("arm")})
        return {"resolution_rate": won / len(valid) if valid else 0.0, "resolved": won, "of": len(valid), "invalid": len(results) - len(valid), "empty_patches": empty,
                "arm": arms[0] if len(arms) == 1 else ",".join(arms)}

    def failure_facts(self, result: TrialResult) -> Dict[str, Any]:
        return {"error": result.error, **result.facts}


ADAPTER = SweBenchAdapter
