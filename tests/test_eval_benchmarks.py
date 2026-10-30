"""The evaluation's phase-0 additions: a warning for a k below 5, the server's own timing and cache figures, the none/RAG/CAG comparison, and
public benchmarks (BFCL, Terminal-Bench, SWE-bench Pro) read from a folder. Every folder here is made by the test; no dataset is needed or fetched."""
import io
import json
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.eval import EvalRunner, generate_eval_report
from harness.eval.__main__ import main as eval_main
from harness.eval import benchmarks
from harness.eval.report import k_warning, strategy_rows, summarize
from harness.eval.runner import EvalResult, Trial
from harness.eval.serverlog import ServerTimings, summarize_requests
from harness.eval.tasks import Task, make_workspace
from test_agent_loop import ScriptedClient, call, say


def runner_for(*steps):
    return EvalRunner(client_factory=lambda t, r: ScriptedClient(*steps))


# -- k below 5 -----------------------------------------------------------------------------------------------------


def test_a_k_below_five_is_warned_about_in_words_and_5_is_not():
    assert "k = 3 is below 5" in k_warning(3) and "--k 5" in k_warning(3)
    assert k_warning(5) is None and k_warning(10) is None and k_warning("5") is None


@pytest.fixture
def server():
    from serve.engine.mock import MockEngine
    from serve.frontend import ChatTemplate
    from serve.handler import serve
    from serve.service import Service
    from serve.util.text import ByteTokenizer
    from test_e2e_mock_server import answer, tool_call

    root = Path(__file__).resolve().parents[1]
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, [tool_call("write_file", path="textutil.py", content="def reverse_words(s):\n    return ' '.join(reversed(s.split()))\n"),
                                    answer("Done.")] * 14, max_context=16384), tok, ChatTemplate(root / "serve" / "chat_template.jinja"))
    svc.telemetry = SimpleNamespace(snapshot=lambda: {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}})
    httpd = serve(svc, port=0)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def run_cli(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = eval_main(args)
    return code, out.getvalue(), err.getvalue()


def test_the_command_line_and_the_report_warn_when_k_is_below_five_and_the_server_figures_are_read(server, tmp_path):
    report, data = tmp_path / "r.md", tmp_path / "r.json"
    code, out, err = run_cli(["--set", "core", "--only", "write_function", "--k", "2", "--temperature", "0", "--endpoint", server, "--out", str(report), "--json", str(data)])
    assert code == 0, err
    assert "warning: k = 2 is below 5" in err
    text = report.read_text(encoding="utf-8")
    assert "> **Warning**: k = 2 is below 5" in text
    summary = json.loads(data.read_text(encoding="utf-8"))["summary"]
    assert summary["server"]["trials"] == 2 and summary["server"]["requests"] >= 2 and summary["server"]["ttft_median_s"] is not None
    assert "Seen by the server" in text and "| TTFT (s) |" in text
    code, out, err = run_cli(["--set", "core", "--only", "write_function", "--k", "5", "--temperature", "0", "--endpoint", server, "--no-server-timings"])
    assert code == 0 and "warning" not in err and "Seen by the server**: not read" in out


# -- the server's own figures ---------------------------------------------------------------------------------------


def log_events(*timing):
    return {"enabled": True, "last": 9, "events": [{"id": i + 1, "req": req, "kind": "timing", "data": data} for i, (req, data) in enumerate(timing)]}


def test_the_two_timing_events_of_a_request_are_merged_and_only_the_trials_window_is_read():
    asked = []

    def fetch(after):
        asked.append(after)
        return log_events(("r1", {"first_token_s": 0.4, "prompt_tokens": 100}), ("r1", {"cached_tokens": 60, "prefill_s": 0.3, "first_token_s": 0.4, "prompt_tokens": 100, "tokens_per_s": 90.0}),
                          ("r2", {"first_token_s": 0.2, "cached_tokens": 100, "prompt_tokens": 100, "prefill_s": 0.05}))
    timings = ServerTimings("http://x", fetch=fetch)
    mark = timings.mark()
    assert mark == 9 and asked == [1 << 62]
    rows = timings.requests_since(mark)
    assert len(rows) == 2 and rows[0] == {"first_token_s": 0.4, "prompt_tokens": 100, "cached_tokens": 60, "prefill_s": 0.3, "tokens_per_s": 90.0}
    seen = summarize_requests(rows)
    assert seen["requests"] == 2 and seen["ttft_median_s"] == pytest.approx(0.3) and seen["cached_share"] == pytest.approx(0.8)
    assert ServerTimings("http://x", fetch=lambda after: None).mark() is None
    assert ServerTimings("http://x", fetch=fetch).requests_since(None) == []


def test_a_trial_carries_the_servers_figures_and_the_report_shows_them():
    figures = iter([log_events(("a", {"first_token_s": 0.5, "prompt_tokens": 200, "cached_tokens": 150, "prefill_s": 0.1}))])
    timings = ServerTimings("http://x", fetch=lambda after: {"enabled": True, "last": 4, "events": []} if after == 1 << 62 else next(figures))
    task = Task(id="t", category="c", prompt="say hi", checks=[{"type": "answer_contains", "text": "hi"}], mode="tools", tools=[])
    result = EvalRunner(client_factory=lambda t, r: ScriptedClient(say("hi")), timings=timings).evaluate_task(task, 1)
    trial = result.trials[0]
    assert (trial.server_requests, trial.server_ttft_s, trial.server_prompt_tokens, trial.server_cached_tokens) == (1, 0.5, 200, 150)
    text = generate_eval_report([result], config={"k": 1})
    assert "median time to the first token 0.50 s" in text and "read from the cache 75.0%" in text and "| 0.50 |" in text


# -- none, RAG, CAG -------------------------------------------------------------------------------------------------


def test_the_report_compares_the_three_strategies_side_by_side():
    def result(strategy, passed, tokens, cached):
        trials = [Trial(repetition=i, passed=p, prompt_tokens=tokens, cached_tokens=cached, seconds=2.0, stop_reason="final") for i, p in enumerate(passed)]
        return EvalResult(f"q@{strategy}", f"knowledge/{strategy}", len(trials), 1.0 if any(passed) else 0.0, all(passed), trials)
    results = [result("none", [False, False], 100, 0), result("rag", [True, False], 300, 50), result("cag", [True, True], 900, 800)]
    rows = {r["strategy"]: r for r in strategy_rows(results)}
    assert rows["none"]["success"] == 0.0 and rows["rag"]["success"] == 0.5 and rows["cag"]["pass_pow_k"] == 1 and rows["cag"]["cache"] == pytest.approx(800 / 900)
    text = generate_eval_report(results, config={"k": 2})
    assert "## Reference documents: none, RAG, CAG" in text and "| cag | 1 | 100.0% | 1/1 | 900 |" in text
    assert "Reference documents: none" not in generate_eval_report(results[:1], config={"k": 2})


def test_compare_strategies_names_the_knowledge_set_once_per_strategy(server):
    from harness.eval import load_task_set
    from harness.eval.tasks import expand_strategies
    _, tasks = load_task_set("knowledge")
    ids = {t.id for t in expand_strategies(tasks, ["none", "rag", "cag"])}
    first = next(t.id for t in tasks if t.knowledge)
    assert {f"{first}@none", f"{first}@rag", f"{first}@cag"} <= ids
    with pytest.raises(SystemExit):
        run_cli(["--compare-strategies", "--only", first, "--k", "1", "--endpoint", server])               # the flag expands the ids: a bare id matches none of them
    code, out, err = run_cli(["--compare-strategies", "--only", f"{first}@none", "--k", "1", "--temperature", "0", "--endpoint", server])
    assert f"{first}@none" in out and "warning: k = 1" in err


# -- BFCL -----------------------------------------------------------------------------------------------------------

AREA = {"name": "calculate_triangle_area", "description": "Area of a triangle.", "parameters": {"type": "dict", "properties": {
    "base": {"type": "integer", "description": "base"}, "height": {"type": "integer"}, "unit": {"type": "string"}}, "required": ["base", "height"]}}
SQRT = {"name": "math.sqrt", "description": "Square root.", "parameters": {"type": "dict", "properties": {"x": {"type": "float"}}, "required": ["x"]}}


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


@pytest.fixture
def bfcl(tmp_path):
    root = tmp_path / "bfcl"
    write_jsonl(root / "BFCL_v3_simple.json", [{"id": "simple_0", "question": [[{"role": "user", "content": "Area of a triangle with base 10 and height 5?"}]], "function": [AREA]}])
    write_jsonl(root / "possible_answer" / "BFCL_v3_simple.json", [{"id": "simple_0", "ground_truth": [{"calculate_triangle_area": {"base": [10], "height": [5], "unit": ["units", ""]}}]}])
    write_jsonl(root / "BFCL_v3_parallel.json", [{"id": "parallel_0", "question": [[{"role": "user", "content": "Roots of 4 and 9."}]], "function": [SQRT]}])
    write_jsonl(root / "possible_answer" / "BFCL_v3_parallel.json", [{"id": "parallel_0", "ground_truth": [{"math.sqrt": {"x": [4]}}, {"math.sqrt": {"x": [9]}}]}])
    write_jsonl(root / "BFCL_v3_irrelevance.json", [{"id": "irrelevance_0", "question": [[{"role": "user", "content": "What is the capital of France?"}]], "function": [AREA]}])
    write_jsonl(root / "BFCL_v3_java.json", [{"id": "java_0", "question": [[{"role": "user", "content": "x"}]], "function": [AREA]}])
    return root


def bfcl_run(root, steps, task_id, categories=None):
    loaded = benchmarks.load_bfcl(root, categories)
    task = next(t for t in loaded.tasks if t.id == task_id)
    return runner_for(*steps).evaluate_task(task, 1).trials[0]


def test_a_bfcl_folder_becomes_tasks_with_the_other_syntaxes_left_out(bfcl):
    loaded = benchmarks.load_bfcl(bfcl)
    assert sorted(t.id for t in loaded.tasks) == ["bfcl/irrelevance_0", "bfcl/parallel_0", "bfcl/simple_0"]
    assert any("java" in s for s in loaded.skipped)
    simple = next(t for t in loaded.tasks if t.id == "bfcl/simple_0")
    assert simple.mode == "tools" and simple.tools[0]["parameters"]["type"] == "object" and simple.tools[0]["name"] == "calculate_triangle_area"
    sqrt = next(t for t in loaded.tasks if t.id == "bfcl/parallel_0")
    assert sqrt.tools[0]["name"] == "math_sqrt" and sqrt.tools[0]["parameters"]["properties"]["x"]["type"] == "number"
    assert [t.id for t in benchmarks.load_bfcl(bfcl, ["simple"]).tasks] == ["bfcl/simple_0"]
    assert len(benchmarks.load_bfcl(bfcl, limit=1).tasks) == 1


def test_bfcl_scores_the_call_and_not_the_words(bfcl):
    right = bfcl_run(bfcl, [call("calculate_triangle_area", {"base": 10, "height": 5}), say("25")], "bfcl/simple_0")
    assert right.passed
    assert bfcl_run(bfcl, [call("calculate_triangle_area", {"base": 10, "height": 5, "unit": "units"}), say("25")], "bfcl/simple_0").passed        # an accepted value
    assert bfcl_run(bfcl, [call("calculate_triangle_area", {"base": 10, "height": 5.0}), say("25")], "bfcl/simple_0").repaired_calls == 1           # a float for an integer runs as an integer, and is counted
    wrong_value = bfcl_run(bfcl, [call("calculate_triangle_area", {"base": 11, "height": 5}), say("x")], "bfcl/simple_0")
    assert not wrong_value.passed and "argument base" in wrong_value.failures[0]
    assert not bfcl_run(bfcl, [call("calculate_triangle_area", {"base": 10, "height": 5, "unit": "miles"}), say("x")], "bfcl/simple_0").passed        # not an accepted value
    missing = bfcl_run(bfcl, [call("calculate_triangle_area", {"base": 10}), say("x")], "bfcl/simple_0")
    assert not missing.passed and missing.invalid_calls == 1                                                                                       # refused before it ran
    assert not bfcl_run(bfcl, [say("It is 25.")], "bfcl/simple_0").passed                                                                        # saying it is not calling it


def test_bfcl_parallel_calls_may_come_in_any_order_and_each_answers_one(bfcl):
    two = [call("math_sqrt", {"x": 9}, "c1"), call("math_sqrt", {"x": 4}, "c2"), say("3 and 2")]
    assert bfcl_run(bfcl, two, "bfcl/parallel_0").passed
    twice = [call("math_sqrt", {"x": 4}, "c1"), call("math_sqrt", {"x": 4}, "c2"), say("2 and 2")]
    assert not bfcl_run(bfcl, twice, "bfcl/parallel_0").passed
    assert "1 call(s) were made and 2" in bfcl_run(bfcl, [call("math_sqrt", {"x": 4}), say("2")], "bfcl/parallel_0").failures[0]


def test_bfcl_irrelevance_passes_only_when_no_function_is_called(bfcl):
    assert bfcl_run(bfcl, [say("Paris. None of my functions fit.")], "bfcl/irrelevance_0").passed
    assert "no function fits" in bfcl_run(bfcl, [call("calculate_triangle_area", {"base": 1, "height": 1}), say("x")], "bfcl/irrelevance_0").failures[0]


def test_a_folder_that_is_not_bfcl_is_said_so(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="no BFCL_v"):
        benchmarks.load_bfcl(tmp_path / "empty")
    write_jsonl(tmp_path / "BFCL_v3_simple.json", [{"id": "s", "question": [[{"role": "user", "content": "x"}]], "function": [AREA]}])
    with pytest.raises(ValueError, match="nothing could be loaded"):
        benchmarks.load_bfcl(tmp_path)                                                       # no possible_answer file


# -- Terminal-Bench ---------------------------------------------------------------------------------------------------

TB_TEST = "from pathlib import Path\n\n\ndef test_hello():\n    assert Path('/app/hello.txt').read_text().strip() == 'hi'\n"


@pytest.fixture
def terminal_bench(tmp_path):
    root = tmp_path / "tb"
    task = root / "hello-world"
    (task / "tests").mkdir(parents=True)
    (task / "task.yaml").write_text("instruction: |\n  Create /app/hello.txt that contains the word hi.\ndifficulty: easy\ncategory: file-operations\n", encoding="utf-8")
    (task / "tests" / "test_outputs.py").write_text(TB_TEST, encoding="utf-8")
    (task / "solution.sh").write_text("echo hi > /app/hello.txt\n", encoding="utf-8")
    (task / "data.csv").write_text("a,b\n", encoding="utf-8")
    (root / "not-a-task").mkdir()
    return root


def test_a_terminal_bench_task_keeps_its_tests_and_solution_from_the_model(terminal_bench, tmp_path):
    loaded = benchmarks.load_terminal_bench(terminal_bench)
    task = loaded.tasks[0]
    assert task.id == "tb/hello-world" and task.category == "terminal-bench/file-operations" and "/app" not in task.prompt and "hello.txt" in task.prompt
    workspace = make_workspace(task, tmp_path / "ws")
    assert sorted(p.name for p in workspace.iterdir()) == ["data.csv"]                          # no tests, no solution, no task.yaml


def test_a_terminal_bench_task_is_scored_by_its_own_tests_run_against_the_workspace(terminal_bench):
    task = benchmarks.load_terminal_bench(terminal_bench).tasks[0]
    done = runner_for(call("write_file", {"path": "hello.txt", "content": "hi\n"}), say("Done.")).evaluate_task(task, 1).trials[0]
    assert done.passed, done.failures
    undone = runner_for(say("I created it.")).evaluate_task(task, 1).trials[0]
    assert not undone.passed and "exited" in undone.failures[0]
    fake = runner_for(call("write_file", {"path": "tests/test_outputs.py", "content": "def test_hello():\n    assert True\n"}), say("Done.")).evaluate_task(task, 1).trials[0]
    assert not fake.passed                                                                       # a test the model wrote is replaced by the task's own


def test_the_yaml_fields_are_read_without_pyyaml_too(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    fields = benchmarks.read_yaml_fields("instruction: |\n  Line one.\n  Line two.\ndifficulty: easy\nmax_test_timeout_sec: 90\n")
    assert fields == {"instruction": "Line one.\nLine two.", "difficulty": "easy", "max_test_timeout_sec": "90"}


# -- SWE-bench Pro ----------------------------------------------------------------------------------------------------

TEST_PATCH = ("diff --git a/test_calc.py b/test_calc.py\nnew file mode 100644\n--- /dev/null\n+++ b/test_calc.py\n@@ -0,0 +1,5 @@\n"
              "+from calc import add\n+\n+\n+def test_add():\n+    assert add(1, 2) == 3\n")


@pytest.fixture
def swebench(tmp_path):
    root = tmp_path / "swe"
    (root / "repos" / "demo__calc-1").mkdir(parents=True)
    (root / "repos" / "demo__calc-1" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "repos" / "demo__calc-1" / ".git").mkdir()
    rows = [{"instance_id": "demo__calc-1", "repo": "demo/calc", "problem_statement": "add subtracts instead of adding.", "test_patch": TEST_PATCH,
             "FAIL_TO_PASS": json.dumps(["test_calc.py::test_add"]), "PASS_TO_PASS": []},
            {"instance_id": "demo__missing-2", "problem_statement": "x", "test_patch": TEST_PATCH, "FAIL_TO_PASS": ["a"], "PASS_TO_PASS": []}]
    write_jsonl(root / "instances.jsonl", rows)
    return root


@pytest.mark.skipif(shutil.which("git") is None, reason="git applies the instance's test patch")
def test_a_swe_instance_is_resolved_when_its_tests_start_passing(swebench):
    loaded = benchmarks.load_swebench(swebench)
    assert [t.id for t in loaded.tasks] == ["swe/demo__calc-1"] and any("demo__missing-2" in s for s in loaded.skipped)
    task = loaded.tasks[0]
    fixed = runner_for(call("write_file", {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}), say("Fixed.")).evaluate_task(task, 1).trials[0]
    assert fixed.passed, fixed.failures
    untouched = runner_for(say("Looks fine to me.")).evaluate_task(task, 1).trials[0]
    assert not untouched.passed and "FAIL_TO_PASS tests failed" in untouched.failures[0]


def test_the_checkout_given_to_the_model_has_no_git_history(swebench, tmp_path):
    task = benchmarks.load_swebench(swebench).tasks[0]
    assert sorted(p.name for p in make_workspace(task, tmp_path / "w").iterdir()) == ["calc.py"]


def test_the_benchmark_flags_choose_one_loader_and_an_unreadable_folder_is_an_error_not_a_crash(tmp_path):
    with pytest.raises(SystemExit):
        eval_main(["--bfcl", str(tmp_path), "--swebench", str(tmp_path)])
    with pytest.raises(SystemExit):
        eval_main(["--bfcl", str(tmp_path / "nowhere")])
