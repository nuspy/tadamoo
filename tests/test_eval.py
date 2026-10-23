import pytest
from pathlib import Path
from harness.eval.runner import EvalRunner
from harness.eval.report import generate_eval_report
from harness.client import extract_reasoning_and_content


def test_extract_reasoning_inline_think():
    msg = {"content": "<think>First check the files</think>Here is the file content."}
    reasoning, content = extract_reasoning_and_content(msg)
    assert reasoning == "First check the files"
    assert content == "Here is the file content."


def test_extract_reasoning_explicit_field():
    msg = {"reasoning_content": "Detailed reasoning here", "content": "Direct response"}
    reasoning, content = extract_reasoning_and_content(msg)
    assert reasoning == "Detailed reasoning here"
    assert content == "Direct response"


def test_eval_runner_pass_pow_k(tmp_path: Path):
    runner = EvalRunner()
    task = {
        "id": "t1",
        "category": "test",
        "prompt": "Say hello",
        "expected_keyword": "hello"
    }

    # All pass -> pass_pow_k is True
    res_all_pass = runner.evaluate_task(
        task,
        k=3,
        mock_output_fn=lambda t, i: ("hello world", "reasoning", 50),
    )
    assert res_all_pass.pass_at_k == 1.0
    assert res_all_pass.pass_pow_k is True

    # 1 fails -> pass_pow_k is False, pass_at_k is 1.0
    res_partial = runner.evaluate_task(
        task,
        k=3,
        mock_output_fn=lambda t, i: ("hello world" if i < 2 else "wrong answer", "r", 50),
    )
    assert res_partial.pass_at_k == 1.0
    assert res_partial.pass_pow_k is False

    report = generate_eval_report([res_all_pass, res_partial])
    assert "**pass^k Reliability**: 1/2 (50.0%)" in report
    assert "Qwen3.8-Flash-Next" in report
