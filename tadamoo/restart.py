"""Restarting the running server from inside itself, however it was started: a plain process, or a Windows or Linux service.

A service that wraps this process (NSSM on Windows, systemd on Linux) is named in `TADAMOO_SERVICE_NAME` at install time: with it set, a restart asks
the service manager itself (`sc.exe`/`systemctl`) to stop and start that service, the same way an administrator would from the command line. Without
it, this is a plain process (a script, a foreground window): a detached helper waits for this process to end, then starts the exact command that
started it again; this process then exits."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence

Run = Callable[[Sequence[str]], Any]
Spawn = Callable[[Sequence[str]], Any]


def _exit_soon(seconds: float = 1.0) -> None:
    """This process's own answer to the client has to leave first: the exit happens a moment later, not inside the call that is still forming the reply."""
    threading.Timer(seconds, lambda: os._exit(0)).start()


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=60.0)


def _spawn_detached(argv: Sequence[str]) -> Any:
    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(list(argv), close_fds=True, **kwargs)


def service_name(env: Optional[Dict[str, str]] = None) -> str:
    """The name the service is registered under, or "" when this is a plain process."""
    return ((env if env is not None else os.environ).get("TADAMOO_SERVICE_NAME") or "").strip()


def service_restart_command(name: str, platform: str = sys.platform) -> List[str]:
    """The one command that stops and starts a named service: `sc.exe` on Windows (one call does both with `/y`... no, sc has no combined verb, so two
    calls joined with `&&` under cmd), `systemctl restart` on Linux."""
    if platform == "win32":
        return ["cmd", "/c", f"sc stop {name} & timeout /t 2 /nobreak >nul & sc start {name}"]
    return ["systemctl", "restart", name]


def relaunch_command(argv: Optional[Sequence[str]] = None, executable: str = sys.executable) -> List[str]:
    """What started this process, to start it again: this interpreter and the arguments it was given (frozen builds pass their own argv[0] as
    `executable` and an empty rest)."""
    return [executable, *(list(argv) if argv is not None else sys.argv)]


def restart(pid: int = 0, env: Optional[Dict[str, str]] = None, run: Run = _run, spawn: Spawn = _spawn_detached,
            argv: Optional[Sequence[str]] = None, executable: str = sys.executable, platform: str = sys.platform,
            exit_soon: Callable[[], None] = _exit_soon) -> Dict[str, Any]:
    """Restarts the server. Returns `{"way": "service", "name": ...}` or `{"way": "relaunch", "command": [...]}`. The service way asks the service
    manager, which does its own stopping of this process, and returns at once. The relaunch way starts a detached helper that waits for `pid` (this
    process, by default) to end before starting the command again, so the old and the new process are never both bound to the same port, then
    schedules this process's own exit a moment later (`exit_soon`), so a reply already on its way out still reaches the caller."""
    name = service_name(env)
    if name:
        cmd = service_restart_command(name, platform)
        run(cmd)
        return {"way": "service", "name": name, "command": cmd}
    me = pid or os.getpid()
    command = relaunch_command(argv, executable)
    if platform == "win32":
        helper = [sys.executable, "-c",
                  "import subprocess,sys,time\n"
                  f"pid={me}\n"
                  "while True:\n"
                  "    r=subprocess.run(['tasklist','/fi',f'PID eq {pid}'],capture_output=True,text=True)\n"
                  "    if str(pid) not in r.stdout: break\n"
                  "    time.sleep(0.3)\n"
                  f"subprocess.Popen({command!r})\n"]
    else:
        helper = [sys.executable, "-c",
                  "import os,subprocess,sys,time\n"
                  f"pid={me}\n"
                  "while True:\n"
                  "    try: os.kill(pid, 0)\n"
                  "    except OSError: break\n"
                  "    time.sleep(0.3)\n"
                  f"subprocess.Popen({command!r})\n"]
    spawn(helper)
    exit_soon()
    return {"way": "relaunch", "command": command}
