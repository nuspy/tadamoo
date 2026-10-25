"""run_code: output, errors, timeout and cancel that kill the tree, a clean environment, caps, and the tool's place in the permission layer."""
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from harness.permissions import PermissionLevel, PermissionManager
from harness.tools import create_standard_tool_registry
from harness.tools.execute import RUN_DIR, build_command, find_runtimes, register_run_code, run_code
from harness.tools.registry import EffectClass

RUNTIMES = find_runtimes()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def test_python_prints_and_returns_success(ws):
    res = run_code("python", "print('hello', 6 * 7)", ws, RUNTIMES)
    assert res["success"] and res["exit_code"] == 0
    assert res["stdout"].strip() == "hello 42"
    assert not any((ws / RUN_DIR).iterdir())                  # nothing left behind when the code made no files


def test_an_error_gives_the_traceback_and_the_exit_code(ws):
    res = run_code("python", "raise ValueError('boom')", ws, RUNTIMES)
    assert not res["success"] and res["exit_code"] == 1
    assert "ValueError: boom" in res["stderr"]


def test_it_runs_in_isolated_mode_with_the_workspace_in_the_environment(ws, monkeypatch):
    monkeypatch.setenv("PYTHONSTARTUP", "x")
    code = "import os, sys\nprint(sys.flags.isolated)\nprint(os.environ['BASALT_WORKSPACE'])\nprint(os.getcwd())"
    res = run_code("python", code, ws, RUNTIMES)
    isolated, workspace, cwd = res["stdout"].split()
    assert isolated == "1"
    assert Path(workspace) == ws.resolve()
    assert Path(cwd).parent == (ws / RUN_DIR).resolve()         # a new folder inside the workspace's own scratch area


def test_secrets_of_the_pc_are_not_in_the_environment(ws, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk_should_not_leak")
    res = run_code("python", "import os; print(sorted(os.environ))", ws, RUNTIMES)
    assert res["success"], res
    assert "GITHUB_TOKEN" not in res["stdout"] and "OPENAI_API_KEY" not in res["stdout"]


def test_files_the_code_makes_are_kept_and_listed(ws):
    res = run_code("python", "open('out.csv', 'wb').write(b'a,b\\n1,2\\n')", ws, RUNTIMES)
    assert res["success"]
    assert res["files"] == [{"path": "out.csv", "bytes": 8}]
    assert (ws / res["folder"] / "out.csv").read_bytes() == b"a,b\n1,2\n"
    assert not (ws / res["folder"] / "main.py").exists()


def test_output_is_capped(ws):
    res = run_code("python", "print('x' * 100000)", ws, RUNTIMES, max_output_bytes=1000)
    assert res["truncated"] and len(res["stdout"]) == 1000


def test_timeout_kills_the_whole_process_tree(ws):
    marker = ws / "alive.txt"
    code = ("import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(4); open(r'{marker}', 'w').write('x')\"])\n"
            "time.sleep(60)\n")
    start = time.monotonic()
    res = run_code("python", code, ws, RUNTIMES, timeout=1)
    assert res["timed_out"] and not res["success"] and res["exit_code"] == -1
    assert "stopped after 1 seconds" in res["stderr"]
    assert time.monotonic() - start < 15
    time.sleep(5)
    assert not marker.exists(), "the child outlived the timeout"


def test_cancel_stops_the_code(ws):
    cancel = threading.Event()
    threading.Timer(0.8, cancel.set).start()
    start = time.monotonic()
    res = run_code("python", "import time; time.sleep(60)", ws, RUNTIMES, timeout=60, cancel=cancel)
    assert res["cancelled"] and not res["success"]
    assert time.monotonic() - start < 15


@pytest.mark.skipif(sys.platform != "win32", reason="the memory and process caps are a Windows job object")
def test_the_process_cap_is_enforced(ws):
    code = ("import subprocess, sys\n"
            "kids = []\n"
            "try:\n"
            "    for _ in range(8):\n"
            "        kids.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']))\n"
            "    print('spawned', len(kids))\n"
            "except OSError as e:\n"
            "    print('refused', len(kids))\n"
            "for k in kids: k.kill()\n")
    res = run_code("python", code, ws, RUNTIMES, max_processes=3)
    assert "refused" in res["stdout"], res


def test_no_runtime_and_empty_code_are_reported_not_raised(ws):
    assert "No runtime" in run_code("typescript", "1", ws, {"python": RUNTIMES["python"]})["stderr"]
    assert "Give the code" in run_code("python", "  ", ws, RUNTIMES)["stderr"]


def test_commands_for_each_runtime(tmp_path):
    work, space = tmp_path / "w", tmp_path / "s"
    py = build_command({"kind": "python", "exe": "py"}, work / "main.py", work, space)
    assert py[:3] == ["py", "-I", "-X"]
    deno = build_command({"kind": "deno", "exe": "deno"}, work / "main.ts", work, space)
    assert f"--allow-write={work}" in deno and not any(a.startswith("--allow-net") or a == "--allow-all" for a in deno)
    assert f"--allow-read={work},{space}" in deno
    assert build_command({"kind": "node", "exe": "node"}, work / "main.js", work, space)[0] == "node"


@pytest.mark.skipif(not shutil.which("node") and not shutil.which("deno"), reason="neither deno nor node is installed")
def test_javascript_runs_when_a_runtime_is_installed(ws):
    res = run_code("javascript", "console.log([1, 2, 3].map(x => x * 2).join(','))", ws, RUNTIMES)
    assert res["success"], res
    assert res["stdout"].strip() == "2,4,6"


def test_the_tool_is_irreversible_and_the_permission_layer_decides(ws):
    registry, sandbox = create_standard_tool_registry(ws)
    register_run_code(registry, ws, sandbox)
    tool = registry.get("run_code")
    assert tool.effect == EffectClass.IRREVERSIBLE
    args = {"language": "python", "code": "print(1)"}
    asked = []
    default = PermissionManager(PermissionLevel.DEFAULT, confirm_callback=lambda name, a: asked.append(name) or False)
    assert default.evaluate("run_code", tool.effect, args)[0] is False and asked == ["run_code"]
    yes = PermissionManager(PermissionLevel.DEFAULT, confirm_callback=lambda name, a: True)
    assert yes.evaluate("run_code", tool.effect, args)[0] is True
    plan = PermissionManager(PermissionLevel.AUTO, plan_mode=True)
    assert plan.evaluate("run_code", tool.effect, args)[0] is False
    deny = PermissionManager(PermissionLevel.AUTO)
    deny.add_rule("run_code", "deny")
    assert deny.evaluate("run_code", tool.effect, args)[0] is False


def test_through_the_registry_and_with_the_edits_it_makes_recorded(ws):
    registry, sandbox = create_standard_tool_registry(ws)
    register_run_code(registry, ws, sandbox)
    out = registry.execute("run_code", {"language": "python",
                                        "code": "import os\nopen(os.path.join(os.environ['BASALT_WORKSPACE'], 'made.txt'), 'w').write('hi')"})
    assert out["success"]
    assert [c["file"] for c in sandbox.changes] == ["made.txt"]
