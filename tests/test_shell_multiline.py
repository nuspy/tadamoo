"""shell_command with commands of several lines, the `shell` argument, the timeout check and the notes and hints in its result.

The Windows parts need cmd.exe; the parsing and the checks that come before any run are tested everywhere.
"""
import math
import shutil
import sys
from pathlib import Path

import pytest

from harness import environment
from harness.hooks import HookManager
from harness.permissions import PermissionLevel, PermissionManager, PermissionRule
from harness.sandbox import MAX_TIMEOUT, blocked_reason
from harness.secrets_broker import SecretsBroker
from harness.tools import create_standard_tool_registry, shell
from harness.tools.registry import EffectClass, ToolArgumentError
from harness.tools.shell import check_timeout, execute_shell_command, python_wrapper

windows = pytest.mark.skipif(sys.platform != "win32", reason="the shell is cmd.exe")
needs_powershell = pytest.mark.skipif(sys.platform != "win32" or environment.powershell_path() is None, reason="PowerShell on Windows")
SHELL = ("shell_command", EffectClass.IRREVERSIBLE)


@pytest.fixture
def python_ok(monkeypatch):
    """Every Python name runs the interpreter of this test run, so the result does not depend on the machine's PATH."""
    monkeypatch.setattr(environment, "working_pythons", lambda: {n: (sys.executable, "Python 3") for n in environment.PYTHON_NAMES})


@pytest.fixture
def temp_folders(monkeypatch):
    """The private folders the tool made, so a test can look at whether they are gone."""
    made = []
    real = shell.tempfile.mkdtemp

    def record(*args, **kwargs):
        made.append(Path(real(*args, **kwargs)))
        return str(made[-1])
    monkeypatch.setattr(shell.tempfile, "mkdtemp", record)
    return made


# -- reading a python wrapper ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("command, interpreter, unbuffered", [
    ('python -c "\nimport re\nprint(1)\n"', "python", False),
    ("python3 -c '\nprint(1)\nprint(2)'", "python3", False),
    ('py -u -c "x = 1\nprint(x)"', "py", True),
    ('  python.exe -c "a\nb"  ', "python", False),
])
def test_a_multi_line_python_wrapper_is_read_as_code(command, interpreter, unbuffered):
    name, code, u = python_wrapper(command)
    assert (name, u) == (interpreter, unbuffered) and "\n" in code


@pytest.mark.parametrize("command", [
    'python -c "print(1)"',                       # one line: the normal path
    'python script.py',
    'python -c "\nprint(1)\n" > out.txt',          # more than the wrapper
    'python -c "\nprint("hi")\n"',                 # a quote the shell would have ended the string with
    "python -c '\nprint(\"it's\")\n'",
    'echo a\npython -c "x"',
    'ls\npwd',
])
def test_anything_else_is_not_a_python_wrapper(command):
    assert python_wrapper(command) is None


def test_escaped_quotes_inside_the_code_are_unescaped():
    assert python_wrapper('python -c "\nprint(\\"hi\\")\n"')[1] == '\nprint("hi")\n'
    assert python_wrapper("python -c '\nprint(\"it'\\''s\")\n'")[1] == "\nprint(\"it's\")\n"


# -- the timeout ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1, 0, "10", True, [5]])
def test_a_timeout_that_is_not_a_finite_positive_number_is_refused_with_a_clear_error(tmp_path, bad):
    marker = tmp_path / "ran.txt"
    res = execute_shell_command(f"echo x> {marker.name}", tmp_path, timeout=bad)
    assert not res["success"] and res["exit_code"] == -1 and not marker.exists()
    assert "timeout must be a finite number of seconds greater than 0" in res["stderr"] and "was not run" in res["stderr"]


def test_a_timeout_above_the_maximum_is_lowered_and_says_so():
    assert check_timeout(5) == (5.0, "")
    seconds, note = check_timeout(10 ** 9)
    assert seconds == MAX_TIMEOUT and str(int(MAX_TIMEOUT)) in note
    assert check_timeout(math.nan)[0] is None


def test_an_unknown_shell_is_refused(tmp_path):
    res = execute_shell_command("echo x", tmp_path, shell="zsh")
    assert not res["success"] and "must be one of cmd, powershell" in res["stderr"]


@pytest.mark.skipif(sys.platform == "win32", reason="PowerShell exists on Windows")
def test_powershell_is_refused_where_only_the_default_shell_exists(tmp_path):
    res = execute_shell_command("echo x", tmp_path, shell="powershell")
    assert not res["success"] and "Only the default shell" in res["stderr"]


