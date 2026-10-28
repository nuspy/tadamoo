"""The person's secrets (vault, device key, control token, phone keys, payment files) cannot be opened by what the agent runs:
adversarial snippets build the path at run time, start helpers, link to the files, and use the file tools."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.secrets_broker import PROTECTED_FILES, SecretsBroker
from harness.tools import create_standard_tool_registry
from harness.tools.execute import find_runtimes, run_code

CANARY = "CANARY-vault-content-do-not-leak"


@pytest.fixture
def world(tmp_path):
    data = tmp_path / "userdata"
    (data / "vault").mkdir(parents=True)
    (data / "vault2").mkdir()
    (data / "journal").mkdir()
    (data / "vault" / "vault.bin").write_text(CANARY)
    (data / "vault" / "device.key").write_text(CANARY)
    (data / "vault2" / "notes.txt").write_text("not a secret")
    (data / "control-7232.token").write_text(CANARY)
    (data / "api-keys.json").write_text(CANARY)
    (data / "journal" / "agent_sessions.json").write_text(CANARY)
    broker = SecretsBroker()
    broker.protect(data / "vault", data / "payments", data / "browser-profile")
    broker.protect_in(data, *PROTECTED_FILES)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.txt").write_text("fine")
    return {"data": data, "ws": ws, "broker": broker}


def py(world, code, **kw):
    return run_code("python", code, world["ws"], find_runtimes(), broker=world["broker"], **kw)


def runtime_path(world, *parts):
    """The snippet's own spelling of a path, assembled at run time so that no name is in its text."""
    split = [f"{p[:len(p) // 2]!r} + {p[len(p) // 2:]!r}" for p in parts]
    here = str(world["data"])
    return f"os.path.join({here[:len(here) // 2]!r} + {here[len(here) // 2:]!r}, " + ", ".join(split) + ")"


def test_a_snippet_that_names_a_secret_is_refused_by_its_text(world):
    res = py(world, "print(open('vault/vault.bin').read())")
    assert res["blocked"] and CANARY not in res["stdout"]


@pytest.mark.parametrize("parts", [("vault", "vault.bin"), ("vault", "device.key"), ("control-7232.token",),
                                   ("api-keys.json",), ("journal", "agent_sessions.json")])
def test_a_snippet_that_builds_the_path_at_run_time_is_stopped_by_the_guard(world, parts):
    res = py(world, f"import os\nprint(open({runtime_path(world, *parts)}).read())")
    assert CANARY not in res["stdout"] and CANARY not in res["stderr"], res
    assert "PermissionError" in res["stderr"], res


def test_other_ways_to_reach_the_file_are_stopped(world):
    p = runtime_path(world, "vault", "vault.bin")
    code = f"""
import os, shutil, sqlite3, pathlib
p = {p}
tries = {{
 "pathlib": lambda: pathlib.Path(p).read_bytes(),
 "os.open": lambda: os.read(os.open(p, os.O_RDONLY), 10),
 "copy": lambda: shutil.copyfile(p, "stolen.txt"),
 "link": lambda: os.link(p, "hard.txt"),
 "rename": lambda: os.rename(p, p + ".x"),
 "remove": lambda: os.remove(p),
 "sqlite": lambda: sqlite3.connect(p),
 "inside": lambda: open(os.path.join(os.path.dirname(p), "newfile"), "w"),
}}
for name, go in tries.items():
    try:
        go(); print(name, "OPENED")
    except PermissionError:
        print(name, "refused")
    except Exception as e:
        print(name, "other", type(e).__name__)
"""
    res = py(world, code)
    assert "OPENED" not in res["stdout"] and "other" not in res["stdout"], res
    assert res["stdout"].count("refused") == 8, res
    assert (world["data"] / "vault" / "vault.bin").read_text() == CANARY


def test_a_python_helper_the_snippet_starts_is_guarded_too(world):
    helper = "import sys, os; print(open(os.path.join(*sys.argv[1:])).read())"       # the helper assembles the path itself
    code = (f"import subprocess, sys\nr = subprocess.run([sys.executable, '-c', {helper!r}, {str(world['data'])!r}, 'vau' + 'lt', 'vau' + 'lt.bin'], "
            "capture_output=True, text=True)\nprint(r.stdout, r.stderr)")
    res = py(world, code)
    assert CANARY not in res["stdout"], res
    assert "PermissionError" in res["stdout"], res


@pytest.mark.skipif(os.name != "nt", reason="type is a cmd builtin")
def test_a_command_the_snippet_builds_at_run_time_is_refused_when_it_names_the_file(world):
    p = runtime_path(world, "vault", "vault.bin")
    code = f"import os, subprocess\nprint(subprocess.run('type \"' + {p} + '\"', shell=True, capture_output=True, text=True).stdout)"
    res = py(world, code)
    assert CANARY not in res["stdout"] and "PermissionError" in res["stderr"], res


