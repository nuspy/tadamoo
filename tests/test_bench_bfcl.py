"""The BFCL adapter (tadamoo/bench/adapters/bfcl.py) against a fake BFCL checkout and a fake `bfcl generate` / `bfcl evaluate`: planning, the commands and the
environment, reading BFCL's result and score files, and the score. No BFCL install, no model."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.bench import analysis
from tadamoo.bench.adapters import bfcl
from tadamoo.bench.importer import Result
from tadamoo.bench.runner import BenchRun, TrialContext, TrialResult
from tadamoo.eval import resume
from bench_support import MODEL, make_spec

CATS = {"simple_python": 5, "multiple": 4, "parallel": 3, "irrelevance": 3, "live_simple": 3, "multi_turn_base": 3, "simple_java": 2, "simple_javascript": 1, "parallel_multiple": 2}


@pytest.fixture(autouse=True)
def fixed_tree(monkeypatch):
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": False, "changes": ""})


@pytest.fixture
def checkout(tmp_path):
    src = tmp_path / "bench" / "bfcl-v4" / "source"
    data = src / bfcl.PROJECT / "bfcl_eval" / "data"
    data.mkdir(parents=True)
    for cat, n in CATS.items():
        (data / f"BFCL_v4_{cat}.json").write_text("\n".join(json.dumps({"id": f"{cat}_{i}", "question": [[{"role": "user", "content": "q"}]]}) for i in range(n)) + "\n")
    return src.parent


def ctx_for(bench_dir: Path, run_dir: Path, name: str, settings=None, stop=lambda: False) -> TrialContext:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "settings.json").write_text(json.dumps({"model": MODEL, "settings": settings or {}}))
    return TrialContext(run_dir, run_dir / "trials" / name, bench_dir, dict(settings or {}), stop)


class FakeBfcl:
    """Stands in for BFCL's command line. `outcomes` maps a case id to 'ok' (default), 'wrong', 'undecodable', 'inference-error', 'context' or 'missing'."""

    def __init__(self, outcomes=None, generate_code=0, write_score=True):
        self.outcomes, self.generate_code, self.write_score = outcomes or {}, generate_code, write_score
        self.calls = []

    def __call__(self, args, cwd=None, env=None, timeout=None, should_stop=None, log_path=None):
        self.calls.append({"args": list(args), "env": env, "cwd": cwd, "timeout": timeout})
        root = Path(env["BFCL_PROJECT_ROOT"])
        if "generate" in args:
            ids = json.loads((root / "test_case_ids_to_generate.json").read_text())
            (category, wanted), = ids.items()
            out = root / "result" / bfcl.REGISTRY_NAME / "non_live"
            out.mkdir(parents=True, exist_ok=True)
            lines = []
            for i in wanted:
                how = self.outcomes.get(i, "ok")
                if how == "missing":
                    continue
                result = ("Error during inference: connection refused" if how == "inference-error" else
                          "Error during inference: context length exceeded, 40000 tokens" if how == "context" else
                          [{"calculate_area": json.dumps({"base": 10, "height": 5})}])
                lines.append(json.dumps({"id": i, "result": result, "input_token_count": 120, "output_token_count": 30, "latency": 1.5}))
            (out / f"BFCL_v4_{category}_result.json").write_text("\n".join(lines) + "\n")
            return Result(self.generate_code, "generated", "")
        category = args[args.index("--test-category") + 1]
        if not self.write_score:
            return Result(1, "", "Traceback")
        done = {json.loads(l)["id"] for l in (next((root / "result" / bfcl.REGISTRY_NAME).rglob(f"BFCL_v4_{category}_result.json"))).read_text().splitlines()}
        bad = [i for i in done if self.outcomes.get(i, "ok") in ("wrong", "undecodable", "context")]
        header = {"accuracy": 1 - len(bad) / len(done), "correct_count": len(done) - len(bad), "total_count": len(done)}
        rows = [header]
        for i in sorted(bad):
            kind = self.outcomes[i]
            rows.append({"id": i, "valid": False, "error": ["Invalid value for parameter 'base'. Expected 10"] if kind == "wrong" else ["Failed to decode the AST"],
                         "error_type": {"wrong": "value_error:others", "context": "inference_error"}.get(kind, "ast_decoder:decoding_failure"), "prompt": {}, "model_result_raw": "x", "possible_answer": {"f": {"base": [10]}}})
        out = root / "score" / bfcl.REGISTRY_NAME / "non_live"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"BFCL_v4_{category}_score.json").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return Result(0, "evaluated", "")


def make(runner):
    return bfcl.BfclAdapter(runner=runner, which=lambda n: "/bin/" + n, environ={"PATH": "/bin", "OPENAI_API_KEY": "sk-real", "SERPAPI_API_KEY": "s", "KEEP": "1"})


