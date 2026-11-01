"""The Terminal-Bench adapter (harness/bench/adapters/terminal_bench.py) and its Harbor agent, with a fake Harbor: no Docker, no GPU, no model."""
import asyncio
import json
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_support import MODEL, make_spec
from harness.bench import analysis, registry
from harness.bench.adapters import terminal_bench as tb
from harness.bench.importer import Result
from harness.bench.runner import BenchRun, TrialContext, TrialResult, get_adapter

TASK_TOML = """schema_version = "1.1"
[metadata]
difficulty = "{difficulty}"
expert_time_estimate_min = {minutes}

[verifier]
timeout_sec = 600.0

[agent]
timeout_sec = {agent_s}

[environment]
build_timeout_sec = 300.0
"""
TASKS = {"fix-git": ("easy", 5.0, 900.0), "overfull-hbox": ("easy", 15.0, 750.0), "cobol-modernization": ("easy", 20.0, 900.0), "prove-plus-comm": ("easy", 10.0, 900.0),
         "m-quick": ("medium", 5.0, 900.0), "m-slow": ("medium", 90.0, 1200.0), "m-mid": ("medium", 30.0, 900.0), "h-one": ("hard", 60.0, 1800.0),
         "m-a": ("medium", 31.0, 900.0), "m-b": ("medium", 32.0, 900.0), "m-c": ("medium", 33.0, 900.0), "m-d": ("medium", 34.0, 900.0)}


def make_source(base: Path, tasks=TASKS) -> Path:
    source = base / "terminal-bench-2" / "source"
    for name, (difficulty, minutes, agent_s) in tasks.items():
        d = source / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.toml").write_text(TASK_TOML.format(difficulty=difficulty, minutes=minutes, agent_s=agent_s), encoding="utf-8")
        (d / "instruction.md").write_text("Do the thing.", encoding="utf-8")
    return source


# -- tasks and subsets ------------------------------------------------------------------------------------------------

def test_task_meta_is_read_with_or_without_a_toml_reader(tmp_path, monkeypatch):
    source = make_source(tmp_path)
    meta = tb.read_task_meta(source / "fix-git")
    assert meta["difficulty"] == "easy" and meta["agent_timeout"] == 900.0 and meta["verifier_timeout"] == 600.0 and meta["build_timeout"] == 300.0
    small = tb._small_toml((source / "m-slow" / "task.toml").read_text())
    assert small["agent"]["timeout_sec"] == 1200.0 and small["metadata"]["difficulty"] == "medium" and small["metadata"]["expert_time_estimate_min"] == 90.0


def test_the_subsets_are_fixed_and_nested(tmp_path):
    source = make_source(tmp_path)
    smoke = tb.select_tasks(source, "smoke")
    easy5 = tb.select_tasks(source, "easy5")
    dev = tb.select_tasks(source, "dev")
    assert smoke == ["fix-git"] and len(easy5) == 5 and len(dev) == 10
    assert easy5[:4] == ["fix-git", "prove-plus-comm", "overfull-hbox", "cobol-modernization"] and easy5[4] == "m-quick"
    assert easy5 == dev[:5] and "h-one" not in dev
    assert tb.select_tasks(source, "full") == sorted(TASKS)
    assert tb.select_tasks(source, "dev") == dev                  # the same on every call
    assert tb.select_tasks(source, "x", {"tasks": ["m-a"]}) == ["m-a"]
    with pytest.raises(ValueError, match="no such Terminal-Bench task: nope"):
        tb.select_tasks(source, "dev", {"tasks": ["nope"]})
    with pytest.raises(ValueError, match="unknown subset"):
        tb.select_tasks(source, "weird")


def test_smoke_falls_back_to_an_easy_task_and_an_empty_folder_is_an_error(tmp_path):
    source = make_source(tmp_path, {"b-easy": ("easy", 5.0, 900.0), "a-hard": ("hard", 1.0, 900.0)})
    assert tb.select_tasks(source, "smoke") == ["b-easy"]
    with pytest.raises(FileNotFoundError, match="import the benchmark"):
        tb.select_tasks(tmp_path / "nothing", "smoke")


