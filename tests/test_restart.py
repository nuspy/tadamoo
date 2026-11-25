"""Restarting the server: a named service asks its own manager, a plain process is relaunched by a detached helper. No real service or process is touched."""
from tadamoo import restart


def test_the_service_name_comes_from_its_own_environment_variable_only():
    assert restart.service_name({}) == ""
    assert restart.service_name({"TADAMOO_SERVICE_NAME": " TaDaMoo "}) == "TaDaMoo"
    assert restart.service_name({"OTHER": "x"}) == ""


def test_the_service_restart_command_asks_the_right_manager():
    win = restart.service_restart_command("TaDaMoo", "win32")
    assert win[0] == "cmd" and "sc stop TaDaMoo" in win[-1] and "sc start TaDaMoo" in win[-1]
    assert restart.service_restart_command("tadamoo", "linux") == ["systemctl", "restart", "tadamoo"]


def test_a_named_service_is_restarted_through_its_manager_not_relaunched():
    calls = {"run": [], "spawn": [], "exit": 0}
    out = restart.restart(env={"TADAMOO_SERVICE_NAME": "TaDaMoo"}, run=lambda cmd: calls["run"].append(cmd),
                          spawn=lambda cmd: calls["spawn"].append(cmd), exit_soon=lambda: calls.__setitem__("exit", calls["exit"] + 1), platform="win32")
    assert out["way"] == "service" and out["name"] == "TaDaMoo"
    assert len(calls["run"]) == 1 and "sc stop TaDaMoo" in calls["run"][0][-1]
    assert calls["spawn"] == [] and calls["exit"] == 0              # the service manager restarts it: no relaunch helper, no self-exit either


def test_under_a_supervisor_a_restart_only_exits_with_an_error_code_and_spawns_nothing():
    calls = {"spawn": [], "exit": []}
    out = restart.restart(env={"TADAMOO_SUPERVISED": "1"}, run=lambda cmd: calls.__setitem__("ran", cmd), spawn=lambda cmd: calls["spawn"].append(cmd),
                          exit_soon=lambda code=0: calls["exit"].append(code))
    assert out == {"way": "supervisor", "exit": restart.RESTART_EXIT} and calls["spawn"] == [] and calls["exit"] == [75] and "ran" not in calls
    assert restart.supervised({"TADAMOO_SUPERVISED": "1"}) and not restart.supervised({"TADAMOO_SUPERVISED": "0"}) and not restart.supervised({})
    named = restart.restart(env={"TADAMOO_SUPERVISED": "1", "TADAMOO_SERVICE_NAME": "svc"}, run=lambda cmd: None, spawn=lambda cmd: None, exit_soon=lambda code=0: None, platform="win32")
    assert named["way"] == "service"              # a service named to the manager is still restarted through it


def test_a_plain_process_spawns_a_detached_helper_then_schedules_its_own_exit():
    calls = {"run": [], "spawn": [], "exit": 0}
    out = restart.restart(pid=4242, env={}, run=lambda cmd: calls["run"].append(cmd), spawn=lambda cmd: calls["spawn"].append(cmd),
                          exit_soon=lambda: calls.__setitem__("exit", calls["exit"] + 1),
                          argv=["--config", "default"], executable="C:/python.exe", platform="win32")
    assert out["way"] == "relaunch" and out["command"] == ["C:/python.exe", "--config", "default"]
    assert calls["run"] == [] and len(calls["spawn"]) == 1 and calls["exit"] == 1
    helper = calls["spawn"][0]
    assert "4242" in helper[-1] and "python.exe" in helper[-1] and "default" in helper[-1]


def test_relaunch_command_defaults_to_this_interpreter_and_its_own_argv(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["server.py", "--port", "7232"])
    assert restart.relaunch_command() == [sys.executable, "server.py", "--port", "7232"]
    assert restart.relaunch_command(["--other"], executable="X.exe") == ["X.exe", "--other"]