# -- planning -------------------------------------------------------------------------------------------------------

def test_the_subsets_pick_their_categories_and_cases(checkout):
    a = make(FakeBfcl())
    smoke = a.plan({"subset": "smoke"}, checkout)
    assert [k[0] for k in smoke][:3] == ["simple_python"] * 3 and len(smoke) == 3 * 6 and smoke[0] == ("simple_python", "simple_python_0", 1)
    assert {k[0] for k in a.plan({"subset": "simple"}, checkout)} == set(bfcl.SIMPLE) and len(a.plan({"subset": "simple"}, checkout)) == 5 + 4 + 3 + 2 + 1 + 2
    assert a.plan({"subset": "category", "categories": "multiple, parallel", "limit": 2}, checkout) == [("multiple", "multiple_0", 1), ("multiple", "multiple_1", 1),
                                                                                                           ("parallel", "parallel_0", 1), ("parallel", "parallel_1", 1)]
    assert a.plan({"subset": "category", "categories": ["parallel"], "tasks": ["parallel_2"]}, checkout) == [("parallel", "parallel_2", 1)]
    assert a.plan({"subset": "smoke", "trials": 5}, checkout) == a.plan({"subset": "smoke"}, checkout)          # one answer per case, whatever the trials say


def test_categories_that_are_not_run_or_do_not_exist_are_refused_with_the_reason(checkout):
    a = make(FakeBfcl())
    with pytest.raises(ValueError, match="web_search_base is not run here"):
        a.plan({"subset": "category", "categories": ["web_search_base"]}, checkout)
    with pytest.raises(ValueError, match="not a BFCL v4 category"):
        a.plan({"subset": "category", "categories": ["nope"]}, checkout)
    with pytest.raises(ValueError, match="no such BFCL case"):
        a.plan({"subset": "category", "categories": ["multiple"], "tasks": ["multiple_99"]}, checkout)
    with pytest.raises(ValueError, match="unknown subset"):
        a.plan({"subset": "huge"}, checkout)
    with pytest.raises(FileNotFoundError, match="import the benchmark first"):
        a.plan({"subset": "category", "categories": ["live_parallel"]}, checkout)


# -- the commands -----------------------------------------------------------------------------------------------------

def test_one_category_is_generated_and_scored_once_for_all_its_cases_with_the_local_engine(checkout, tmp_path):
    fake = FakeBfcl()
    a = make(fake)
    settings = {"subset": "category", "categories": ["multiple"], "limit": 3}
    results = [a.run_trial(k, ctx_for(checkout, tmp_path / "run", f"t{i}", settings)) for i, k in enumerate(a.plan(settings, checkout))]
    assert [r.passed for r in results] == [True, True, True] and len(fake.calls) == 2                      # one generate, one evaluate
    gen, ev = fake.calls
    assert gen["args"][:4] == ["/bin/uv", "run", "--project", str(checkout / "source" / bfcl.PROJECT)] and "--python" in gen["args"] and "3.12" in gen["args"]
    tail = gen["args"][gen["args"].index("python") + 1:]
    assert tail[0].endswith("launcher.py") and tail[1:] == ["generate", "--model", "tadamoo-local-FC", "--run-ids", "--num-threads", "1"]
    assert ev["args"][-1] == "--partial-eval" and "evaluate" in ev["args"] and ev["args"][ev["args"].index("--test-category") + 1] == "multiple"
    env = gen["env"]
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:7232/v1" and env["OPENAI_API_KEY"] == "local" and "SERPAPI_API_KEY" not in env and env["KEEP"] == "1"
    assert env["TADAMOO_BFCL_REGISTRY"] == "tadamoo-local-FC" and env["TADAMOO_BFCL_MODEL"] == "Qwen3.8-Flash-Next" and env["TADAMOO_BFCL_HOST"] == "127.0.0.1"
    root = tmp_path / "run" / "bfcl"
    assert env["BFCL_PROJECT_ROOT"] == str(root)
    assert json.loads((root / "test_case_ids_to_generate.json").read_text()) == {"multiple": ["multiple_0", "multiple_1", "multiple_2"]}
    assert (root / ".env").read_text() == "OPENAI_API_KEY=local\nOPENAI_BASE_URL=http://127.0.0.1:7232/v1\n"
    assert gen["timeout"] == bfcl.GENERATE_S and ev["timeout"] == bfcl.EVALUATE_S


# -- reading the results ----------------------------------------------------------------------------------------------

def run_all(checkout, tmp_path, fake, settings):
    a = make(fake)
    return a, {k: a.run_trial(k, ctx_for(checkout, tmp_path / "run", "-".join(map(str, k)), settings)) for k in a.plan(settings, checkout)}