def test_plan_gives_k_trials_and_five_for_the_full_set(tmp_path):
    make_source(tmp_path)
    bench = tmp_path / "terminal-bench-2"
    a = tb.TerminalBenchAdapter()
    assert a.plan({"subset": "smoke"}, bench) == [("fix-git", 1)]
    assert a.plan({"subset": "easy5", "trials": 2}, bench)[:3] == [("fix-git", 1), ("fix-git", 2), ("prove-plus-comm", 1)]
    full = a.plan({"subset": "full"}, bench)
    assert len(full) == len(TASKS) * 5 and full[4] == (sorted(TASKS)[0], 5)
    assert len(a.plan({"subset": "full", "trials": 1}, bench)) == len(TASKS)


# -- Harbor's command and environment ---------------------------------------------------------------------------------

def test_harbor_is_started_from_the_setting_the_path_or_uv():
    assert tb.harbor_argv({"harbor_command": ["wsl", "-e", "harbor"]}) == ["wsl", "-e", "harbor"]
    assert tb.harbor_argv({"harbor_command": "uvx harbor"}) == ["uvx", "harbor"]
    assert tb.harbor_argv({}, which=lambda n: "/bin/harbor" if n == "harbor" else None) == ["/bin/harbor"]
    assert tb.harbor_argv({}, which=lambda n: "/bin/uv" if n == "uv" else None) == ["/bin/uv", "tool", "run", "--python", "3.12", "harbor"]


def test_the_harbor_process_gets_no_cloud_key_and_can_import_our_agent():
    env = tb.trial_env({"PATH": "/bin", "OPENAI_API_KEY": "sk-1", "ANTHROPIC_API_KEY": "k", "GEMINI_API_KEY": "g", "HF_TOKEN": "h", "PYTHONPATH": "/x", "KEEP": "1"})
    assert env["KEEP"] == "1" and not any(k in env for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "HF_TOKEN"))
    assert env["HARBOR_TELEMETRY"] == "off" and env["PYTHONPATH"].split(";" if ";" in env["PYTHONPATH"] else ":")[0] == str(tb.REPO_ROOT)


def ctx_for(tmp_path, settings, stop=lambda: False, bench=None):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "settings.json").write_text(json.dumps({"model": MODEL, "settings": settings}), encoding="utf-8")
    return TrialContext(run_dir, run_dir / "trials" / "t", bench or tmp_path / "terminal-bench-2", settings, stop)


def test_the_command_of_a_trial(tmp_path):
    source = make_source(tmp_path)
    a = tb.TerminalBenchAdapter(which=lambda n: "/bin/harbor")
    ctx = ctx_for(tmp_path, {"subset": "dev", "agent_config": {}})
    ctx.trial_dir.mkdir(parents=True)
    argv, jobs, job = a.command(("fix-git", 1), ctx, source / "fix-git", MODEL["endpoint"])
    assert argv[:2] == ["/bin/harbor", "run"] and argv[argv.index("-p") + 1] == str(source / "fix-git") and argv[argv.index("-k") + 1] == "1"
    assert "--yes" in argv and argv[argv.index("-a") + 1] == tb.AGENT_PATH and argv[argv.index("-e") + 1] == "docker"
    assert not any("timeout" in x for x in argv)                  # the task's own limits are never changed
    settings_file = Path(argv[argv.index("--ak") + 1].split("=", 1)[1])
    assert json.loads(settings_file.read_text())["endpoint"] == MODEL["endpoint"]
    smoke = tb.TerminalBenchAdapter(which=lambda n: "/bin/harbor").command(("fix-git", 1), ctx_for(tmp_path, {"subset": "smoke"}), source / "fix-git", MODEL["endpoint"])[0]
    assert smoke[smoke.index("-a") + 1] == "oracle" and "--ak" not in smoke
    with pytest.raises(ValueError, match="'basalt' or 'oracle'"):
        tb.TerminalBenchAdapter()._agent({"agent": "gpt-5"})