def test_a_link_in_the_workspace_does_not_hide_the_file(world):
    link = world["ws"] / "innocent"
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(world["data"] / "vault")], check=True, capture_output=True)
        else:
            os.symlink(world["data"] / "vault", link)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("links cannot be made here")
    code = "import os\nprint(open(os.path.join(os.environ['BASALT_WORKSPACE'], 'innocent', 'vau' + 'lt.bin')).read())"
    res = py(world, code)
    assert CANARY not in res["stdout"] and "PermissionError" in res["stderr"], res
    registry, _ = create_standard_tool_registry(world["ws"], secrets=world["broker"])
    with pytest.raises(PermissionError):
        registry.execute("read_file", {"path": "innocent/vault.bin"})


def test_what_is_not_a_secret_still_works(world):
    code = ("import os, subprocess\nprint(open(os.path.join(os.environ['BASALT_WORKSPACE'], 'ok.txt')).read())\n"
            f"print(open({runtime_path(world, 'vault2', 'notes.txt')}).read())\n"
            "print(subprocess.run(['cmd', '/c', 'echo', 'hi'] if os.name == 'nt' else ['echo', 'hi'], capture_output=True, text=True).stdout)")
    res = py(world, code)
    assert res["success"], res
    assert "fine" in res["stdout"] and "not a secret" in res["stdout"] and "hi" in res["stdout"]
    err = py(world, "raise ValueError('boom')")                      # the traceback starts at the snippet, not at the launcher
    assert err["exit_code"] == 1 and "ValueError: boom" in err["stderr"] and "launch.py" not in err["stderr"]


def test_the_file_tools_refuse_the_secrets_even_when_the_workspace_contains_them(world):
    registry, _ = create_standard_tool_registry(world["data"], secrets=world["broker"])
    for path in ("vault/vault.bin", "vault/device.key", "control-7232.token", "api-keys.json", "journal/agent_sessions.json"):
        with pytest.raises(PermissionError):
            registry.execute("read_file", {"path": path})
    with pytest.raises(PermissionError):
        registry.execute("write_file", {"path": "vault/vault.bin", "content": "x"})
    with pytest.raises(PermissionError):
        registry.execute("list_directory", {"path": "vault"})
    assert registry.execute("read_file", {"path": "vault2/notes.txt"}) == "not a secret"
    assert (world["data"] / "vault" / "vault.bin").read_text() == CANARY


def test_a_shell_python_command_that_builds_the_path_is_guarded(world):
    registry, _ = create_standard_tool_registry(world["ws"], secrets=world["broker"])
    code = f"import os; print(open({runtime_path(world, 'vault', 'vault.bin')}).read())"
    out = registry.execute("shell_command", {"command": 'python -c "' + code.replace('"', "'") + '"'})
    assert CANARY not in out["stdout"], out
    assert "PermissionError" in out["stderr"], out


def test_the_control_token_of_the_server_is_out_of_reach_of_the_agents_tools(world):
    """The secret that approves plans and changes things is a file of the data folder: no tool of the agent opens it."""
    ws = world["data"]
    code = f"import os\nprint(open({runtime_path(world, 'control-7232.token')}).read())"
    res = run_code("python", code, ws, find_runtimes(), broker=world["broker"])
    assert CANARY not in res["stdout"] and "PermissionError" in res["stderr"], res
    registry, _ = create_standard_tool_registry(ws, secrets=world["broker"])
    with pytest.raises(PermissionError):
        registry.execute("read_file", {"path": "control-7232.token"})


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_javascript_cannot_read_outside_the_workspace(world):
    runtimes = find_runtimes()
    if runtimes.get("javascript", {}).get("kind") != "node" or not runtimes["javascript"].get("permission"):
        pytest.skip("this node has no permission model")
    here = str(world["data"]).replace("\\", "/")
    code = (f"const p = [{here[:10]!r} + {here[10:]!r}, 'vault', 'vault' + '.bin'].join('/');\n"
            "try { console.log(require('fs').readFileSync(p, 'utf8')) } catch (e) { console.log(e.code) }")
    res = run_code("javascript", code, world["ws"], runtimes, broker=world["broker"])
    assert CANARY not in res["stdout"] and "ERR_ACCESS_DENIED" in res["stdout"], res


def test_without_the_guard_the_same_snippet_reads_the_file(world):
    """The tests above fail without the correction: with no broker the snippet gets the file."""
    res = run_code("python", f"import os\nprint(open({runtime_path(world, 'vault', 'vault.bin')}).read())", world["ws"], find_runtimes())
    assert CANARY in res["stdout"]
