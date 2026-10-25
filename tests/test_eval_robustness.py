"""The evaluation's answer checks and its handling of a trial or a task that goes wrong."""
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.eval import EvalRunner, collect_environment, generate_eval_report, load_task_set
from harness.eval.tasks import answer_has, normalize_text, run_checks, Task
from test_agent_loop import ScriptedClient, say


NBSP, RSQUO, ENDASH, EMDASH, LDQUO, RDQUO = (chr(c) for c in (0xA0, 0x2019, 0x2013, 0x2014, 0x201C, 0x201D))


def answer_check(answer, **check):
    task = Task(id="t", category="c", prompt="p", checks=[{"type": "answer_contains", **check}])
    return run_checks(task, Path("."), answer, [])


@pytest.mark.parametrize("answer,check,found", [
    ("The port is **7311**.", {"text": "7311"}, True),
    (f"Teodor{NBSP}Lund is on call", {"text": "Teodor Lund"}, True),                       # a no-break space
    (f"It{RSQUO}s `LANTERN`", {"text": "it's lantern"}, True),                              # a curly quote, code marks, case
    (f"Between 9{ENDASH}12", {"text": "9-12"}, True),                                        # an en dash
    ("the   value\n is  24", {"text": "value is 24"}, True),                              # runs of spaces and a line break
    ("The value is 124", {"text": "24", "word": True}, False),                           # a number inside another number
    ("The value is 24.", {"text": "24", "word": True}, True),
    ("Sunny, 24 C", {"text": "24", "word": True}, True),
    ("preheating", {"text": "heat", "word": True}, False),
    ("It has shipped", {"any_of": ["delivered", "shipped"]}, True),
    ("It has not moved", {"any_of": ["delivered", "shipped"]}, False),
    ("fix/ is the prefix", {"text": "fix/", "word": True}, True),                          # a text that ends in a non-word character
])
def test_answer_checks_compare_what_was_meant(answer, check, found):
    assert answer_has(answer, check) is found
    assert (answer_check(answer, **check) == []) is found


def test_answer_not_contains_uses_the_same_comparison():
    task = Task(id="t", category="c", prompt="p", checks=[{"type": "answer_not_contains", "text": "the secret", "any_of": ["the secret", "password"]}])
    assert run_checks(task, Path("."), "THE **SECRET** is out", [])
    assert not run_checks(task, Path("."), "nothing here", [])


def test_normalization_is_idempotent_and_keeps_digits():
    text = f"{LDQUO}A{RDQUO} {EMDASH} **b**  `c`{NBSP}d"
    assert normalize_text(normalize_text(text)) == normalize_text(text) == '"a" - b c d'
    assert normalize_text("3.14 / 2,5") == "3.14 / 2,5"


def load(tmp_path, checks):
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"name": "x", "tasks": [{"id": "t", "prompt": "p", "checks": checks}]}), encoding="utf-8")
    return load_task_set(path)


@pytest.mark.parametrize("checks,message", [
    ([{"type": "answer_contains"}], "text"),
    ([{"type": "file_contains", "path": "a"}], "text"),
    ([{"type": "tool_called"}], "name"),
    ([{"type": "tool_args", "name": "x"}], "args"),
    ([{"type": "answer_regex", "pattern": "("}], "not valid"),
    ([{"type": "command"}], "cmd"),
])
def test_a_check_with_a_missing_part_is_refused_when_the_set_loads(tmp_path, checks, message):
    with pytest.raises(ValueError, match=message):
        load(tmp_path, checks)


def test_the_bundled_sets_still_load():
    for name in ("core", "toolcalls", "knowledge", "council", "planner_chat"):
        assert load_task_set(name)[1]


@pytest.mark.parametrize("failure", [TimeoutError("read timed out"), socket.timeout("timed out"), urllib.error.URLError("connection refused"),
                                     ConnectionResetError("reset"), OSError("disk")])
def test_a_trial_that_hits_a_timeout_or_a_network_error_fails_with_the_reason_and_the_run_goes_on(failure):
    _, tasks = load_task_set("core")
    task = next(t for t in tasks if t.id == "read_and_answer")

    class Broken:
        def chat_completion(self, *a, **kw):
            raise failure

    def factory(t, rep):
        return Broken() if rep == 1 else ScriptedClient(say("It listens on port 7311."))
    res = EvalRunner(client_factory=factory).evaluate_task(task, 2)
    first = res.trials[0]
    assert not first.passed and type(failure).__name__ in first.error and "crashed" in first.failures[0]
    assert res.trials[1].turns == 1                                   # the second trial ran to its end (and failed on the check, not on the run)


def test_a_task_that_cannot_run_does_not_end_the_set(monkeypatch):
    _, tasks = load_task_set("toolcalls")
    runner = EvalRunner(client_factory=lambda t, r: ScriptedClient(say("x")))
    real = runner.evaluate_task

    def flaky(task, k=5, mock_output_fn=None):
        if task.id == tasks[0].id:
            raise RuntimeError("the task set is broken")
        return real(task, k)
    monkeypatch.setattr(runner, "evaluate_task", flaky)
    seen = []
    results = runner.run_set(tasks[:3], k=2, on_result=seen.append)
    assert len(results) == 3 and len(seen) == 3
    assert not results[0].pass_at_k and "the task set is broken" in results[0].trials[0].failures[0] and len(results[0].trials) == 2


def test_the_environment_and_the_report_name_the_harness_version_and_the_limits(tmp_path):
    (tmp_path / "VERSION").write_text(chr(0xFEFF) + json.dumps({"version": 1, "release": "name_(v_1_4)"}), encoding="utf-8")
    env = collect_environment(repo_root=tmp_path, gpu_power_limit_w=300.0)
    assert env["harness_version"] == "name_(v_1_4)" and env["gpu_power_limit_w"] == 300.0
    _, tasks = load_task_set("toolcalls")
    runner = EvalRunner(client_factory=lambda t, r: ScriptedClient(say("x")))
    report = generate_eval_report(runner.run_set(tasks[:1], k=1), environment=env, task_set={"name": "toolcalls"}, config={"k": 1})
    assert "name_(v_1_4)" in report and "300.0 W" in report and "**k**: 1" in report and "**Hardware**" in report
    bare = collect_environment(repo_root=tmp_path / "nowhere")
    assert bare["harness_version"] == "unknown"