# -- reading Harbor's result ------------------------------------------------------------------------------------------

def harbor_trial(root: Path, reward=1.0, exception=None, tokens=(1000, 200), trajectory=True, verifier="2 passed") -> Path:
    trial = root / "job" / "fix-git__abc"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir(parents=True)
    data = {"task_name": "fix-git", "trial_name": "fix-git__abc", "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
            "agent_result": {"n_input_tokens": tokens[0], "n_output_tokens": tokens[1]},
            "exception_info": {"exception_type": exception[0], "exception_message": exception[1]} if exception else None}
    (trial / "result.json").write_text(json.dumps(data), encoding="utf-8")
    (trial / "verifier" / "test-stdout.txt").write_text(verifier, encoding="utf-8")
    if trajectory:
        (trial / "agent" / "trajectory.jsonl").write_text(json.dumps({"type": "stop", "reason": "final"}) + "\n", encoding="utf-8")
    return trial


def one(tmp_path, **kw):
    ctx = ctx_for(tmp_path, {"subset": "dev"})
    trial = harbor_trial(ctx.trial_dir / "jobs", **kw)
    return tb.trial_result(json.loads((trial / "result.json").read_text()), trial, ctx), ctx


def test_a_reward_of_one_is_a_pass_and_zero_a_fail(tmp_path):
    res, ctx = one(tmp_path)
    assert res.passed is True and res.score == 1.0 and res.tokens == 1200 and res.error == ""
    assert res.trajectory_path.endswith("fix-git__abc/agent/trajectory.jsonl") and res.verifier_output_path.endswith("fix-git__abc/verifier/test-stdout.txt")
    assert res.trajectory_path.startswith("trials/t/jobs/job/")      # relative to the run folder, as the analysis reads it
    res0, _ = one(tmp_path / "b", reward=0.0)
    assert res0.passed is False and res0.score == 0.0
    half, _ = one(tmp_path / "c", reward=0.5)
    assert half.passed is False                                    # resolved means reward 1; there is no partial credit


def test_an_agent_timeout_is_graded_but_an_environment_failure_is_not(tmp_path):
    late, _ = one(tmp_path / "a", reward=0.0, exception=("AgentTimeoutError", "Agent execution timed out after 900 seconds"))
    assert late.passed is False and "AgentTimeoutError" in late.error
    ctx_overflow, _ = one(tmp_path / "b", reward=0.0, exception=("ContextWindowExceededError", "too long"))
    assert ctx_overflow.passed is False
    broken, _ = one(tmp_path / "c", reward=None, exception=("SandboxBuildFailedError", "docker build failed"))
    assert broken.passed is None and not broken.valid and broken.facts["class"] == "environment_problem"
    crashed, _ = one(tmp_path / "d", reward=0.0, exception=("AgentUnavailableError", "cannot reach the Basalt server"))
    assert crashed.passed is None                                  # a reward next to an exception that is not the agent's is not taken
    silent, _ = one(tmp_path / "e", reward=None)
    assert silent.passed is None and "no reward" in silent.error
    assert tb.trial_result(None, None, ctx_for(tmp_path / "f", {}), "exit code 1: boom").passed is None


# -- the adapter against a fake Harbor --------------------------------------------------------------------------------

class FakeHarbor:
    """Stands in for `harbor run`: records the command and writes the files a real run leaves."""

    def __init__(self, **trial):
        self.trial, self.calls, self.stop_seen = trial, [], []

    def __call__(self, args, cwd=None, env=None, timeout=None, should_stop=None, log_path=None):
        self.calls.append({"args": list(args), "cwd": cwd, "env": env, "timeout": timeout})
        self.stop_seen.append(should_stop() if should_stop else None)
        jobs = Path(args[args.index("-o") + 1])
        if self.trial.get("nothing"):
            return Result(1, "docker: cannot connect to the docker daemon", "")
        harbor_trial(jobs, **{k: v for k, v in self.trial.items() if k != "nothing"})
        return Result(0, "done", "")