def test_a_rejected_answer_fails_with_bfcls_reason_and_a_decoding_failure_is_a_format_error(checkout, tmp_path):
    settings = {"subset": "category", "categories": ["simple_python"], "limit": 5}
    fake = FakeBfcl({"simple_python_1": "wrong", "simple_python_2": "undecodable"})
    a, res = run_all(checkout, tmp_path, fake, settings)
    assert [r.passed for r in res.values()] == [True, False, False, True, True]
    wrong, bad = res[("simple_python", "simple_python_1", 1)], res[("simple_python", "simple_python_2", 1)]
    assert "value_error:others" in wrong.error and "Expected 10" in wrong.error and wrong.tokens == 150 and wrong.seconds == 1.5
    assert bad.facts["class"] == "tool_format_error" and bad.facts["error_type"] == "ast_decoder:decoding_failure"
    events = analysis._read_events(tmp_path / "run" / bad.trajectory_path)
    assert events[0]["decision"] == "invalid" and events[-1] == {"type": "stop", "reason": "final"}
    assert analysis.classify(bad, events, (tmp_path / "run" / bad.verifier_output_path).read_text())[0] == "tool_format_error"
    ok = res[("simple_python", "simple_python_0", 1)]
    assert analysis._read_events(tmp_path / "run" / ok.trajectory_path)[0] == {"type": "tool_call", "name": "calculate_area", "args": {"base": 10, "height": 5}, "decision": "executed"}
    assert "accepted" in (tmp_path / "run" / ok.verifier_output_path).read_text()


def test_a_server_failure_during_generation_leaves_the_case_unjudged_but_a_context_overflow_is_the_models(checkout, tmp_path):
    settings = {"subset": "category", "categories": ["multiple"], "limit": 4}
    fake = FakeBfcl({"multiple_0": "inference-error", "multiple_1": "context", "multiple_2": "missing"})
    _a, res = run_all(checkout, tmp_path, fake, settings)
    r0, r1, r2, r3 = res.values()
    assert r0.passed is None and r0.facts["class"] == "environment_problem" and "connection refused" in r0.error
    assert r1.passed is False and r1.facts["class"] == "context_overflow"
    assert r2.passed is None and "no answer" in r2.error
    assert r3.passed is True


def test_a_context_overflow_error_counts_against_the_model(checkout, tmp_path):
    entry = {"id": "multiple_1", "result": "Error during inference: context length exceeded, 40000 tokens", "input_token_count": 0}
    failure = {"id": "multiple_1", "valid": False, "error": ["Model did not output a list of model responses"], "error_type": "inference_error"}
    ctx = ctx_for(checkout, tmp_path / "run", "x", {})
    res = bfcl.case_result("multiple_1", "multiple", entry, {"accuracy": 0.0}, failure, ctx)
    assert res.passed is False and res.facts["class"] == "context_overflow"


def test_a_failed_generation_or_a_missing_score_file_is_an_environment_problem_tried_twice_then_left(checkout, tmp_path):
    settings = {"subset": "category", "categories": ["multiple"], "limit": 3}
    a = make(lambda args, **kw: Result(1, "", "uv: boom") if "generate" in args else Result(0, "", ""))
    res = [a.run_trial(k, ctx_for(checkout, tmp_path / "run", f"d{i}", settings)) for i, k in enumerate(a.plan(settings, checkout))]
    assert all(r.passed is None and "bfcl generate failed (exit 1)" in r.error for r in res)
    counts = []
    a2 = make(lambda args, **kw: (counts.append(args), Result(1, "", "boom"))[1])
    for i, k in enumerate(a2.plan(settings, checkout)):
        a2.run_trial(k, ctx_for(checkout, tmp_path / "run2", f"e{i}", settings))
    assert len(counts) == bfcl.ATTEMPTS                                            # not one failing uv start for every case
    noscore = FakeBfcl(write_score=False)
    r = make(noscore).run_trial(("multiple", "multiple_0", 1), ctx_for(checkout, tmp_path / "run3", "n", {"subset": "category", "categories": ["multiple"], "limit": 1}))
    assert r.passed is None and "left no score file" in r.error


def test_a_stop_request_ends_the_category_without_a_result(checkout, tmp_path):
    fake = FakeBfcl()
    r = make(fake).run_trial(("multiple", "multiple_0", 1), ctx_for(checkout, tmp_path / "run", "s", {"subset": "category", "categories": ["multiple"]}, stop=lambda: True))
    assert r.passed is None and "stopped" in r.error and len(fake.calls) == 1


# -- the score and the whole run ----------------------------------------------------------------------------------------

