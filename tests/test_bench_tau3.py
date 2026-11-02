"""The tau-bench adapter (tadamoo/bench/adapters/tau3.py) against a fake tau2-bench checkout and a fake `tau2 run`: planning, the command and the environment, reading
the simulation, pass^k, and the launcher that keeps every model call on the local engine. No tau2-bench install, no model."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.bench import analysis
from tadamoo.bench.adapters import tau3
from tadamoo.bench.importer import Result
from tadamoo.bench.runner import BenchRun, TrialContext, TrialResult
from tadamoo.eval import resume
from bench_support import MODEL, make_spec

AIRLINE = [str(i) for i in range(5)]


@pytest.fixture(autouse=True)
def fixed_tree(monkeypatch):
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": False, "changes": ""})


@pytest.fixture
def checkout(tmp_path):
    src = tmp_path / "bench" / "tau3-bench" / "source"
    d = src / "data" / "tau2" / "domains"
    (d / "airline").mkdir(parents=True)
    (d / "airline" / "split_tasks.json").write_text(json.dumps({"train": ["0"], "test": ["1"], "base": AIRLINE}))
    (d / "retail").mkdir()
    (d / "retail" / "split_tasks.json").write_text(json.dumps({"base": ["r1", "r2", "r3"]}))
    (d / "telecom").mkdir()
    (d / "telecom" / "split_tasks.json").write_text(json.dumps({"base": ["[mobile_data_issue]user_abroad[PERSONA:None]"]}))
    (d / "banking_knowledge").mkdir()
    (d / "banking_knowledge" / "tasks.json").write_text(json.dumps([{"id": "task_001"}, {"id": "task_002"}]))
    return src.parent


def ctx_for(bench_dir: Path, run_dir: Path, name: str, settings=None, stop=lambda: False) -> TrialContext:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "settings.json").write_text(json.dumps({"model": MODEL, "settings": settings or {}}))
    return TrialContext(run_dir, run_dir / "trials" / name, bench_dir, dict(settings or {}), stop)


def simulation(task, reward=1.0, termination="agent_stop", **over):
    info = {"reward": reward, "reward_basis": ["DB", "ACTION"], "db_check": {"db_match": reward >= 1.0, "db_reward": reward},
            "action_checks": [] if reward >= 1.0 else [{"action": {"name": "cancel_reservation", "arguments": {"reservation_id": "X1"}}, "action_match": False, "action_reward": 0.0}],
            "communicate_checks": [] if reward >= 1.0 else [{"info": "the refund is 120 dollars", "met": False, "justification": "never said"}],
            "nl_assertions": [], "env_assertions": []}
    messages = [{"role": "user", "content": "Cancel my flight"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "get_reservation_details", "arguments": {"reservation_id": "X1"}, "requestor": "assistant"}],
                 "usage": {"prompt_tokens": 800, "completion_tokens": 40}},
                {"role": "tool", "id": "c1", "content": "Error: reservation not found", "requestor": "assistant", "error": over.pop("tool_error", False)},
                {"role": "assistant", "content": "I cancelled it.", "usage": {"prompt_tokens": 900, "completion_tokens": 20}},
                {"role": "user", "content": "Thanks", "usage": {"prompt_tokens": 300, "completion_tokens": 10}}]
    sim = {"id": "s", "task_id": task, "duration": 42.5, "termination_reason": termination, "reward_info": info, "messages": messages}
    sim.update(over)
    return sim


class FakeTau2:
    """Stands in for `uv run ... launcher.py run ...`: writes results.json under --save-to for the scenario of the task."""

    def __init__(self, scenarios=None, repairs=0):
        self.scenarios, self.repairs, self.calls = scenarios or {}, repairs, []

    def __call__(self, args, cwd=None, env=None, timeout=None, should_stop=None, log_path=None):
        self.calls.append({"args": list(args), "env": env, "cwd": cwd, "timeout": timeout})
        task = args[args.index("--task-ids") + 1]
        how = self.scenarios.get(task, "win")
        if how == "none":
            return Result(1, "Traceback: litellm.APIConnectionError", "")
        save = Path(args[args.index("--save-to") + 1])
        save.mkdir(parents=True, exist_ok=True)
        sim = {"win": lambda: simulation(task), "lose": lambda: simulation(task, 0.0), "max": lambda: simulation(task, 0.0, "max_steps"),
               "ctx": lambda: simulation(task, 0.0, "context_window_exceeded"), "infra": lambda: simulation(task, 0.0, "infrastructure_error"),
               "user": lambda: simulation(task, 0.0, "user_error"), "tool": lambda: simulation(task, 0.0, tool_error=True)}[how]()
        if how == "infra":
            sim["reward_info"] = None
        (save / "results.json").write_text(json.dumps({"info": {}, "tasks": [], "simulations": [sim]}))
        if self.repairs and "BASALT_TAU_REPAIRS_LOG" in env:
            Path(env["BASALT_TAU_REPAIRS_LOG"]).write_text("\n".join(json.dumps({"repaired": ["arguments"]}) for _ in range(self.repairs)))
        return Result(0, "done", "")


def make(runner):
    return tau3.Tau3Adapter(runner=runner, which=lambda n: "/bin/" + n, environ={"PATH": "/bin", "OPENAI_API_KEY": "sk-real", "ANTHROPIC_API_KEY": "a", "KEEP": "1"},
                            repo_root=Path(tau3.REPO_ROOT))


# -- planning -------------------------------------------------------------------------------------------------------

def test_the_subsets_are_the_domains_and_banking_is_tau2s_banking_knowledge(checkout):
    a = make(FakeTau2())
    assert a.plan({"subset": "smoke"}, checkout) == [("airline", "0", 1), ("airline", "1", 1)]
    assert a.plan({"subset": "airline", "trials": 2, "limit": 2}, checkout) == [("airline", "0", 1), ("airline", "0", 2), ("airline", "1", 1), ("airline", "1", 2)]
    assert [k[1] for k in a.plan({"subset": "retail"}, checkout)] == ["r1", "r2", "r3"]
    assert a.plan({"subset": "banking"}, checkout) == [("banking", "task_001", 1), ("banking", "task_002", 1)]
    assert a.plan({"subset": "telecom"}, checkout)[0][1].startswith("[mobile_data_issue]")
    assert a.plan({"subset": "airline", "tasks": ["3"]}, checkout) == [("airline", "3", 1)]
    with pytest.raises(ValueError, match="no such task in airline"):
        a.plan({"subset": "airline", "tasks": ["99"]}, checkout)
    with pytest.raises(ValueError, match="unknown subset"):
        a.plan({"subset": "cars"}, checkout)
    with pytest.raises(FileNotFoundError, match="import the benchmark first"):
        a.plan({"subset": "airline"}, checkout.parent / "empty")


# -- the command ------------------------------------------------------------------------------------------------------

def test_every_model_in_the_command_is_the_local_one_and_the_environment_has_no_cloud_key(checkout, tmp_path):
    fake = FakeTau2()
    a = make(fake)
    res = a.run_trial(("airline", "2", 2), ctx_for(checkout, tmp_path / "run", "t", {"subset": "airline"}))
    args, env = fake.calls[0]["args"], fake.calls[0]["env"]
    assert args[:4] == ["/bin/uv", "run", "--project", str(checkout / "source")] and args[4] == "python" and args[5].endswith("launcher.py") and args[6] == "run"
    assert args[args.index("--domain") + 1] == "airline" and args[args.index("--task-ids") + 1] == "2" and args[args.index("--num-trials") + 1] == "1"
    assert args[args.index("--seed") + 1] == "301" and args[args.index("--max-concurrency") + 1] == "1"
    assert args[args.index("--agent") + 1] == "llm_agent" and args[args.index("--user") + 1] == "user_simulator"
    for flag in ("--agent-llm", "--user-llm"):
        assert args[args.index(flag) + 1] == "openai/Qwen3.8-Flash-Next"
    for flag in ("--agent-llm-args", "--user-llm-args"):
        got = json.loads(args[args.index(flag) + 1])
        assert got == {"api_base": "http://127.0.0.1:7232/v1", "api_key": "local", "temperature": 0.0}
    judge = json.loads(args[args.index("--communicate-judge") + 1])
    assert judge == {"model": "openai/Qwen3.8-Flash-Next", "model_args": {"api_base": "http://127.0.0.1:7232/v1", "api_key": "local"}} and "--communicate-scorer" not in args
    assert env["OPENAI_API_KEY"] == "local" and env["OPENAI_API_BASE"] == "http://127.0.0.1:7232/v1" and "ANTHROPIC_API_KEY" not in env and env["KEEP"] == "1"
    assert env["BASALT_TAU_MODEL"] == "Qwen3.8-Flash-Next" and env["BASALT_TAU_REPAIR"] == "1" and env["BASALT_REPO"] == str(tau3.REPO_ROOT)
    assert fake.calls[0]["timeout"] == tau3.TRIAL_TIMEOUT_S and res.passed is True


def test_the_judge_free_setting_uses_the_substring_match_and_banking_installs_its_extra(checkout, tmp_path):
    fake = FakeTau2()
    a = make(fake)
    a.run_trial(("banking", "task_001", 1), ctx_for(checkout, tmp_path / "run", "b", {"subset": "banking", "judge": "none", "repair": False, "max_steps": 80, "sim_timeout_s": 900}))
    args, env = fake.calls[0]["args"], fake.calls[0]["env"]
    assert args[args.index("--domain") + 1] == "banking_knowledge" and args[args.index("--extra") + 1] == "knowledge" and args.index("--extra") < args.index("python")
    assert args[args.index("--communicate-scorer") + 1] == "substring" and "--communicate-judge" not in args
    assert args[args.index("--max-steps") + 1] == "80" and args[args.index("--timeout") + 1] == "900.0" and env["BASALT_TAU_REPAIR"] == "0"
    res = a.run_trial(("airline", "0", 1), ctx_for(checkout, tmp_path / "run", "c", {"subset": "airline", "judge": "openai"}))
    assert res.passed is None and "not allowed" in res.error


# -- reading the simulation ---------------------------------------------------------------------------------------------

def test_a_won_task_passes_and_a_lost_one_says_what_the_check_found(checkout, tmp_path):
    a = make(FakeTau2({"0": "win", "1": "lose"}))
    won = a.run_trial(("airline", "0", 1), ctx_for(checkout, tmp_path / "run", "w", {"subset": "airline"}))
    lost = a.run_trial(("airline", "1", 1), ctx_for(checkout, tmp_path / "run", "l", {"subset": "airline"}))
    assert won.passed is True and won.score == 1.0 and won.seconds == 42.5 and won.tokens == 800 + 40 + 900 + 20 + 300 + 10
    assert won.facts["agent_tokens"] == 1760 and won.facts["user_tokens"] == 310 and won.facts["domain"] == "airline"
    assert lost.passed is False and lost.score == 0.0 and "reward 0" in lost.error
    text = (tmp_path / "run" / lost.verifier_output_path).read_text()
    assert "DOES NOT match" in text and "cancel_reservation" in text and "the refund is 120 dollars" in text
    events = analysis._read_events(tmp_path / "run" / lost.trajectory_path)
    assert events[0] == {"type": "tool_call", "name": "get_reservation_details", "args": {"reservation_id": "X1"}, "decision": "executed"}
    assert events[-1] == {"type": "stop", "reason": "final"}
    assert analysis.classify(lost, events, text)[0] == "wrong_final_state"


def test_a_tool_error_ends_the_call_as_failed_and_a_step_limit_is_a_timeout(checkout, tmp_path):
    a = make(FakeTau2({"0": "tool", "1": "max", "2": "ctx"}))
    tool = a.run_trial(("airline", "0", 1), ctx_for(checkout, tmp_path / "run", "a", {"subset": "airline"}))
    ev = analysis._read_events(tmp_path / "run" / tool.trajectory_path)
    assert ev[0]["decision"] == "failed" and ev[1]["error"].startswith("Error: reservation not found")
    mx = a.run_trial(("airline", "1", 1), ctx_for(checkout, tmp_path / "run", "b", {"subset": "airline"}))
    assert mx.passed is False and analysis._read_events(tmp_path / "run" / mx.trajectory_path)[-1]["reason"] == "timeout"
    cx = a.run_trial(("airline", "2", 1), ctx_for(checkout, tmp_path / "run", "c", {"subset": "airline"}))
    assert cx.passed is False and analysis._read_events(tmp_path / "run" / cx.trajectory_path)[-1]["reason"] == "context_overflow"


def test_an_engine_failure_or_a_failure_of_the_simulated_user_leaves_the_task_unjudged(checkout, tmp_path):
    a = make(FakeTau2({"0": "infra", "1": "user", "2": "none"}))
    for i, why in (("0", "infrastructure_error"), ("1", "user_error"), ("2", "no simulation")):
        res = a.run_trial(("airline", i, 1), ctx_for(checkout, tmp_path / "run", i, {"subset": "airline"}))
        assert res.passed is None and not res.valid and res.facts["class"] == "environment_problem", (i, res.error)
        assert why in res.error or "no reward" in res.error or "no simulation" in res.error, (i, res.error)
    assert "exit code 1" in res.error and "APIConnectionError" in res.error


def test_the_repairs_the_launcher_made_are_counted(checkout, tmp_path):
    res = make(FakeTau2(repairs=3)).run_trial(("airline", "0", 1), ctx_for(checkout, tmp_path / "run", "r", {"subset": "airline"}))
    assert res.facts["repaired_calls"] == 3


def test_an_earlier_attempt_is_not_read_as_this_one_and_a_stop_ends_the_trial(checkout, tmp_path):
    ctx = ctx_for(checkout, tmp_path / "run", "s", {"subset": "airline"})
    old = ctx.trial_dir / "sim"
    old.mkdir(parents=True)
    (old / "results.json").write_text(json.dumps({"simulations": [simulation("0")]}))
    assert make(FakeTau2({"0": "none"})).run_trial(("airline", "0", 1), ctx).passed is None
    assert make(FakeTau2()).run_trial(("airline", "0", 1), ctx_for(checkout, tmp_path / "run", "s2", {"subset": "airline"}, stop=lambda: True)).error.startswith("stopped")


# -- the score --------------------------------------------------------------------------------------------------------

def test_pass_hat_k_is_the_mean_over_tasks_of_the_chance_that_k_trials_all_succeed():
    a = make(FakeTau2())

    def r(ok):
        return TrialResult(passed=ok)

    got = a.score({("airline", "a", 1): r(True), ("airline", "a", 2): r(False), ("airline", "b", 1): r(True), ("airline", "b", 2): r(True),
                   ("retail", "c", 1): r(False), ("retail", "c", 2): r(False), ("retail", "d", 1): TrialResult(error="x")})
    assert got["k"] == 2 and got["pass_hat"]["1"] == pytest.approx((0.5 + 1 + 0) / 3) and got["pass_hat"]["2"] == pytest.approx((0 + 1 + 0) / 3)
    assert got["pass_hat_k"] == got["pass_hat"]["2"] and got["invalid"] == 1 and got["tasks"] == 3 and got["of"] == 6 and got["passed"] == 3
    assert got["by_domain"]["airline"]["pass_hat_2"] == pytest.approx(0.5) and got["by_domain"]["retail"]["pass_hat_1"] == 0.0
    assert "provisional" in got["notes"][0] and "more than the harness" in got["notes"][1]
    assert a.score({})["pass_hat_k"] == 0.0 and a.score({("airline", "a", 1): TrialResult(error="x")})["k"] == 0


def test_a_whole_two_trial_run_through_the_runner_and_the_failure_report(checkout, tmp_path):
    fake = FakeTau2({"0": "win", "1": "lose", "2": "tool"})
    spec = make_spec("tau", id="tau3-bench", primary_metric="pass_hat_k", official_cloud_judge=True)
    run = BenchRun(tmp_path / "base", spec, {"subset": "airline", "limit": 3, "trials": 2}, adapter=make(fake), model=MODEL, power_limit_w=400.0, root=tmp_path)
    run.bench_dir = checkout
    state = run.execute()
    assert state["status"] == "done" and state["total"] == 6 and state["passed"] == 2 and state["failed"] == 4
    results = json.loads((run.dir / "results.json").read_text())
    assert results["provisional"] is True and results["metrics"]["k"] == 2 and results["metrics"]["pass_hat_k"] == pytest.approx(1 / 3)
    assert [c["args"][c["args"].index("--seed") + 1] for c in fake.calls[:2]] == ["300", "301"]
    assert analysis.report(run.dir)["failed"] == 4


# -- the launcher that runs inside tau2-bench's environment --------------------------------------------------------------

def load_launcher():
    spec = importlib.util.spec_from_file_location("tau3_launcher_under_test", tau3.LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def reply(name, arguments):
    call = types.SimpleNamespace(function=types.SimpleNamespace(name=name, arguments=arguments))
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(tool_calls=[call]))]), call


def test_the_launcher_sends_every_call_to_the_local_engine_whatever_model_was_named():
    mod = load_launcher()
    seen = {}
    wrapped = mod.local_completion(lambda *a, **kw: (seen.update(kw), reply("f", "{}")[0])[1], "http://127.0.0.1:7232/v1", "Qwen")
    wrapped(model="gpt-5.5", messages=[], api_base="https://api.openai.com/v1", api_key="sk-real", reasoning_effort="low", temperature=0.0)
    assert seen == {"model": "openai/Qwen", "messages": [], "api_base": "http://127.0.0.1:7232/v1", "api_key": "local", "temperature": 0.0}
    mod.check_local("http://127.0.0.1:7232/v1", "")
    mod.check_local("http://192.168.1.5:7232/v1", "192.168.1.5")
    with pytest.raises(SystemExit, match="not to the local engine"):
        mod.check_local("https://api.openai.com/v1", "127.0.0.1")


def test_the_launcher_repairs_a_tool_call_written_slightly_wrong_and_logs_it(tmp_path):
    mod = load_launcher()
    repair = mod.load_repair(str(tau3.REPO_ROOT))
    assert repair is not None and mod.load_repair(str(tmp_path)) is None and mod.load_repair("") is None
    tools = [{"function": {"name": "get_reservation_details"}}, {"function": {"name": "cancel_reservation"}}]
    log = tmp_path / "r.jsonl"
    response, call = reply("Get_Reservation_Details", '{"reservation_id": "X1",}')
    assert mod.repair_tool_calls(response, tools, repair, str(log)) == 1
    assert call.function.name == "get_reservation_details" and json.loads(call.function.arguments) == {"reservation_id": "X1"}
    assert json.loads(log.read_text().splitlines()[0]) == {"repaired": ["arguments", "name"], "name": "get_reservation_details"}
    fine, call2 = reply("cancel_reservation", '{"reservation_id": "X1"}')
    assert mod.repair_tool_calls(fine, tools, repair, str(log)) == 0 and call2.function.arguments == '{"reservation_id": "X1"}'
    hopeless, call3 = reply("cancel_reservation", "not json at all")
    assert mod.repair_tool_calls(hopeless, tools, repair, str(log)) == 0 and call3.function.arguments == "not json at all"
    assert mod.repair_tool_calls(types.SimpleNamespace(choices=[]), tools, repair, str(log)) == 0
    off, _ = reply("f", "{}")
    wrapped = mod.local_completion(lambda *a, **kw: reply("Cancel_Reservation", "{}")[0], "http://127.0.0.1:1/v1", "m", repair=None)
    assert wrapped(tools=tools).choices[0].message.tool_calls[0].function.name == "Cancel_Reservation"        # repair off: the reply is left as it came


def test_the_launcher_wraps_taus_one_completion_function_and_starts_its_cli(monkeypatch):
    mod = load_launcher()
    started = []
    llm_utils = types.ModuleType("tau2.utils.llm_utils")
    llm_utils.completion = lambda **kw: reply("f", "{}")[0]
    nl = types.ModuleType("tau2.evaluator.evaluator_nl_assertions")
    nl.DEFAULT_LLM_NL_ASSERTIONS, nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = "gpt-5.4-mini", {"reasoning_effort": "low"}
    cli = types.ModuleType("tau2.cli")
    cli.main = lambda: started.append(True)
    tree = {"tau2": types.ModuleType("tau2"), "tau2.utils": types.ModuleType("tau2.utils"), "tau2.utils.llm_utils": llm_utils,
            "tau2.evaluator": types.ModuleType("tau2.evaluator"), "tau2.evaluator.evaluator_nl_assertions": nl, "tau2.cli": cli}
    for name, module in tree.items():
        monkeypatch.setitem(sys.modules, name, module)
    for parent, child in (("tau2", "utils"), ("tau2", "evaluator"), ("tau2.utils", "llm_utils"), ("tau2.evaluator", "evaluator_nl_assertions"), ("tau2", "cli")):
        setattr(sys.modules[parent], child, sys.modules[parent + "." + child])
    for k, v in {"BASALT_TAU_BASE_URL": "http://127.0.0.1:7232/v1", "BASALT_TAU_MODEL": "Qwen", "BASALT_TAU_HOST": "127.0.0.1", "BASALT_REPO": str(tau3.REPO_ROOT)}.items():
        monkeypatch.setenv(k, v)
    mod.main()
    assert started == [True] and nl.DEFAULT_LLM_NL_ASSERTIONS == "openai/Qwen" and nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS == {}
    out = llm_utils.completion(model="gpt-5.5", tools=[])
    assert out.choices[0].message.tool_calls[0].function.name == "f"


def test_the_adapter_is_found_by_the_lazy_loader_and_has_the_runners_protocol():
    from tadamoo.bench import runner
    from tadamoo.bench.adapters import load
    adapter = runner.get_adapter(tau3.ID, {}, lazy=True)
    assert load(tau3.ID) is tau3.Tau3Adapter and isinstance(adapter, tau3.Tau3Adapter) and adapter.id == "tau3-bench"
    assert all(callable(getattr(adapter, n)) for n in ("plan", "run_trial", "score", "failure_facts"))
