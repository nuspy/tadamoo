"""Shell command tool: runs through the process sandbox, with an optional secrets broker.

On Windows the default shell, cmd.exe, runs only the first line of a command given with line breaks. A command of several lines is
therefore run from a script file in a private temporary folder: a `python -c "<code>"` wrapper as the Python file itself, anything else as a
batch file (cmd) or a PowerShell script. Every check (permissions, hooks, the deny list, the vault) is made on the command as the model
wrote it; only the way it is started differs.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness import environment
from harness.sandbox import MAX_TIMEOUT, ProcessSandbox, command_words, guard
from harness.secret_scan import PROGRAM_THAT_USES_THE_NETWORK, scripts_run, uses_network
from harness.secrets_broker import NETWORK_TOOLS, SecretsBroker, command_hosts, url_hosts
from harness.vault import VaultError

SHELLS = ("cmd", "powershell")
_RESULT_KEYS = ("exit_code", "stdout", "stderr", "success", "blocked", "timed_out", "truncated")
_PY_WRAPPER = re.compile(r"""\A(?P<exe>python3?|py)(?:\.exe)?(?P<u>\s+-u)?\s+-c\s+(?P<q>["'])(?P<code>.*)(?P=q)\Z""", re.S | re.I)
_NOT_FOUND = re.compile(r"not recognized|not recognised|CommandNotFound|command not found|non è riconosciut", re.I)
_PATH_MISSING = re.compile(r"cannot find the (?:file|path)|no such file or directory|cannot find path|file not found|non riesce a trovare", re.I)
_CMD_NOT_FOUND_CODE = 9009
_POWERSHELL_FLAGS = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
# set on the command's own first line so line numbers in errors stay the model's
_PS_PREFIX = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $ErrorView = 'NormalView'; "
              "if (Test-Path variable:PSStyle) { $PSStyle.OutputRendering = 'PlainText' }; ")


def _refused(text: str, blocked: bool = False) -> Dict[str, Any]:
    return {"exit_code": -1, "stdout": "", "stderr": text, "success": False, "blocked": blocked, "timed_out": False, "truncated": False}


