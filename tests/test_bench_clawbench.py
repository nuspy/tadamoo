"""The ClawBench adapter (tadamoo/bench/adapters/clawbench.py) against a fake ClawBench checkout and a fake `clawbench-run`: planning, adding our harness to the
imported tree, the command, reading the results and the score. No Docker, no model."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.bench import analysis
from tadamoo.bench.adapters import clawbench as cb
from tadamoo.bench.importer import Result
from tadamoo.bench.runner import BenchRun, TrialContext, TrialResult, read_trials
from tadamoo.eval import resume
from bench_support import MODEL, make_spec

YAML = """default: openclaw
base:
  image: clawbench-base
  dockerfile: base/Dockerfile.base
  entrypoint_script: base/entrypoint.sh
  agent_message_sources:
    - type: file
      path: /data/agent-messages.jsonl
harnesses:
  - name: openclaw
    image: clawbench-openclaw
    dockerfile: openclaw/Dockerfile.openclaw
    setup_script: openclaw/setup-openclaw.sh
    run_script: openclaw/run-openclaw.sh
    usage_emitter: openclaw/usage-emitter.py
    agent_message_sources:
      - type: file
        path: /data/agent-messages.jsonl
  - name: "null"
    image: clawbench-null
    dockerfile: "null/Dockerfile.null"
    setup_script: "null/setup-null.sh"
    run_script: "null/run-null.sh"
    usage_emitter: "null/usage-emitter.py"
    agent_message_sources:
      - type: file
        path: /data/agent-messages.jsonl