def test_the_score_gives_accuracy_by_category_and_the_notes_say_what_this_measures():
    a = make(FakeBfcl())

    def r(passed, err=""):
        return TrialResult(passed=passed, facts={"error_type": err} if err else {})

    got = a.score({("a", "a_0", 1): r(True), ("a", "a_1", 1): r(False, "value_error:others"), ("b", "b_0", 1): r(True), ("b", "b_1", 1): TrialResult(error="x")})
    assert got["accuracy"] == pytest.approx(2 / 3) and got["of"] == 3 and got["invalid"] == 1 and got["categories"]["a"]["accuracy"] == 0.5
    assert got["macro_accuracy"] == pytest.approx(0.75) and got["errors_by_type"] == {"value_error:others": 1}
    assert "tests the served model" in got["notes"][0] and "not run" in got["notes"][2]


def test_a_whole_smoke_run_through_the_runner_and_the_failure_report(checkout, tmp_path):
    fake = FakeBfcl({"simple_python_1": "wrong", "parallel_0": "undecodable"})
    spec = make_spec("bfcl", id="bfcl-v4", primary_metric="accuracy")
    run = BenchRun(tmp_path / "base", spec, {"subset": "smoke"}, adapter=make(fake), model=MODEL, power_limit_w=400.0, root=tmp_path)
    run.bench_dir = checkout
    state = run.execute()
    assert state["status"] == "done" and state["total"] == 18 and state["failed"] == 2 and state["invalid"] == 0
    metrics = json.loads((run.dir / "results.json").read_text())["metrics"]
    assert metrics["accuracy"] == pytest.approx(16 / 18) and set(metrics["categories"]) == set(bfcl.SMOKE)
    assert sum(1 for c in fake.calls if "generate" in c["args"]) == 6
    report = analysis.report(run.dir)
    assert report["failed"] == 2 and {f["class"] for f in report["failures"]} == {"wrong_final_state", "tool_format_error"}


# -- the launcher that runs inside BFCL's environment -------------------------------------------------------------------

def load_launcher():
    spec = importlib.util.spec_from_file_location("bfcl_launcher_under_test", bfcl.LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_launcher_adds_the_served_model_and_starts_bfcls_command_line(monkeypatch):
    mod = load_launcher()
    seen = {}
    mapping = {}

    class Config:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Handler:
        pass

    fake = {"bfcl_eval": types.ModuleType("bfcl_eval"), "bfcl_eval.constants": types.ModuleType("bfcl_eval.constants"),
            "bfcl_eval.constants.model_config": types.ModuleType("bfcl_eval.constants.model_config"),
            "bfcl_eval.model_handler": types.ModuleType("bfcl_eval.model_handler"), "bfcl_eval.model_handler.api_inference": types.ModuleType("x"),
            "bfcl_eval.model_handler.api_inference.openai_completion": types.ModuleType("bfcl_eval.model_handler.api_inference.openai_completion"),
            "bfcl_eval.__main__": types.ModuleType("bfcl_eval.__main__")}
    fake["bfcl_eval.constants.model_config"].MODEL_CONFIG_MAPPING = mapping
    fake["bfcl_eval.constants.model_config"].ModelConfig = Config
    fake["bfcl_eval.model_handler.api_inference.openai_completion"].OpenAICompletionsHandler = Handler
    fake["bfcl_eval.__main__"].cli = lambda: seen.setdefault("cli", True)
    for name, module in fake.items():
        monkeypatch.setitem(sys.modules, name, module)
    for parent, child in (("bfcl_eval", "constants"), ("bfcl_eval", "model_handler"), ("bfcl_eval.constants", "model_config"),
                          ("bfcl_eval.model_handler", "api_inference"), ("bfcl_eval.model_handler.api_inference", "openai_completion")):
        setattr(sys.modules[parent], child, sys.modules[parent + "." + child])
    for k, v in {"TADAMOO_BFCL_REGISTRY": "tadamoo-local-FC", "TADAMOO_BFCL_MODEL": "Qwen", "OPENAI_BASE_URL": "http://127.0.0.1:7232/v1", "TADAMOO_BFCL_HOST": "127.0.0.1"}.items():
        monkeypatch.setenv(k, v)
    mod.main()
    entry = mapping["tadamoo-local-FC"]
    assert seen["cli"] and entry.model_name == "Qwen" and entry.model_handler is Handler and entry.is_fc_model is True and entry.input_price is None
    assert "_" not in "tadamoo-local-FC"
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    with pytest.raises(SystemExit, match="not to the local engine"):
        mod.main()


def test_the_adapter_is_found_by_the_lazy_loader_and_has_the_runners_protocol():
    from tadamoo.bench import runner
    from tadamoo.bench.adapters import load
    adapter = runner.get_adapter(bfcl.ID, {}, lazy=True)
    assert load(bfcl.ID) is bfcl.BfclAdapter and isinstance(adapter, bfcl.BfclAdapter) and adapter.id == "bfcl-v4"
    assert all(callable(getattr(adapter, n)) for n in ("plan", "run_trial", "score", "failure_facts"))