def check_timeout(timeout: Any) -> Tuple[Optional[float], str]:
    """(seconds, note) for a valid timeout, capped at the maximum; (None, error text) for one that is not a finite number above zero."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        return None, (f"The timeout must be a finite number of seconds greater than 0 (at most {MAX_TIMEOUT:g}); got {timeout!r}. "
                      "The command was not run.")
    if timeout > MAX_TIMEOUT:
        return MAX_TIMEOUT, f"the timeout was lowered to the maximum, {MAX_TIMEOUT:g} seconds"
    return float(timeout), ""


def python_wrapper(command: str) -> Optional[Tuple[str, str, bool]]:
    """(interpreter name, code, unbuffered) when the whole command is `python -c "<code>"` (or with '...') and the code has line breaks;
    None for any other command, and for code whose quoting cannot be read with certainty."""
    match = _PY_WRAPPER.match(command.strip())
    if not match:
        return None
    code = match.group("code")
    if match.group("q") == '"':
        if re.search(r'(?<!\\)"', code):
            return None
        code = code.replace('\\"', '"')
    else:
        code = code.replace("'\\''", "\ufffd")                 # the shell idiom for a quote inside single quotes
        if "'" in code:
            return None
        code = code.replace("\ufffd", "'")
    if "\n" not in code and "\r" not in code:
        return None
    return match.group("exe").lower(), code, bool(match.group("u"))


def _write(path: Path, text: str, bom: bool = False) -> None:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + "\r\n".join(lines).encode("utf-8"))


def _plan(command: str, shell: str, make_folder: Callable[[], Path]) -> Tuple[Optional[List[str]], str, str]:
    """(argv, ran_as_script text, error) for a command that needs a program or script file on Windows; argv None when it runs as it is."""
    lines = command.strip().splitlines()
    if shell == "powershell":
        found = environment.powershell_path()
        if not found:
            return None, "", "PowerShell was not found on this machine (neither pwsh nor powershell.exe); use shell \"cmd\"."
        base = [found[1], *_POWERSHELL_FLAGS]
        if len(lines) <= 1:
            return [*base, "-Command", _PS_PREFIX + command.strip()], "", ""
        script = make_folder() / "command.ps1"
        _write(script, _PS_PREFIX + command.strip() + "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }\n", bom=True)
        return [*base, "-File", str(script)], f"{len(lines)} lines", ""
    wrapper = python_wrapper(command)
    if wrapper:
        name, code, unbuffered = wrapper
        picked = environment.pick_python(name)
        if not picked:
            return None, "", ("No Python interpreter works on this machine: python, python3 and py were tried and none printed a version. "
                              "The command was not run.")
        script = make_folder() / "snippet.py"
        _write(script, code + "\n")
        shown = f"{len(code.strip().splitlines())} lines of Python, run from a file with {picked[0]}"
        return [picked[1], *(["-u"] if unbuffered else []), str(script)], shown, ""
    if len(lines) <= 1:
        return None, "", ""
    script = make_folder() / "command.cmd"
    _write(script, "@echo off\nchcp 65001 >nul\n" + command.strip() + "\n")
    return [os.environ.get("COMSPEC") or shutil.which("cmd") or "cmd.exe", "/d", "/c", str(script)], f"{len(lines)} lines", ""


def _hint(command: str, shell: str) -> str:
    """Facts for a command the system did not recognise, or a Python command that prints nothing because it is a stub."""
    word = (command_words(command) or [""])[0]
    if word in environment.PYTHON_NAMES and word not in environment.working_pythons():
        working = ", ".join(f"`{n}`" for n in environment.working_pythons()) or "none"
        return f"`{word}` does not run Python on this machine; the Python commands that work: {working}."
    if not environment.is_windows():
        return "The program is not installed or not on PATH; this shell is sh."
    if shell == "powershell":
        return "This shell is PowerShell: the name is not a cmdlet, function, alias or program on PATH. shell \"cmd\" runs cmd.exe instead."
    return ("This shell is Windows cmd.exe: it has `dir` where Unix has `ls`, `type` for `cat`, `findstr` for `grep`. "
            "shell \"powershell\" runs the command in PowerShell instead. A Python snippet can be passed as one line or on several lines.")


def _folder_hint(cwd: Path) -> str:
    """Facts for a command that could not find a file or folder: where it ran and what is there."""
    try:
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in Path(cwd).resolve().iterdir())
    except OSError:
        names = []
    shown = ", ".join(names[:15]) + (f", and {len(names) - 15} more" if len(names) > 15 else "")
    return f"The command ran in {Path(cwd).resolve()}; relative paths are relative to it." + (f" It holds: {shown}." if names else " It is empty.")


def execute_shell_command(
    command: str,
    cwd: str | Path,
    timeout: float = 30.0,
    max_output_bytes: int = 50_000,
    secrets: Optional[SecretsBroker] = None,
    shell: str = "cmd",
) -> Dict[str, Any]:
    """Runs `command` in `cwd`. {{secret:NAME}} placeholders are filled in only here, at the boundary,
    and the secret values are masked again in everything returned to the model."""
    seconds, note = check_timeout(timeout)
    if seconds is None:
        return _refused(note)
    if shell not in SHELLS:
        return _refused(f"The shell must be one of {', '.join(SHELLS)}; got {shell!r}. The command was not run.")
    if shell == "powershell" and not environment.is_windows():
        return _refused("Only the default shell (sh) exists on this system; \"powershell\" is available on Windows. The command was not run.")
    sandbox = ProcessSandbox(cwd, timeout=seconds, max_output_bytes=max_output_bytes)
    hit = secrets.protected_hit(command) if secrets else ""
    if hit:
        return _refused(f"A command cannot name '{hit}': the vault, the payment files and the browser's sign-ins are the person's, and they are read in the Wallet.", True)
    if secrets and secrets.has_references(command):
        try:
            tools = [w for w in command_words(command) if w in NETWORK_TOOLS]
            hosts = set(command_hosts(command, tools))
            scripts = scripts_run(command, cwd)                              # the sites the scripts it runs name count as the command's own
            hosts |= set(url_hosts(scripts))
            if not tools and uses_network(command + " " + scripts):
                tools.append(PROGRAM_THAT_USES_THE_NETWORK)                # an address built at run time is not visible: a secret with limited sites is not given to it
            secrets.check_destinations(command, sorted(hosts), tools)
        except PermissionError as exc:
            return _refused(str(exc), True)
    try:
        to_run = secrets.resolve_tokens(command) if secrets else command
    except VaultError as exc:
        return _refused(str(exc), True)
    folders: List[Path] = []

    def make_folder() -> Path:
        folders.append(Path(tempfile.mkdtemp(prefix="basalt-cmd-")))
        return folders[-1]

    argv: Optional[List[str]] = None
    as_script = ""
    extra_env: Optional[Dict[str, str]] = guard.environment(secrets.places(), os.environ.get("PYTHONPATH", "")) if secrets else {}
    try:
        if environment.is_windows():
            extra_env = {**extra_env, "PYTHONIOENCODING": "utf-8"}
            if to_run.strip():
                argv, as_script, error = _plan(to_run, shell, make_folder)
                if error:
                    return _refused(error)
        res = sandbox.run(to_run, timeout=seconds, extra_env=extra_env or None, argv=argv)
    finally:
        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)
    if secrets:
        res["stdout"] = secrets.mask_secrets(res["stdout"])
        res["stderr"] = secrets.mask_secrets(res["stderr"])
    out = {k: res[k] for k in _RESULT_KEYS}
    if as_script and not out["blocked"]:
        out["ran_as_script"] = as_script
    notes = [note] if note else []
    silent = out["success"] and not out["stdout"] and not out["stderr"]
    if silent:
        notes.append("the command ran and printed nothing")
    if notes:
        out["note"] = "; ".join(notes)
    word = (command_words(command) or [""])[0]
    stub = silent and not as_script and word in environment.PYTHON_NAMES and word not in environment.working_pythons()
    if stub or (not out["blocked"] and not out["timed_out"] and (out["exit_code"] == _CMD_NOT_FOUND_CODE or _NOT_FOUND.search(out["stderr"]))):
        out["hint"] = _hint(command, shell)
    elif not out["blocked"] and not out["timed_out"] and _PATH_MISSING.search(out["stderr"]):
        out["hint"] = _folder_hint(sandbox.workspace_root)
    return out