def test_the_tool_schema_documents_both_shells_and_rejects_others():
    registry, _ = create_standard_tool_registry(Path.cwd())
    spec = next(t for t in registry.schemas() if t["function"]["name"] == "shell_command")["function"]["parameters"]["properties"]
    assert spec["shell"]["enum"] == ["cmd", "powershell"] and "PowerShell" in spec["shell"]["description"]
    with pytest.raises(ToolArgumentError):
        registry.validate("shell_command", {"command": "x", "shell": "fish"})


# -- running on Windows ---------------------------------------------------------------------------------------------------------

@windows
def test_cmd_alone_drops_the_lines_after_the_first_which_is_why_the_tool_does_not_hand_it_the_text(tmp_path):
    import subprocess
    done = subprocess.run('python -c "\nprint(1)\n"', shell=True, capture_output=True, cwd=tmp_path)
    assert done.stdout == b""                                  # the failure the script path exists for


@windows
def test_a_multi_line_python_wrapper_runs_all_its_lines_from_a_private_file(tmp_path, python_ok, temp_folders):
    code = "import re\ntext = 'one two three'\nprint(len(re.findall(r'\\w+', text)))\nprint('caff\u00e8')"
    res = execute_shell_command(f'python3 -c "\n{code}\n"', tmp_path)
    assert res["success"], res
    assert res["stdout"].split() == ["3", "caff\u00e8"]
    assert res["ran_as_script"].startswith("4 lines of Python")
    assert len(temp_folders) == 1 and not temp_folders[0].exists()
    assert list(tmp_path.iterdir()) == []                      # nothing was left in the working folder


@windows
def test_the_snippet_runs_in_the_working_folder_and_with_the_single_quoted_form(tmp_path, python_ok):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    res = execute_shell_command("python -c '\nimport os\nprint(open(\"a.txt\").read())\nprint(os.getcwd())\n'", tmp_path)
    assert res["success"], res
    assert res["stdout"].split()[0] == "hello" and Path(res["stdout"].split()[1]).resolve() == tmp_path.resolve()


@windows
def test_an_error_in_the_snippet_is_reported_with_its_exit_code(tmp_path, python_ok):
    res = execute_shell_command('python -c "\nimport sys\nprint(\'before\')\nsys.exit(3)\n"', tmp_path)
    assert res["exit_code"] == 3 and not res["success"] and res["stdout"].strip() == "before"


@windows
def test_the_interpreter_used_is_one_that_exists_on_this_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "working_pythons", lambda: {"python": (sys.executable, "Python 3.12")})
    res = execute_shell_command('python3 -c "\nprint(7)\nprint(8)\n"', tmp_path)
    assert res["success"] and res["stdout"].split() == ["7", "8"] and res["ran_as_script"].endswith("with python")


@windows
def test_with_no_working_python_the_command_is_not_run_and_the_error_says_why(tmp_path, monkeypatch, temp_folders):
    monkeypatch.setattr(environment, "working_pythons", lambda: {})
    res = execute_shell_command('python3 -c "\nprint(7)\nprint(8)\n"', tmp_path)
    assert not res["success"] and "No Python interpreter works" in res["stderr"] and "was not run" in res["stderr"]
    assert all(not f.exists() for f in temp_folders)


@windows
def test_the_snippet_file_is_removed_when_the_command_times_out(tmp_path, python_ok, temp_folders):
    res = execute_shell_command('python -c "\nimport time\ntime.sleep(60)\n"', tmp_path, timeout=1)
    assert res["timed_out"] and not res["success"]
    assert len(temp_folders) == 1 and not temp_folders[0].exists()


@windows
def test_other_multi_line_commands_run_as_a_batch_file_in_order(tmp_path, temp_folders):
    res = execute_shell_command("echo first\r\necho second\necho third > out.txt\ntype out.txt", tmp_path)
    assert res["success"], res
    assert res["stdout"].split() == ["first", "second", "third"]
    assert res["ran_as_script"] == "4 lines"
    assert not temp_folders[0].exists()


@windows
def test_a_failing_middle_line_does_not_drop_the_lines_after_it(tmp_path):
    res = execute_shell_command("echo one\nthis_program_does_not_exist_zz\necho three", tmp_path)
    assert res["stdout"].split() == ["one", "three"] and "not recognized" in res["stderr"]
    assert "hint" in res


@windows
def test_a_single_line_command_runs_as_before_without_a_script(tmp_path, temp_folders):
    res = execute_shell_command("echo hi", tmp_path)
    assert res["success"] and res["stdout"].strip() == "hi" and "ran_as_script" not in res and temp_folders == []