def test_run_trial_runs_harbor_once_and_reads_the_trial(tmp_path):
    make_source(tmp_path)
    fake = FakeHarbor(reward=1.0)
    a = tb.TerminalBenchAdapter(runner=fake, which=lambda n: "/bin/harbor", environ={"PATH": "/bin", "OPENAI_API_KEY": "x"})
    ctx = ctx_for(tmp_path, {"subset": "dev"})
    res = a.run_trial(("fix-git", 1), ctx)
    assert res.passed is True and len(fake.calls) == 1
    call = fake.calls[0]
    assert call["timeout"] == 900 + 600 + 300 + tb.EXTRA_WAIT_S and "OPENAI_API_KEY" not in call["env"] and call["cwd"] == ctx.trial_dir
    assert res.trajectory_path.startswith("trials/t/jobs/job/fix-git__abc/agent")


def test_a_missing_result_leaves_the_trial_unjudged_and_a_stop_is_not_a_failure(tmp_path):
    make_source(tmp_path)
    a = tb.TerminalBenchAdapter(runner=FakeHarbor(nothing=True), which=lambda n: "/bin/harbor")
    res = a.run_trial(("fix-git", 1), ctx_for(tmp_path, {"subset": "dev"}))
    assert res.passed is None and "cannot connect to the docker daemon" in res.error
    stopped = tb.TerminalBenchAdapter(runner=FakeHarbor(nothing=True), which=lambda n: "/bin/harbor").run_trial(("fix-git", 1), ctx_for(tmp_path, {"subset": "dev"}, stop=lambda: True))
    assert stopped.passed is None and "stopped" in stopped.error


def test_a_missing_task_folder_or_a_foreign_model_address_is_refused(tmp_path):
    make_source(tmp_path)
    a = tb.TerminalBenchAdapter(runner=FakeHarbor(reward=1.0), which=lambda n: "/bin/harbor")
    missing = a.run_trial(("not-there", 1), ctx_for(tmp_path, {"subset": "dev"}))
    assert missing.passed is None and "import the benchmark again" in missing.error
    ctx = ctx_for(tmp_path, {"subset": "dev", "endpoint": "https://api.openai.com/v1"})
    (ctx.run_dir / "settings.json").write_text(json.dumps({"model": {"endpoint": ""}, "settings": ctx.settings}), encoding="utf-8")
    with pytest.raises(ValueError, match="not the local engine"):
        a.run_trial(("fix-git", 1), ctx)
    (ctx.run_dir / "settings.json").write_text(json.dumps({"model": {}, "settings": {}}), encoding="utf-8")
    ctx.settings.pop("endpoint")
    with pytest.raises(ValueError, match="no model address"):
        a.run_trial(("fix-git", 1), ctx)


def test_score_and_failure_facts():
    a = tb.TerminalBenchAdapter()
    results = {("a", 1): TrialResult(passed=True), ("a", 2): TrialResult(passed=False), ("b", 1): TrialResult(passed=False), ("c", 1): TrialResult(passed=None, error="x")}
    m = a.score(results)
    assert m["resolution_rate"] == pytest.approx(1 / 3) and (m["resolved"], m["of"], m["invalid"], m["tasks"], m["tasks_solved_any"]) == (1, 3, 1, 2, 1)
    assert a.failure_facts(TrialResult(error="e", facts={"class": "timeout"})) == {"error": "e", "class": "timeout"}


