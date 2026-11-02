"""Lessons from shell failures: only a change that made a command of the same intent work counts; plain red test runs and failed looks around do not."""
import json

from tadamoo import shell_lessons
from tadamoo.lessons import Recorder, apply_polish


def feed(rec, *steps):
    """steps: ("shell", command, exit_code, printed) | ("edit", path) | ("code", language, exit_code, printed)."""
    for i, step in enumerate(steps, 1):
        kind = step[0]
        if kind == "edit":
            name, args, output, status = "edit_file", {"path": step[1]}, "edited", "completed"
        elif kind == "shell":
            name, args = "shell_command", {"command": step[1]}
            output = json.dumps({"exit_code": step[2], "stdout": "" if step[2] else step[3], "stderr": step[3] if step[2] else "", "success": step[2] == 0})
            status = "completed"
        else:
            name, args = "run_code", {"language": step[1], "code": f"print({i})"}
            output = json.dumps({"exit_code": step[2], "stdout": "", "stderr": step[3], "success": step[2] == 0})
            status = "completed"
        rec.observe({"type": "tool_start", "call_id": f"c{i}", "name": name, "arguments": args, "effect": "irreversible"})
        rec.observe({"type": "tool_end", "call_id": f"c{i}", "name": name, "status": status, "decision": "executed", "output": output})
    return rec


def found(*steps, mask=None):
    return Recorder(mask=mask).finish("done", "do it") if not steps else feed(Recorder(mask=mask), *steps).finish("done", "do it")[1]


def test_a_script_that_failed_and_worked_after_an_edit_is_a_lesson():
    (c,) = found(("shell", "python app.py", 1, "NameError: name 'x' is not defined"), ("edit", "src/app.py"), ("shell", "python app.py", 0, "ok"))
    assert c["kind"] == "error_fix" and c["facts"]["tools"] == ["python"] and c["evidence"] == ["c1", "c3"]
    assert "NameError" in c["went_wrong"] and "edit_file src/app.py" in c["fix"] and "exit code 1" in c["went_wrong"]


def test_another_program_or_other_flags_are_a_real_change():
    (a,) = found(("shell", "python tool.py --go", 9009, "'python' is not recognized as an internal or external command"), ("shell", "py tool.py --go", 0, "done"))
    assert "py instead of python" in a["fix"] and a["facts"]["tools"] == ["python", "py"]
    (b,) = found(("shell", "pip install requests", 1, "ERROR: Could not install packages: permission denied"), ("shell", "pip install --user requests", 0, "installed"))
    assert "flags added: --user" in b["fix"]


def test_a_red_test_run_is_never_a_lesson_even_after_an_edit():
    assert found(("shell", "pytest tests/test_a.py", 1, "1 failed"), ("edit", "src/a.py"), ("shell", "pytest tests/test_a.py", 0, "1 passed")) == []
    assert found(("shell", "cd app && npm test", 1, "failing"), ("edit", "app/a.js"), ("shell", "npm test", 0, "ok")) == []
    assert found(("shell", "python -m pytest -x tests", 1, "FAILED"), ("edit", "tests/x.py"), ("shell", "python -m pytest tests", 0, "ok")) == []
    assert found(("shell", "cargo test", 101, "test failed"), ("edit", "src/lib.rs"), ("shell", "cargo test", 0, "ok")) == []


def test_a_test_command_that_could_not_run_is_not_a_red_run():
    (c,) = found(("shell", "pytest tests/test_a.py", 127, "pytest: command not found"), ("shell", "python -m pytest tests/test_a.py", 0, "1 passed"))
    assert "python instead of pytest" in c["fix"]
    (d,) = found(("shell", "pytest tests/test_b.py", 1, "ModuleNotFoundError: No module named 'numpy'"), ("shell", "python -m pytest tests/test_b.py", 0, "passed"))
    assert d["kind"] == "error_fix"


def test_a_failed_look_around_is_not_a_lesson():
    assert found(("shell", "grep -rn foo src", 1, ""), ("shell", "grep -rn foo src/", 0, "src/a.py:1:foo")) == []
    assert found(("shell", "ls missing", 2, "No such file"), ("edit", "missing/x"), ("shell", "ls missing", 0, "x")) == []
    assert found(("shell", "git status", 128, "not a git repository"), ("shell", "git status -s", 0, "")) == []


def test_the_same_command_run_again_with_nothing_changed_is_a_flaky_call():
    assert found(("shell", "python app.py", 1, "timeout talking to the host"), ("shell", "python app.py", 0, "ok")) == []


def test_a_command_about_something_else_does_not_answer_a_failure():
    assert found(("shell", "python a.py", 1, "boom"), ("shell", "python b.py", 0, "ok")) == []
    assert found(("shell", "pip install foo", 1, "no match"), ("shell", "pip install bar", 0, "ok")) == []


def test_a_success_far_after_the_failure_is_ignored():
    steps = [("shell", "python app.py", 1, "boom")] + [("shell", f"echo {i}", 0, "x") for i in range(shell_lessons.WINDOW + 1)] + [("edit", "app.py"), ("shell", "python app.py", 0, "ok")]
    assert found(*steps) == []


def test_a_code_call_that_worked_after_an_edit_or_in_another_language_counts():
    (c,) = found(("code", "python", 1, "SyntaxError: invalid syntax"), ("edit", "helper.py"), ("code", "python", 0, ""))
    assert c["kind"] == "error_fix" and "edit_file helper.py" in c["fix"]
    assert found(("code", "python", 1, "SyntaxError"), ("code", "python", 0, "")) == []


def test_the_same_failure_in_another_place_is_one_candidate_and_keeps_its_signature():
    first = found(("shell", "python app.py", 1, "NameError: name 'x' is not defined"), ("edit", "app.py"), ("shell", "python app.py", 0, "ok"))[0]
    second = found(("shell", "python other.py", 1, "NameError: name 'y' is not defined"), ("edit", "other.py"), ("shell", "python other.py", 0, "ok"))[0]
    assert first["signature"] == second["signature"]


def test_secrets_in_a_command_are_masked_and_the_draft_can_be_polished():
    (c,) = found(("shell", "python app.py --token=S3CRET", 1, "bad token S3CRET"), ("edit", "app.py"), ("shell", "python app.py --token=S3CRET", 0, "ok"), mask=lambda t: t.replace("S3CRET", "***"))
    assert "S3CRET" not in json.dumps(c)
    good = "WENT WRONG: python app.py failed because of a rejected token.\nFIX: After editing app.py the python command worked."
    assert apply_polish(c, good).get("polished") is True


def test_the_exit_code_is_read_from_a_whole_or_cut_result():
    rec = Recorder()
    rec.observe({"type": "tool_start", "call_id": "c1", "name": "shell_command", "arguments": {"command": "python a.py"}})
    rec.observe({"type": "tool_end", "call_id": "c1", "name": "shell_command", "status": "completed", "decision": "executed",
                 "output": '{"exit_code": 2, "stdout": "partial output that was cut off by the tool and is not valid js'})
    assert rec.calls[0].code == 2
    assert shell_lessons.parse("cd x && python -m pytest").test and shell_lessons.parse("ls -la").look