@needs_powershell
def test_powershell_runs_a_single_line_and_several_lines(tmp_path, temp_folders):
    one = execute_shell_command("Write-Output (1 + 2)", tmp_path, shell="powershell")
    assert one["success"] and one["stdout"].strip() == "3" and "ran_as_script" not in one
    many = execute_shell_command("$a = 40\n$b = 2\nWrite-Output ($a + $b)\nWrite-Output 'caff\u00e8'", tmp_path, shell="powershell")
    assert many["success"], many
    assert many["stdout"].split() == ["42", "caff\u00e8"] and many["ran_as_script"] == "4 lines"
    assert not temp_folders[0].exists()


@needs_powershell
def test_powershell_exit_codes_come_through(tmp_path):
    assert execute_shell_command("exit 4", tmp_path, shell="powershell")["exit_code"] == 4
    assert execute_shell_command("Write-Output a\nexit 5", tmp_path, shell="powershell")["exit_code"] == 5


@needs_powershell
def test_an_unknown_powershell_command_gets_a_powershell_hint(tmp_path):
    res = execute_shell_command("Get-NoSuchThingAtAll", tmp_path, shell="powershell")
    assert not res["success"] and "PowerShell" in res["hint"] and "shell \"cmd\"" in res["hint"]


# -- notes and hints ------------------------------------------------------------------------------------------------------------

@windows
def test_a_command_that_succeeds_without_output_keeps_its_fields_and_says_so(tmp_path):
    res = execute_shell_command("rem nothing at all", tmp_path)
    assert res["success"] and res["stdout"] == "" and res["stderr"] == "" and res["exit_code"] == 0
    assert res["note"] == "the command ran and printed nothing"


@windows
def test_a_command_with_output_has_no_note(tmp_path):
    assert "note" not in execute_shell_command("echo hi", tmp_path)


@windows
def test_a_command_cmd_does_not_know_gets_a_hint_with_the_facts(tmp_path):
    res = execute_shell_command("this_program_does_not_exist_zz --all", tmp_path)
    assert not res["success"]
    hint = res["hint"]
    assert "cmd.exe" in hint and "`dir`" in hint and "`ls`" in hint and 'shell "powershell"' in hint and "one line or on several lines" in hint


def test_a_python_name_that_does_not_run_gets_a_hint_naming_the_ones_that_do(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "working_pythons", lambda: {"python": (sys.executable, "Python 3.12")})
    monkeypatch.setattr(shell.ProcessSandbox, "run", lambda self, command, **kw: {
        "exit_code": 9009, "stdout": "", "stderr": "Python was not found", "success": False, "blocked": False, "timed_out": False, "truncated": False})
    res = execute_shell_command("python3 script.py", tmp_path)
    assert "`python3` does not run Python" in res["hint"] and "`python`" in res["hint"]


def test_a_store_stub_that_prints_nothing_gets_the_same_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "working_pythons", lambda: {"py": (sys.executable, "Python 3.12")})
    monkeypatch.setattr(shell.ProcessSandbox, "run", lambda self, command, **kw: {
        "exit_code": 0, "stdout": "", "stderr": "", "success": True, "blocked": False, "timed_out": False, "truncated": False})
    res = execute_shell_command("python3 script.py", tmp_path)
    assert res["note"] == "the command ran and printed nothing" and "`python3` does not run Python" in res["hint"] and "`py`" in res["hint"]


@windows
def test_a_missing_file_in_a_command_gets_the_working_folder_and_its_contents(tmp_path):
    (tmp_path / "story.txt").write_text("x", encoding="utf-8")
    res = execute_shell_command("type storia.txt", tmp_path)
    assert not res["success"] and str(tmp_path.resolve()) in res["hint"] and "story.txt" in res["hint"]


def test_the_notes_and_hints_are_added_only_when_they_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(shell.environment, "is_windows", lambda: False)
    monkeypatch.setattr(shell.ProcessSandbox, "run", lambda self, command, **kw: {
        "exit_code": 127, "stdout": "", "stderr": "sh: 1: nothing: command not found", "success": False, "blocked": False,
        "timed_out": False, "truncated": False})
    res = execute_shell_command("nothing", tmp_path)
    assert "sh" in res["hint"] and "note" not in res


# -- safety: the judges see the command as written, the whole of it --------------------------------------------------------------

WRAPPER_WITH_DANGER = 'python -c "\nimport os\nos.system(\'rm -rf /\')\n"'


def test_the_safety_hook_judges_the_whole_multi_line_text():
    hooks = HookManager()
    assert hooks.trigger_pre_tool("shell_command", {"command": WRAPPER_WITH_DANGER})
    assert hooks.trigger_pre_tool("shell_command", {"command": "echo hi\nrm -rf /"})
    assert hooks.trigger_pre_tool("shell_command", {"command": "Write-Output 1\nRemove-Item -Recurse C:\\", "shell": "powershell"})
    assert not hooks.trigger_pre_tool("shell_command", {"command": 'python -c "\nprint(1)\n"'})