"""
CASES = ("002-food-doordash", "007-food-instacart", "047-care-taskrabbit")


@pytest.fixture(autouse=True)
def fixed_tree(monkeypatch):
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": False, "changes": ""})


@pytest.fixture
def checkout(tmp_path):
    """bench_dir with source/ = a small ClawBench checkout."""
    src = tmp_path / "bench" / "clawbench" / "source"
    for suite in ("v1-lite", "v1", "v2"):
        for c in CASES:
            d = src / "test-cases" / suite / c
            d.mkdir(parents=True)
            (d / "task.json").write_text(json.dumps({"instruction": f"do {c}", "eval_schema": {"url_pattern": "x", "method": "POST"}, "time_limit": 30}))
    h = src / cb.HARNESS_DIR
    h.mkdir(parents=True)
    (h / "harnesses.yaml").write_bytes(YAML.replace("\n", "\r\n").encode())             # a Windows checkout
    (src / ".env").write_text('PURELY_MAIL_API_KEY="pm-test"\nPURELY_MAIL_DOMAIN="x.cc"\n')
    return src.parent


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "harness" / "__pycache__").mkdir(parents=True)
    (root / "harness" / "__pycache__" / "x.pyc").write_bytes(b"x")
    (root / "harness" / "headless.py").write_text("print('h')\n")
    (root / "serve" / "web").mkdir(parents=True)
    (root / "serve" / "web" / "index.html").write_text("<html>")
    (root / "serve" / "tests").mkdir()
    (root / "serve" / "tests" / "test_x.py").write_text("")
    (root / "serve" / "longform.py").write_text("x = 1\n")
    return root


def ctx_for(bench_dir: Path, run_dir: Path, name: str, settings=None, stop=lambda: False) -> TrialContext:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "settings.json").write_text(json.dumps({"model": MODEL, "settings": settings or {}}))
    return TrialContext(run_dir, run_dir / "trials" / name, bench_dir, dict(settings or {}), stop)


class FakeClawBench:
    """Stands in for `uv run ... clawbench-run`: writes what ClawBench writes for a scenario, and records every call."""

    def __init__(self, scenarios=None):
        self.scenarios = scenarios or {}
        self.calls = []

    def __call__(self, args, cwd=None, env=None, timeout=None, should_stop=None, log_path=None):
        self.calls.append({"args": list(args), "env": env, "timeout": timeout, "cwd": cwd})
        if "clawbench-run" not in args:
            return Result(0, "", "")
        out = Path(args[args.index("--output-dir") + 1])
        harness, case = args[args.index("--harness") + 1], Path(args[args.index("clawbench-run") + 1]).name
        s = self.scenarios.get((harness, case), self.scenarios.get(harness, {}))
        if s.get("crash"):
            return Result(2, "Traceback: boom", "")
        run = out / "m" / f"{harness}-{case}-m-1"
        (run / "data").mkdir(parents=True)
        intercepted = s.get("intercepted", False)
        stop = s.get("stop_reason", "agent_exited" if intercepted else "time_limit_exceeded")
        (run / "data" / "interception.json").write_text(json.dumps({"intercepted": intercepted, "stop_reason": stop, "stop_description": "Session stopped: " + stop,
                                                                     "request": {"method": "POST", "url": "https://shop.example/api/order", "body": {"item": "x"}} if intercepted else None}))
        events = s.get("events", [{"type": "tool_call", "name": "browser_open", "args": {"url": "https://shop.example"}, "decision": "executed"},
                                  {"type": "tool_result", "name": "browser_open"}, {"type": "assistant", "text": "I placed the order."}])
        (run / "data" / "agent-messages.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        (run / "data" / "actions.jsonl").write_text(json.dumps({"type": "click", "url": "https://shop.example"}) + "\n")
        meta = {"intercepted": intercepted, "duration_seconds": 61, "failure_category": s.get("category"), "infra_flags": s.get("flags", []),
                "failure_reason": s.get("reason"), "usage": {"total_tokens": s.get("tokens", 1500)}, "run_metrics": {"stop_reason": stop, "actions": s.get("actions", 3)}}
        (run / "run-meta.json").write_text(json.dumps(meta))
        if "judge" in s:
            (run / "judge.json").write_text(json.dumps(s["judge"]))
        return Result(0 if intercepted else 1, "ok", "")


def make(checkout, repo, runner, **kw):
    return cb.ClawBenchAdapter(runner=runner, which=lambda n: "/bin/" + n, environ={"PATH": "/bin", "OPENAI_API_KEY": "sk-real", "HF_TOKEN": "hf", "KEEP": "1"},
                               repo_root=repo, **kw)


# -- planning -------------------------------------------------------------------------------------------------------

def test_the_subsets_pick_their_cases_and_smoke_runs_the_two_baselines_on_two_lite_cases(checkout, repo):
    a = make(checkout, repo, FakeClawBench())
    assert a.plan({"subset": "lite", "trials": 2}, checkout)[:2] == [("basalt", CASES[0], 1), ("basalt", CASES[0], 2)]
    assert len(a.plan({"subset": "v2"}, checkout)) == 3
    smoke = a.plan({"subset": "smoke"}, checkout)
    assert smoke == [(h, c, 1) for h in ("null", "random-click") for c in CASES[:2]]
    assert a.plan({"subset": "lite", "limit": 1}, checkout) == [("basalt", CASES[0], 1)]
    assert a.plan({"subset": "lite", "tasks": [CASES[2]]}, checkout) == [("basalt", CASES[2], 1)]
    with pytest.raises(ValueError, match="no such ClawBench case"):
        a.plan({"subset": "lite", "tasks": ["nope"]}, checkout)
    with pytest.raises(ValueError, match="unknown subset"):
        a.plan({"subset": "huge"}, checkout)
    with pytest.raises(ValueError, match="harness is one of"):
        a.plan({"subset": "lite", "harness": "hermes"}, checkout)
    with pytest.raises(FileNotFoundError, match="import the benchmark first"):
        a.plan({"subset": "lite"}, checkout.parent / "empty")


# -- adding our harness ---------------------------------------------------------------------------------------------

def test_our_harness_is_added_once_with_its_payload_and_lf_scripts(checkout, repo):
    src = checkout / "source"
    dest = cb.install_harness(src, repo)
    assert dest == src / cb.HARNESS_DIR / "basalt"
    for name in ("Dockerfile.tadamoo", "setup-tadamoo.sh", "run-tadamoo.sh", "usage-emitter.py", "tadamoo_driver.py"):
        assert b"\r" not in (dest / name).read_bytes(), name
    assert (dest / "payload" / "harness" / "headless.py").is_file() and (dest / "payload" / "serve" / "longform.py").is_file()
    assert not (dest / "payload" / "harness" / "__pycache__").exists()
    assert not (dest / "payload" / "serve" / "web").exists() and not (dest / "payload" / "serve" / "tests").exists()
    cb.install_harness(src, repo)                                   # again: the entry is not doubled
    text = (src / cb.HARNESS_DIR / "harnesses.yaml").read_text()
    assert text.count("name: basalt") == 1 and text.count(cb.BEGIN) == 1 and "\r" not in text
    yaml = pytest.importorskip("yaml")
    entries = {h["name"]: h for h in yaml.safe_load(text)["harnesses"]}
    assert set(entries) == {"openclaw", "null", "basalt"}
    e = entries["basalt"]
    for rel in (e["dockerfile"], e["setup_script"], e["run_script"], e["usage_emitter"], *e["extra_files"]):
        assert (src / cb.HARNESS_DIR / rel).is_file(), rel


def test_every_file_the_dockerfile_copies_is_in_the_folder_the_adapter_builds(checkout, repo):
    dest = cb.install_harness(checkout / "source", repo)
    copied = re.findall(r"^COPY harnesses/basalt/(\S+)", (dest / "Dockerfile.tadamoo").read_text(), re.M)
    assert {"payload", "setup-tadamoo.sh", "run-tadamoo.sh", "tadamoo_driver.py", "usage-emitter.py"} <= set(copied)
    for rel in copied:
        assert (dest / rel).exists(), rel


def test_a_folder_that_is_not_clawbench_is_refused(tmp_path, repo):
    with pytest.raises(FileNotFoundError, match="not a ClawBench checkout"):
        cb.install_harness(tmp_path, repo)


def test_the_payload_imports_on_its_own(checkout, repo, tmp_path):
    """The copied harness has to run from the image without anything else of this repository: a copy of the real package imports headless and containerkit."""
    import subprocess
    real = Path(cb.REPO_ROOT)
    dest = tmp_path / "payload"
    import shutil
    for name in cb.PAYLOAD:
        shutil.copytree(real / name, dest / name, ignore=cb._ignore)
    code = "import tadamoo.headless, tadamoo.bench.adapters.containerkit, tadamoo.bench.adapters; print('ok')"
    out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(dest)!r}); {code}"], capture_output=True, text=True, cwd=str(tmp_path))
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-800:]


def test_models_yaml_names_the_container_and_the_host_side_and_needs_an_address():
    doc = json.loads(cb.models_yaml(dict(MODEL, name="Qwen3.8 Flash/Next")))
    agent, judge = doc["Qwen3.8-Flash-Next"], doc["Qwen3.8-Flash-Next-judge"]
    assert agent["base_url"] == "http://host.docker.internal:7232/v1" and judge["base_url"] == "http://127.0.0.1:7232/v1"
    assert agent["api_type"] == judge["api_type"] == "openai-completions" and agent["api_key"] == "local"
    with pytest.raises(ValueError, match="recorded no model address"):
        cb.models_yaml(dict(MODEL, endpoint=""))


# -- running a trial ------------------------------------------------------------------------------------------------

def test_the_command_runs_clawbench_on_the_case_with_the_local_model_and_no_cloud_keys(checkout, repo, tmp_path):
    fake = FakeClawBench({"basalt": {"intercepted": True, "judge": {"match": True, "reason": "right item"}}})
    a = make(checkout, repo, fake)
    ctx = ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite", "judge": "local"})
    res = a.run_trial(("basalt", CASES[0], 1), ctx)
    args = fake.calls[0]["args"]
    assert args[:3] == ["/bin/uv", "run", "--project"] and "clawbench-run" in args
    assert args[args.index("clawbench-run") + 1].endswith(f"test-cases/v1-lite/{CASES[0]}".replace("/", args[args.index("clawbench-run") + 1][-len(CASES[0]) - 1]))
    assert args[args.index("clawbench-run") + 2] == "Qwen3.8-Flash-Next"
    assert args[args.index("--harness") + 1] == "basalt" and "--no-upload" in args and "--no-build" not in args
    assert args[args.index("--judge") + 1] == "Qwen3.8-Flash-Next-judge" and "--no-judge" not in args
    env = fake.calls[0]["env"]
    assert "OPENAI_API_KEY" not in env and "HF_TOKEN" not in env and env["KEEP"] == "1" and env["CLAWBENCH_WORKSPACE"].endswith("workspace")
    assert fake.calls[0]["timeout"] == 30 * 60 + cb.CASE_ALLOWANCE_S
    models = json.loads((checkout / "workspace" / "models" / "models.yaml").read_text())
    assert models["Qwen3.8-Flash-Next"]["base_url"].startswith("http://host.docker.internal:7232")
    assert res.passed is True and res.score == 1.0 and res.tokens == 1500 and res.seconds == 61
    # the second case reuses the image
    a.run_trial(("basalt", CASES[1], 1), ctx_for(checkout, tmp_path / "run", "t2", {"subset": "lite", "judge": "local"}))
    assert "--no-build" in fake.calls[1]["args"]


def test_the_judge_free_mode_passes_on_interception_alone(checkout, repo, tmp_path):
    fake = FakeClawBench({"basalt": {"intercepted": True}})
    res = make(checkout, repo, fake).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite", "judge": "none"}))
    assert "--no-judge" in fake.calls[0]["args"] and "--judge" not in fake.calls[0]["args"]
    assert res.passed is True and res.facts["judge"] == "none"


def test_a_judge_that_disagrees_fails_the_trial_and_says_so(checkout, repo, tmp_path):
    fake = FakeClawBench({"basalt": {"intercepted": True, "judge": {"match": False, "reason": "wrong item"}}})
    res = make(checkout, repo, fake).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite"}))
    assert res.passed is False and res.facts["judge_disagrees"] is True and res.facts["judge_match"] is False
    assert "wrong item" in (tmp_path / "run" / res.verifier_output_path).read_text()
    kind = analysis.classify(res, analysis._read_events(tmp_path / "run" / res.trajectory_path), (tmp_path / "run" / res.verifier_output_path).read_text())
    assert kind[0] == "judge_disagreement"


def test_a_judge_that_gave_no_verdict_leaves_the_trial_unjudged(checkout, repo, tmp_path):
    fake = FakeClawBench({"basalt": {"intercepted": True, "judge": {"match": None, "reason": "judge_setup_failed: connection refused"}}})
    res = make(checkout, repo, fake).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite"}))
    assert res.passed is None and not res.valid and "no verdict" in res.error and res.facts["class"] == "environment_problem"


def test_a_case_that_was_not_intercepted_fails_with_its_stop_reason_as_a_timeout(checkout, repo, tmp_path):
    fake = FakeClawBench({"basalt": {"intercepted": False, "stop_reason": "time_limit_exceeded", "category": "model_not_intercepted"}})
    res = make(checkout, repo, fake).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite"}))
    assert res.passed is False and "time_limit_exceeded" in res.error
    events = analysis._read_events(tmp_path / "run" / res.trajectory_path)
    assert events[-1] == {"type": "stop", "reason": "timeout"} and events[0]["type"] == "tool_call"
    assert analysis.classify(res, events, (tmp_path / "run" / res.verifier_output_path).read_text())[0] == "timeout"


def test_an_infrastructure_failure_is_not_the_agents_failure(checkout, repo, tmp_path):
    for scenario in ({"category": "infra_failure", "reason": "infra_failure: container build exited 1"}, {"category": "api_or_credit", "reason": "model_api_preflight: down"},
                     {"stop_reason": "chrome_cdp_timeout"}, {"stop_reason": "basalt_failed"}):
        fake = FakeClawBench({"basalt": scenario})
        res = make(checkout, repo, fake).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite"}))
        assert res.passed is None and res.facts["class"] == "environment_problem", scenario


def test_a_crash_with_no_output_is_unjudged_and_says_what_it_saw(checkout, repo, tmp_path):
    res = make(checkout, repo, FakeClawBench({"basalt": {"crash": True}})).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite"}))
    assert res.passed is None and "no run-meta.json" in res.error and "exit code 2" in res.error and "boom" in res.error


def test_a_stopped_trial_removes_the_container_left_behind(checkout, repo, tmp_path):
    calls = []

    def runner(args, cwd=None, env=None, timeout=None, should_stop=None, log_path=None):
        calls.append(list(args))
        if "clawbench-run" in args:
            return Result(130, "", "stopped on request")
        if "ps" in args:
            return Result(0, "0123456789ab\nabcdef012345\n", "")
        return Result(0, "", "")

    res = make(checkout, repo, runner).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite"}, stop=lambda: True))
    assert res.passed is None and "stopped" in res.error
    assert calls[1][:3] == ["docker", "ps", "-aq"] and calls[1][-1] == f"name=clawbench-basalt-{CASES[0]}-"
    assert calls[2] == ["docker", "rm", "-f", "0123456789ab", "abcdef012345"]


def test_an_earlier_attempt_is_not_read_as_this_one(checkout, repo, tmp_path):
    ctx = ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite"})
    stale = ctx.trial_dir / "out" / "m" / "old"
    stale.mkdir(parents=True)
    (stale / "run-meta.json").write_text(json.dumps({"intercepted": True}))
    res = make(checkout, repo, FakeClawBench({"basalt": {"crash": True}})).run_trial(("basalt", CASES[0], 1), ctx)
    assert res.passed is None


# -- the smoke test --------------------------------------------------------------------------------------------------

def test_the_baselines_prove_the_pipeline_when_nothing_is_intercepted(checkout, repo, tmp_path):
    fake = FakeClawBench({"null": {"events": [], "flags": ["zero_api_calls", "zero_actions"], "category": "infra_failure", "tokens": 0},
                          "random-click": {"events": [], "category": "model_not_intercepted", "tokens": 0}})
    a = make(checkout, repo, fake)
    spec = make_spec("clawbench", id="clawbench", primary_metric="reward_rate", official_cloud_judge=True)
    run = BenchRun(tmp_path / "base", spec, {"subset": "smoke"}, adapter=a, model=MODEL, power_limit_w=400.0, root=tmp_path)
    (run.bench_dir).parent.mkdir(parents=True, exist_ok=True)
    run.bench_dir = checkout
    state = run.execute()
    assert state["status"] == "done" and state["total"] == 4 and state["invalid"] == 0 and state["passed"] == 0 and state["failed"] == 4
    assert all("--no-judge" in c["args"] for c in fake.calls if "clawbench-run" in c["args"])
    results = json.loads((run.dir / "results.json").read_text())
    m = results["metrics"]
    assert results["provisional"] is True and m["smoke_ok"] is True and m["intercepted_rate"] == 0.0 and set(m["by_harness"]) == {"null", "random-click"}
    # the harness never saw the do-nothing flags as a failure: the trials are judged and sit in the journal
    assert all(valid for valid, _r in read_trials(run.dir).values())


def test_a_do_nothing_harness_that_gets_intercepted_fails_the_smoke_test(checkout, repo, tmp_path):
    fake = FakeClawBench({"null": {"intercepted": True, "stop_reason": "eval_matched"}})
    a = make(checkout, repo, fake)
    results = {}
    for key in a.plan({"subset": "smoke"}, checkout):
        results[key] = a.run_trial(key, ctx_for(checkout, tmp_path / "run", "-".join(map(str, key)), {"subset": "smoke"}))
    assert a.score(results)["smoke_ok"] is False


# -- the score and the whole pipeline --------------------------------------------------------------------------------

def test_the_score_reports_both_rates_and_labels_the_judge(checkout, repo):
    a = make(checkout, repo, FakeClawBench())

    def res(passed, intercepted, judge="local"):
        return TrialResult(passed=passed, facts={"intercepted": intercepted, "judge": judge, "harness": "basalt"})

    got = a.score({("basalt", "a", 1): res(True, True), ("basalt", "b", 1): res(False, True), ("basalt", "c", 1): res(False, False), ("basalt", "d", 1): TrialResult(error="x")})
    assert got["reward_rate"] == pytest.approx(1 / 3) and got["intercepted_rate"] == pytest.approx(2 / 3) and got["of"] == 3 and got["invalid"] == 1
    assert "provisional" in " ".join(got["notes"]) and got["reward_basis"].startswith("interception and the local model")
    free = a.score({("basalt", "a", 1): res(True, True, "none")})
    assert free["reward_basis"].startswith("interception only") and free["reward_rate"] == 1.0


def test_a_whole_run_through_the_runner_and_the_failure_report(checkout, repo, tmp_path):
    fake = FakeClawBench({("basalt", CASES[0]): {"intercepted": True, "judge": {"match": True, "reason": "ok"}},
                          ("basalt", CASES[1]): {"intercepted": False, "stop_reason": "time_limit_exceeded"},
                          ("basalt", CASES[2]): {"intercepted": True, "judge": {"match": False, "reason": "wrong quantity"}}})
    a = make(checkout, repo, fake)
    spec = make_spec("clawbench", id="clawbench", primary_metric="reward_rate", official_cloud_judge=True)
    run = BenchRun(tmp_path / "base", spec, {"subset": "lite", "judge": "local"}, adapter=a, model=MODEL, power_limit_w=400.0, root=tmp_path)
    run.bench_dir = checkout
    state = run.execute()
    assert state["status"] == "done" and state["passed"] == 1 and state["failed"] == 2
    metrics = json.loads((run.dir / "results.json").read_text())["metrics"]
    assert metrics["reward_rate"] == pytest.approx(1 / 3) and metrics["intercepted_rate"] == pytest.approx(2 / 3)
    report = analysis.report(run.dir)
    assert report["failed"] == 2 and {f["class"] for f in report["failures"]} == {"timeout", "judge_disagreement"}


# -- the driver that runs inside the container -----------------------------------------------------------------------

def load_driver(monkeypatch, tmp_path):
    import importlib.util
    monkeypatch.setenv("TADAMOO_HOME", str(cb.REPO_ROOT))
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TADAMOO_WORKSPACE", str(tmp_path / "work"))
    spec = importlib.util.spec_from_file_location("basalt_driver_under_test", cb.ASSETS / "basalt" / "tadamoo_driver.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_driver_points_the_harness_at_the_container_browser_and_refuses_the_ways_around_it(monkeypatch, tmp_path):
    d = load_driver(monkeypatch, tmp_path)
    argv = d.build_argv({"BASE_URL": "http://host.docker.internal:7232/v1", "TIME_LIMIT_S": "1800", "CLAWBENCH_BROWSER_CDP_URL": "http://127.0.0.1:9222", "API_KEY": "local"}, "task")
    assert argv[argv.index("--endpoint") + 1] == "http://host.docker.internal:7232" and argv[argv.index("--browser-cdp-url") + 1] == "http://127.0.0.1:9222"
    assert "--browser" in argv and argv[argv.index("--timeout-seconds") + 1] == "1755"
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--deny"] == ["group:shell", "group:web", "group:agents", "group:mcp", "group:phone"]
    from tadamoo.headless import parse_args
    from tadamoo.stack import agent_settings
    got = agent_settings(parse_args(argv))
    assert got["browser"] is True and got["browser_cdp_url"] == "http://127.0.0.1:9222" and got["permission_mode"] == "auto" and got["rule_of_two"] == "off"
    assert got["deny"] == list(d.DENIED) and got["use_memory"] is False


def test_the_driver_writes_the_trajectory_the_usage_and_ends_with_a_reason(monkeypatch, tmp_path):
    d = load_driver(monkeypatch, tmp_path)
    import tadamoo.headless as headless

    def fake_run(cfg, prompt, client=None, on_event=None):
        assert cfg.browser_cdp_url == "http://127.0.0.1:9222" and "WEBMAIL" not in prompt and "COMPLETION NOTE" in prompt
        on_event({"type": "tool_start", "call_id": "1", "name": "browser_open", "arguments": {"url": "https://a.example"}})
        on_event({"type": "tool_end", "call_id": "1", "name": "browser_open", "status": "completed", "decision": "executed", "output": "Title: A"})
        on_event({"type": "tool_start", "call_id": "2", "name": "shell_command", "arguments": {"command": "curl x"}})
        on_event({"type": "tool_end", "call_id": "2", "name": "shell_command", "status": "failed", "decision": "denied", "output": "refused"})
        return {"status": "success", "stop_reason": "final", "error": None, "turns_count": 3, "tokens": {"prompt": 900, "completion": 100, "cached": 5}}

    monkeypatch.setattr(headless, "run_headless", fake_run)
    monkeypatch.setenv("INSTRUCTION", "Order a pizza")
    monkeypatch.setenv("BASE_URL", "http://host.docker.internal:7232/v1")
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.setenv("CLAWBENCH_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    assert d.main() == 0
    events = [json.loads(l) for l in (tmp_path / "data" / "agent-messages.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "tool_call", "tool_result", "stop"]
    assert events[2]["decision"] == "refused" and events[-1] == {"type": "stop", "reason": "final"}
    usage = json.loads((tmp_path / "data" / "usage.jsonl").read_text())
    assert usage["input_tokens"] == 900 and usage["output_tokens"] == 100 and usage["model"] == "m"
    assert json.loads((tmp_path / "data" / "basalt-result.json").read_text())["turns_count"] == 3


def test_the_driver_ends_the_trajectory_when_the_harness_fails_or_is_stopped(monkeypatch, tmp_path):
    d = load_driver(monkeypatch, tmp_path)
    import tadamoo.headless as headless
    monkeypatch.setenv("INSTRUCTION", "x")
    monkeypatch.setenv("BASE_URL", "http://h:1/v1")

    def boom(cfg, prompt, client=None, on_event=None):
        raise RuntimeError("cannot reach the TaDaMoo server")

    monkeypatch.setattr(headless, "run_headless", boom)
    assert d.main() == 1
    last = [json.loads(l) for l in (tmp_path / "data" / "agent-messages.jsonl").read_text().splitlines()][-2:]
    assert last[0]["type"] == "error" and "cannot reach" in last[0]["text"] and last[1] == {"type": "stop", "reason": "error"}

    (tmp_path / "data" / "agent-messages.jsonl").unlink()
    (tmp_path / "data" / ".stop-reason").write_text("time_limit_exceeded")

    def interrupted(cfg, prompt, client=None, on_event=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(headless, "run_headless", interrupted)
    assert d.main() == 1
    assert json.loads((tmp_path / "data" / "agent-messages.jsonl").read_text().splitlines()[-1]) == {"type": "stop", "reason": "timeout"}


def test_the_webmail_login_is_written_out_for_the_agent(monkeypatch, tmp_path):
    d = load_driver(monkeypatch, tmp_path)
    info = tmp_path / "my-info"
    info.mkdir()
    (info / "email_credentials.json").write_text(json.dumps({"email": "a@clawbench.cc", "password": "p4ss"}))
    note = d.credentials_note(info)
    assert "a@clawbench.cc" in note and "p4ss" in note and "purelymail.com/user/login" in note
    assert d.credentials_note(tmp_path / "none") == ""


def test_the_adapter_is_found_by_the_lazy_loader_and_has_the_runners_protocol():
    from tadamoo.bench import runner
    from tadamoo.bench.adapters import load
    adapter = runner.get_adapter(cb.ID, {}, lazy=True)
    assert load(cb.ID) is cb.ClawBenchAdapter and isinstance(adapter, cb.ClawBenchAdapter) and adapter.id == "clawbench"
    assert all(callable(getattr(adapter, n)) for n in ("plan", "run_trial", "score", "failure_facts"))


def test_the_workspace_gets_the_benchmarks_mail_settings_and_a_checkout_without_them_is_unjudged(checkout, repo, tmp_path):
    fake = FakeClawBench({"basalt": {"intercepted": True, "judge": {"match": True}}})
    make(checkout, repo, fake).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "t", {"subset": "lite"}))
    assert (checkout / "workspace" / ".env").read_text() == (checkout / "source" / ".env").read_text()
    (checkout / "source" / ".env").unlink()
    res = make(checkout, repo, FakeClawBench()).run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "u", {"subset": "lite"}))
    assert res.passed is None and "disposable e-mail" in res.error


def test_podman_reaches_the_host_by_its_own_name(checkout, repo, tmp_path):
    a = cb.ClawBenchAdapter(runner=FakeClawBench(), which=lambda n: "/bin/" + n, environ={"CONTAINER_ENGINE": "podman"}, repo_root=repo)
    a.run_trial(("basalt", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "p", {"subset": "lite"}))
    models = json.loads((checkout / "workspace" / "models" / "models.yaml").read_text())
    assert models["Qwen3.8-Flash-Next"]["base_url"] == "http://host.containers.internal:7232/v1"


def test_a_baseline_run_that_shows_the_recorder_is_broken_proves_nothing(checkout, repo, tmp_path):
    for scenario in ({"random-click": {"actions": 0}}, {"random-click": {"flags": ["missing:data/requests.jsonl"]}}, {"null": {"flags": ["missing:data/interception.json"]}}):
        fake = FakeClawBench(scenario)
        harness = next(iter(scenario))
        res = make(checkout, repo, fake).run_trial((harness, CASES[0], 1), ctx_for(checkout, tmp_path / "run", "x", {"subset": "smoke"}))
        assert res.passed is None and res.facts["class"] == "environment_problem" and ("recorded no action" in res.error or "recorder left out" in res.error), scenario
    ok = make(checkout, repo, FakeClawBench({"null": {"flags": ["zero_api_calls", "zero_actions", "zero_requests"], "actions": 0}})).run_trial(
        ("null", CASES[0], 1), ctx_for(checkout, tmp_path / "run", "y", {"subset": "smoke"}))
    assert ok.passed is False and ok.valid                     # a do-nothing run has no action by design
