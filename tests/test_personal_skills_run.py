"""Personal skills: a script runs in a process of its own and what it asks for is answered by the harness."""
import threading
import time

from harness.personal_skills.runner import Handlers, run
from harness.personal_skills.values import resolve


def handlers(**over):
    calls = {"tools": [], "ai": [], "ask": [], "say": []}

    def tool(name, arguments):
        calls["tools"].append((name, arguments))
        return (False, "Permission denied") if name == "refused" else (True, f"<{name}:{arguments.get('path', '')}>")

    def ai(prompt, inputs, cache):
        calls["ai"].append((prompt, inputs, cache))
        return "AI(" + prompt.format(**inputs) + ")"

    def ask(question, options):
        calls["ask"].append((question, options))
        return options[0] if options else "typed"

    base = Handlers(tool, ai, ask, lambda text: calls["say"].append(text))
    for k, v in over.items():
        setattr(base, k, v)
    return base, calls


def script(tmp_path, source, name="skill.py"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def values(specs, given):
    got, errors = resolve(specs, given)
    assert not errors, errors
    return got


def test_a_script_calls_tools_the_model_and_the_person_and_returns_a_result(tmp_path):
    path = script(tmp_path, '''
from basalt_skill import tool, ai, ask, say

def main(params):
    say("starting")
    text = tool("read_file", path=params.file)
    choice = ask("Which one?", ["a", "b"])
    print("a line of output")
    return ai("Summarise {t} for {who}", t=text, who=params.who) + " / " + choice
''')
    h, calls = handlers()
    res = run(path, "restricted", values([{"name": "file", "type": "path"}, {"name": "who", "type": "text"}], {"file": "notes.txt", "who": "Anna"}), h)
    assert res.ok, res.error
    assert res.output == "AI(Summarise <read_file:notes.txt> for Anna) / a"
    assert res.printed == "a line of output" and calls["say"] == ["starting"]
    assert calls["tools"] == [("read_file", {"path": "notes.txt"})] and calls["ai"][0][2] is True
    assert res.calls == [{"name": "read_file", "ok": True}]


def test_typed_values_arrive_as_python_values(tmp_path):
    path = script(tmp_path, '''
import datetime

def main(params):
    d = params.day
    return [type(d).__name__, d.isoformat(), (d + datetime.timedelta(days=1)).isoformat(), params.n * 2, params.items, params.on, params.when.hour]
''')
    specs = [{"name": "day", "type": "date"}, {"name": "n", "type": "number"}, {"name": "items", "type": "list", "item_type": "mixed"},
             {"name": "on", "type": "bool"}, {"name": "when", "type": "datetime"}]
    res = run(path, "restricted", values(specs, {"day": "2026-10-26", "n": "4", "items": "1; due", "on": "si", "when": "2026-10-26 15:30"}), handlers()[0])
    assert res.ok, res.error
    assert res.output == '[\n  "date",\n  "2026-10-26",\n  "2026-10-27",\n  8,\n  [\n    1,\n    "due"\n  ],\n  true,\n  15\n]'


def test_a_refused_tool_raises_in_the_script_which_may_go_on(tmp_path):
    path = script(tmp_path, '''
from basalt_skill import tool, ToolError

def main(params):
    try:
        tool("refused")
    except ToolError as e:
        return "went on after: " + str(e)
''')
    res = run(path, "restricted", {}, handlers()[0])
    assert res.ok and res.output == "went on after: Permission denied" and res.calls == [{"name": "refused", "ok": False}]


def test_an_error_in_the_script_ends_the_run_and_says_where(tmp_path):
    path = script(tmp_path, "def main(params):\n    x = 1\n    return 1 / 0\n")
    res = run(path, "restricted", {}, handlers()[0])
    assert not res.ok and "ZeroDivisionError" in res.error and "line 3" in res.error


def test_in_the_restricted_mode_files_and_programs_are_not_there_even_if_the_check_was_skipped(tmp_path):
    for body in ("import os\n    return os.getcwd()", "return open(__file__).read()", "import subprocess\n    return 1", "return __import__('os').getcwd()",
                 "return eval('1+1')", "import urllib.request\n    return 1"):
        path = script(tmp_path, f"def main(params):\n    {body}\n")
        res = run(path, "restricted", {}, handlers()[0])
        assert not res.ok, body


def test_in_the_free_mode_the_script_may_use_python_as_it_is(tmp_path):
    path = script(tmp_path, "import os\n\ndef main(params):\n    return os.path.basename(os.getcwd())\n")
    res = run(path, "free", {}, handlers()[0])
    assert res.ok and res.output == tmp_path.name


def test_stop_ends_the_script_and_what_it_started(tmp_path):
    path = script(tmp_path, "import time\n\ndef main(params):\n    time.sleep(60)\n")
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    began = time.monotonic()
    res = run(path, "free", {}, handlers()[0], cancel=cancel)
    assert not res.ok and res.error == "stopped" and time.monotonic() - began < 15


def test_the_time_limit_ends_a_script_that_does_not_finish(tmp_path):
    path = script(tmp_path, "import time\n\ndef main(params):\n    time.sleep(60)\n")
    res = run(path, "free", {}, handlers()[0], timeout=1.5)
    assert not res.ok and "more than 1.5 seconds" in res.error


def test_a_handler_that_fails_is_told_to_the_script(tmp_path):
    def broken(prompt, inputs, cache):
        raise RuntimeError("the model is not loaded")

    path = script(tmp_path, '''
from basalt_skill import ai, SkillError

def main(params):
    try:
        ai("hello")
    except SkillError as e:
        return "no model: " + str(e)
''')
    res = run(path, "restricted", {}, handlers(ai=broken)[0])
    assert res.ok and res.output == "no model: RuntimeError: the model is not loaded"


def test_unicode_goes_through_the_pipes(tmp_path):
    path = script(tmp_path, 'from basalt_skill import ai\n\ndef main(params):\n    print("città — ✓")\n    return ai("{t}", t="perché")\n')
    res = run(path, "restricted", {}, handlers()[0])
    assert res.ok and res.output == "AI(perché)" and res.printed == "città — ✓"