def test_a_deny_rule_still_catches_a_program_named_inside_a_multi_line_command():
    pm = PermissionManager(level=PermissionLevel.AUTO, rules=[PermissionRule("shell_command:reg *", "deny")])
    allowed, why = pm.evaluate(*SHELL, {"command": "echo hi\nreg add HKCU\\Software\\x"})
    assert not allowed and "denied by rule" in why
    allowed, why = pm.evaluate(*SHELL, {"command": 'python -c "\nprint(1)\n"\nreg query x', "shell": "cmd"})
    assert not allowed


def test_an_allow_rule_for_python_covers_a_multi_line_wrapper_because_the_person_allowed_python():
    asked = []
    pm = PermissionManager(level=PermissionLevel.DEFAULT, rules=[PermissionRule("shell_command:python *", "allow")],
                           confirm_callback=lambda tool, args: asked.append(args["command"]) or False)
    assert pm.evaluate(*SHELL, {"command": "python build.py"})[0] and asked == []
    wrapper = 'python -c "' + chr(10) + 'import shutil' + chr(10) + "shutil.rmtree('x')" + chr(10) + '"'
    assert pm.evaluate(*SHELL, {"command": wrapper})[0] and asked == []
    other = wrapper + chr(10) + "del /s x"
    assert not pm.evaluate(*SHELL, {"command": other})[0] and asked == [other]      # a statement no rule covers still asks, with the whole text


def test_the_deny_list_judges_every_line_of_a_wrapper_and_nothing_runs(tmp_path):
    assert blocked_reason('python -c "\nprint(1)\nreg add HKCU\\x\n"') is not None
    marker = tmp_path / "ran.txt"
    command = f'python -c "\nopen(r\'{marker}\', \'w\').write(\'x\')\nnetsh winsock reset\n"'
    res = execute_shell_command(command, tmp_path)
    assert res["blocked"] and not res["success"] and "blocked by sandbox" in res["stderr"]
    assert not marker.exists() and "ran_as_script" not in res


def test_the_protected_names_check_sees_the_code_inside_a_wrapper(tmp_path):
    broker = SecretsBroker()
    broker.protect(tmp_path / "vault", tmp_path / "payments")
    marker = tmp_path / "ran.txt"
    command = f'python -c "\nprint(open(r\'{tmp_path / "vault" / "x"}\').read())\nopen(r\'{marker}\', \'w\')\n"'
    res = execute_shell_command(command, tmp_path, secrets=broker)
    assert res["blocked"] and "cannot name" in res["stderr"] and not marker.exists()
    ps = execute_shell_command(f"Get-Content {tmp_path / 'vault' / 'x'}\nWrite-Output 1", tmp_path, secrets=broker, shell="powershell")
    assert ps["blocked"]


@windows
def test_a_secret_in_a_multi_line_snippet_is_filled_in_at_the_boundary_and_masked(tmp_path, python_ok, temp_folders):
    broker = SecretsBroker({"TOKEN": "s3cr3t-value"})
    res = execute_shell_command('python -c "\nvalue = \'{{secret:TOKEN}}\'\nprint(value)\n"', tmp_path, secrets=broker)
    assert res["success"], res
    assert "s3cr3t-value" not in res["stdout"] and "[REDACTED:TOKEN]" in res["stdout"]
    assert not temp_folders[0].exists()                         # the file that held the value is gone


@windows
def test_the_environment_stays_scrubbed_and_the_output_capped_for_a_script(tmp_path, python_ok, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    res = execute_shell_command('python -c "\nimport os\nprint(sorted(os.environ))\nprint(\'x\' * 200000)\n"', tmp_path, max_output_bytes=1000)
    assert res["success"] and "GITHUB_TOKEN" not in res["stdout"] and res["truncated"] and len(res["stdout"]) <= 1000


@windows
def test_the_registered_tool_runs_a_wrapper_and_the_workspace_watch_still_records_files(tmp_path, python_ok):
    registry, sandbox = create_standard_tool_registry(tmp_path)
    res = registry.execute("shell_command", {"command": 'python -c "\nopen(\'made.txt\', \'w\').write(\'abc\')\nprint(\'done\')\n"'})
    assert res["success"] and res["stdout"].strip() == "done"
    assert (tmp_path / "made.txt").read_text() == "abc"
    assert [c["file"] for c in sandbox.changes] == ["made.txt"]