def test_a_whole_run_through_the_runner_and_the_failure_report(tmp_path):
    """The adapter inside BenchRun: three trials (a pass, a timeout with a trajectory, a broken container), the journal, the score and the analysis."""
    make_source(tmp_path)
    outcomes = {"fix-git": dict(reward=1.0), "prove-plus-comm": dict(reward=0.0, exception=("AgentTimeoutError", "timed out after 900 seconds")),
                "overfull-hbox": dict(nothing=True)}
    fakes = {t: FakeHarbor(**o) for t, o in outcomes.items()}

    def runner(args, **kw):
        return fakes[Path(args[args.index("-p") + 1]).name](args, **kw)

    adapter = tb.TerminalBenchAdapter(runner=runner, which=lambda n: "/bin/harbor")
    spec = make_spec("unused", id="terminal-bench-2", primary_metric="resolution_rate")
    run = BenchRun(tmp_path, spec, {"subset": "dev", "tasks": list(outcomes)}, adapter=adapter, model=MODEL, power_limit_w=450.0, root=tmp_path)
    state = run.execute()
    assert state["status"] == "done" and (state["done"], state["passed"], state["failed"], state["invalid"]) == (3, 1, 1, 1)
    metrics = json.loads((run.dir / "results.json").read_text())["metrics"]
    assert metrics["resolution_rate"] == 0.5
    rep = analysis.report(run.dir, adapter)
    assert rep["classes"] == {"environment_problem": 1, "timeout": 1}
    again = BenchRun(tmp_path, spec, {}, adapter=adapter, run_id=run.run_id, root=tmp_path)
    before = sum(len(f.calls) for f in fakes.values())
    again.execute()
    assert sum(len(f.calls) for f in fakes.values()) == before + 1       # only the unjudged trial is run again


def test_the_adapter_is_found_without_being_listed():
    assert get_adapter("terminal-bench-2", lazy=True).id == "terminal-bench-2"
    with pytest.raises(Exception):
        get_adapter("terminal-bench-2")                            # the plain table stays empty
    assert registry.get("terminal-bench-2").primary_metric == "resolution_rate"


# -- the Harbor agent, with a fake Harbor installed --------------------------------------------------------------------

@pytest.fixture
def harbor_stub(monkeypatch):
    """Minimal stand-ins for the few harbor modules the agent imports."""
    for name in list(sys.modules):
        if name.startswith("harbor"):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.delitem(sys.modules, "harness.bench.adapters.terminal_bench_agent", raising=False)
    pkgs = {n: types.ModuleType(n) for n in ("harbor", "harbor.agents", "harbor.agents.base", "harbor.environments", "harbor.environments.base", "harbor.models",
                                             "harbor.models.agent", "harbor.models.agent.context")}

    class BaseAgent:
        def __init__(self, logs_dir, model_name=None, **kwargs):
            self.logs_dir, self.model_name, self.kwargs = logs_dir, model_name, kwargs

    class BaseEnvironment:
        pass

    class AgentContext:
        n_input_tokens = n_output_tokens = n_cache_tokens = metadata = None

    pkgs["harbor.agents.base"].BaseAgent = BaseAgent
    pkgs["harbor.environments.base"].BaseEnvironment = BaseEnvironment
    pkgs["harbor.models.agent.context"].AgentContext = AgentContext
    for n, m in pkgs.items():
        monkeypatch.setitem(sys.modules, n, m)
    import importlib
    return importlib.import_module("harness.bench.adapters.terminal_bench_agent"), AgentContext


BASH = shutil.which("bash")
if BASH and "system32" in BASH.lower().replace("/", "\\"):
    BASH = None


class FakeEnvironment:
    """`environment.exec` over a local bash in a folder, as an async call like Harbor's."""

    def __init__(self, root: Path):
        self.root, self.commands = root, []

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.commands.append(command)

        def run():
            p = subprocess.run([BASH, "-c", command], cwd=self.root, capture_output=True, timeout=timeout_sec or 60)
            return types.SimpleNamespace(return_code=p.returncode, stdout=p.stdout.decode("utf-8", "replace"), stderr=p.stderr.decode("utf-8", "replace"))
        return await asyncio.to_thread(run)


def agent_settings_file(tmp_path):
    f = tmp_path / "agent-settings.json"
    f.write_text(json.dumps({"endpoint": MODEL["endpoint"], "overrides": {}}), encoding="utf-8")
    return f


@pytest.mark.skipif(BASH is None, reason="needs bash to stand in for the container")
def test_the_harbor_agent_runs_our_agent_through_environment_exec(harbor_stub, tmp_path, monkeypatch):
    monkeypatch.setattr("harness.stack.local_now", None)
    module, Context = harbor_stub
    from test_session import Scripted, call, say
    root = tmp_path / "container"
    root.mkdir()
    client = Scripted(call("write_file", path="answer.txt", content="42"), call("shell_command", command="cat answer.txt"), say("Finished."))
    agent = module.BasaltAgent(logs_dir=tmp_path / "agent", settings_file=str(agent_settings_file(tmp_path)))
    agent.client_factory = lambda endpoint: client
    env, ctx = FakeEnvironment(root), Context()
    asyncio.run(agent.run("write 42 to answer.txt", env, ctx))
    assert (root / "answer.txt").read_text() == "42" and len(env.commands) >= 3
    assert ctx.metadata["stop_reason"] == "final" and ctx.metadata["tool_calls"] == 2
    events = [json.loads(l) for l in (tmp_path / "agent" / "trajectory.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events if e["type"] in ("tool_call", "stop")] == ["tool_call", "tool_call", "stop"]
    assert json.loads((tmp_path / "agent" / "harness-summary.json").read_text())["stop_reason"] == "final"
    assert agent.name() == "basalt-harness" and agent.version() == (tb.REPO_ROOT / "VERSION").read_text().strip()


@pytest.mark.skipif(BASH is None, reason="needs bash to stand in for the container")
def test_when_harbor_cancels_the_agent_stops_and_late_commands_are_refused(harbor_stub, tmp_path, monkeypatch):
    monkeypatch.setattr("harness.stack.local_now", None)
    module, Context = harbor_stub
    from test_session import Scripted, call, say

    def slow_then_call():
        time.sleep(1.0)
        return call("shell_command", command="touch late.txt")
    root = tmp_path / "container"
    root.mkdir()
    agent = module.BasaltAgent(logs_dir=tmp_path / "agent", settings_file=str(agent_settings_file(tmp_path)))
    agent.client_factory = lambda endpoint: Scripted(slow_then_call, say("never"))

    async def go():
        await asyncio.wait_for(agent.run("t", FakeEnvironment(root), Context()), timeout=0.4)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(go())
    time.sleep(0.2)
    assert not (root / "late.txt").exists()                        # the tests would have looked at the container before this command
    events = [json.loads(l) for l in (tmp_path / "agent" / "trajectory.jsonl").read_text().splitlines()]
    assert {"type": "stop", "reason": "timeout"} in events or any(e.get("reason") == "timeout" for e in events)


@pytest.mark.skipif(BASH is None, reason="needs bash to stand in for the container")
def test_a_dead_model_server_is_not_graded_as_the_agent_failing(harbor_stub, tmp_path, monkeypatch):
    monkeypatch.setattr("harness.stack.local_now", None)
    module, Context = harbor_stub
    from test_session import Scripted
    root = tmp_path / "container"
    root.mkdir()
    agent = module.BasaltAgent(logs_dir=tmp_path / "agent", settings_file=str(agent_settings_file(tmp_path)))
    agent.client_factory = lambda endpoint: Scripted(RuntimeError("cannot reach the Basalt server at http://127.0.0.1:7232: refused"))
    with pytest.raises(RuntimeError, match="AgentUnavailableError"):
        asyncio.run(agent.run("t", FakeEnvironment(root), Context()))


def test_the_agent_options_are_checked(harbor_stub, tmp_path):
    module, _ = harbor_stub
    with pytest.raises(ValueError, match="no model address"):
        module.BasaltAgent(logs_dir=tmp_path)
