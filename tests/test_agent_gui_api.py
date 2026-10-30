"""Starting, watching, approving and stopping the agent through the server's API, with the real agent process."""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import handler
from serve.agent_runs import AgentRunner
from serve.approvals import ApprovalBoard, ApprovalError
from serve.tasks import ChangeJournal, TaskJournal
from test_e2e_mock_server import answer, start_server, tool_call, ws  # noqa: F401  (fixtures)

PY = f'"{sys.executable}"'


class Api:
    def __init__(self, url, key=""):
        self.url, self.key = url, key

    def __call__(self, method, path, body=None, key=None, headers=None, raw=False):
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        use = self.key if key is None else key
        if use:
            hdrs["Authorization"] = f"Bearer {use}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, headers=hdrs, method=method), timeout=15) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")


@pytest.fixture
def setup(tmp_path, monkeypatch, start_server):  # noqa: F811
    state = {}

    def make(scripts, api_key=""):
        url = start_server(scripts, api_key=api_key)
        tasks, changes, board = TaskJournal(tmp_path / "j"), ChangeJournal(tmp_path / "j"), ApprovalBoard()
        runner = AgentRunner(tmp_path / "j", url, api_key, tasks=tasks, board=board)
        for name, value in (("_TASKS", tasks), ("_CHANGES", changes), ("_APPROVALS", board), ("_RUNNER", runner)):
            monkeypatch.setattr(handler, name, value)
        state.update(runner=runner, tasks=tasks, changes=changes, board=board, api=Api(url, api_key))
        return state

    yield make
    runner = state.get("runner")
    if runner:
        for run in runner.view()["runs"]:
            if run["status"] == "running":
                runner.stop(run["id"])


def wait_for(predicate, seconds=40, step=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


def finished(api, run_id):
    runs = api("GET", "/api/agent/runs")[1]["runs"]
    run = next(r for r in runs if r["id"] == run_id)
    return run if run["status"] != "running" else None


def start(api, ws, goal="do it", **kw):
    return api("POST", "/api/agent/runs", {"workspace": str(ws), "goal": goal, **kw})


def test_a_run_started_from_the_page_works_in_the_folder_and_shows_up_everywhere(setup, ws):  # noqa: F811
    s = setup([tool_call("write_file", path="hello.txt", content="hi\n"), answer("Wrote hello.txt.")])
    status, body = start(s["api"], ws, "write hello.txt", mode="default")
    assert status == 200 and body["run"]["status"] == "running" and body["run"]["mode"] == "default"
    run = wait_for(lambda: finished(s["api"], body["run"]["id"]))
    assert (run["status"], run["exit_code"], run["summary"]) == ("done", 0, "Wrote hello.txt.")
    assert (ws / "hello.txt").read_text(encoding="utf-8") == "hi\n"
    (task,) = s["tasks"].load()["tasks"]
    assert task["id"] == f"agent-{run['id']}" and task["status"] == "done"
    (edit,) = s["changes"].view()["edits"]
    assert (edit["file"], edit["review"]) == ("hello.txt", "pending")
    log = s["api"]("GET", f"/api/agent/runs/{run['id']}/log")[1]["text"]
    assert log.splitlines() == ["> write_file hello.txt", "  ok", "Wrote hello.txt."]      # readable lines, not a JSON dump


def test_a_command_waits_for_the_person_and_runs_once_approved(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command=f'{PY} -c "open(\'made.txt\',\'w\').write(\'x\')"'), answer("Ran it.")])
    run_id = start(s["api"], ws, "run a command", mode="default")[1]["run"]["id"]
    pending = wait_for(lambda: s["api"]("GET", "/api/agent/approvals")[1]["pending"])
    assert [(p["tool"], p["run"], p["status"]) for p in pending] == [("shell_command", run_id, "pending")]
    assert "made.txt" in pending[0]["arguments"] and not (ws / "made.txt").exists()
    assert s["api"]("POST", f"/api/agent/approvals/{pending[0]['id']}", {"decision": "approve"})[0] == 200
    assert wait_for(lambda: finished(s["api"], run_id))["status"] == "done"
    assert (ws / "made.txt").exists()
    assert s["api"]("GET", "/api/agent/approvals")[1]["recent"][0]["status"] == "approved"


def test_a_refused_command_does_not_run_and_the_agent_goes_on(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command=f'{PY} -c "open(\'made.txt\',\'w\').write(\'x\')"'), answer("It was refused, so I stopped.")])
    run_id = start(s["api"], ws, "run a command", mode="default")[1]["run"]["id"]
    pending = wait_for(lambda: s["api"]("GET", "/api/agent/approvals")[1]["pending"])
    s["api"]("POST", f"/api/agent/approvals/{pending[0]['id']}", {"decision": "deny"})
    run = wait_for(lambda: finished(s["api"], run_id))
    assert run["status"] == "done" and not (ws / "made.txt").exists()


def test_stopping_a_run_that_waits_ends_it_and_leaves_nothing_open(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command="echo hi"), answer("never")])
    run_id = start(s["api"], ws, "wait", mode="default")[1]["run"]["id"]
    wait_for(lambda: s["api"]("GET", "/api/agent/approvals")[1]["pending"])
    assert s["api"]("POST", f"/api/agent/runs/{run_id}/stop", {})[0] == 200
    run = wait_for(lambda: finished(s["api"], run_id))
    assert run["status"] == "stopped"
    assert s["api"]("GET", "/api/agent/approvals")[1]["pending"] == []
    wait_for(lambda: not [t for t in s["tasks"].load()["tasks"] if t["id"] == f"agent-{run_id}"])   # closed tasks leave the panel
    assert s["api"]("POST", f"/api/agent/runs/{run_id}/stop", {})[0] == 409                 # nothing left to stop


def test_a_second_run_may_start_while_the_first_one_works(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command="echo hi"), answer("x")])
    first = start(s["api"], ws, "first", mode="default")[1]["run"]["id"]
    wait_for(lambda: s["api"]("GET", "/api/agent/approvals")[1]["pending"])
    status, body = start(s["api"], ws, "second")
    assert status == 200 and body["run"]["id"] != first
    for run_id in (first, body["run"]["id"]):
        s["api"]("POST", f"/api/agent/runs/{run_id}/stop", {})


@pytest.mark.parametrize("payload,fragment", [
    ({"workspace": "", "goal": "x"}, "Choose the folder"),
    ({"workspace": "relative/dir", "goal": "x"}, "full path"),
    ({"workspace": "Z:\\nowhere\\at\\all", "goal": "x"}, "does not exist"),
    ({"goal": "x"}, "Choose the folder"),
    ({"workspace": "@WS", "goal": "   "}, "Write what"),
    ({"workspace": "@WS", "goal": "x", "mode": "yolo"}, "mode must be"),
    ({"workspace": "@WS", "goal": "x", "max_turns": 0}, "number of steps"),
    ({"workspace": "@WS", "goal": "x", "allow": ["a\nb"]}, "Allow rules"),
])
def test_what_cannot_be_started_is_refused_in_words(setup, ws, payload, fragment):  # noqa: F811
    s = setup([answer("x")])
    body = {k: (str(ws) if v == "@WS" else v) for k, v in payload.items()}
    status, out = s["api"]("POST", "/api/agent/runs", body)
    assert status == 400 and fragment in out["error"]["message"]
    assert s["api"]("GET", "/api/agent/runs")[1]["runs"] == []


def test_automatic_mode_starts_without_a_confirmation_and_runs(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command=f'{PY} -c "open(\'auto.txt\',\'w\').write(\'x\')"'), answer("Done.")])
    run_id = start(s["api"], ws, "do it", mode="auto")[1]["run"]["id"]
    assert wait_for(lambda: finished(s["api"], run_id))["status"] == "done" and (ws / "auto.txt").exists()
    assert s["api"]("GET", "/api/agent/approvals")[1]["pending"] == []                      # nothing was asked


def test_a_run_without_a_mode_acts_without_asking_and_has_no_step_limit(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command=f'{PY} -c "open(\'plain.txt\',\'w\').write(\'x\')"'), answer("Done.")])
    status, body = start(s["api"], ws, "do it")
    assert status == 200 and body["run"]["mode"] == "auto"
    assert wait_for(lambda: finished(s["api"], body["run"]["id"]))["status"] == "done" and (ws / "plain.txt").exists()
    assert s["api"]("GET", "/api/agent/approvals")[1]["pending"] == []
    import inspect
    assert inspect.signature(AgentRunner.start).parameters["max_turns"].default == -1


def test_a_long_task_many_rules_many_steps_and_a_drive_root_are_not_refused(setup, ws):  # noqa: F811
    s = setup([answer("x")])
    status, body = start(s["api"], ws, "x" * 5000, allow=[f"shell_command:cmd{i}" for i in range(30)], max_turns=500)
    assert status == 200 and len(body["run"]["allow"]) == 30
    wait_for(lambda: finished(s["api"], body["run"]["id"]))
    anchor = Path(ws).resolve().anchor
    assert AgentRunner.check_workspace(anchor) == Path(anchor).resolve()


def test_plan_mode_changes_nothing(setup, ws):  # noqa: F811
    s = setup([tool_call("write_file", path="x.txt", content="x"), answer("Planned.")])
    run_id = start(s["api"], ws, "plan it", mode="plan")[1]["run"]["id"]
    wait_for(lambda: finished(s["api"], run_id))
    assert not (ws / "x.txt").exists()


def test_the_key_is_needed(setup, ws):  # noqa: F811
    s = setup([answer("x")], api_key="k")
    api = s["api"]
    assert api("GET", "/api/agent/runs", key="")[0] == 401 and api("POST", "/api/agent/runs", {"workspace": str(ws), "goal": "x"}, key="bad")[0] == 401
    assert api("GET", "/api/agent/approvals", key="")[0] == 401
    assert api("GET", "/api/agent/nothing")[0] == 404
    assert api("GET", "/api/agent/runs/zzz/log")[0] == 404


def test_a_run_that_was_going_when_the_server_ended_is_marked_interrupted(tmp_path):
    first = AgentRunner(tmp_path, "http://127.0.0.1:1")
    first.runs.append({"id": "abc123", "workspace": "x", "goal": "g", "mode": "default", "status": "running", "started": 1.0, "ended": None})
    first._save()
    again = AgentRunner(tmp_path, "http://127.0.0.1:1")
    assert again.view()["runs"][0]["status"] == "interrupted" and again.view()["active"] is None


def test_the_approval_board_answers_once_and_keeps_requests_waiting():
    now = [1000.0]
    board = ApprovalBoard(clock=lambda: now[0])
    a = board.request("r1", "shell_command", {"command": "x" * 5000}, "irreversible")
    assert len(a["arguments"]) <= 2000
    board.decide(a["id"], True)
    with pytest.raises(ApprovalError, match="already answered"):
        board.decide(a["id"], False)
    b = board.request("r1", "write_file", {})
    now[0] += 10 * 86400
    assert board.get(b["id"])["status"] == "pending"
    assert board.decide(b["id"], True)["status"] == "approved"
    with pytest.raises(ApprovalError):
        board.request("r1", "  ", {})
    c = board.request("r2", "t", {})
    board.refuse_all("r2")
    assert board.get(c["id"])["status"] == "expired" and board.view()["pending"] == []
    for _ in range(60):
        board.request("r3", "t", {})
    assert len(board.view()["pending"]) == 60


def test_going_back_from_the_page_restores_files_and_says_which_actions_stay_done(setup, ws):  # noqa: F811
    (ws / "a.txt").write_text("one\n", encoding="utf-8")
    s = setup([tool_call("write_file", path="a.txt", content="two\n", overwrite="true"),
               tool_call("shell_command", command=f'{PY} -c "print(1)"'), answer("Done.")])
    run_id = start(s["api"], ws, "change and run", mode="auto")[1]["run"]["id"]
    wait_for(lambda: finished(s["api"], run_id))
    status, body = s["api"]("GET", f"/api/agent/runs/{run_id}/checkpoints")
    first = body["checkpoints"][0]
    assert status == 200 and first["description"] == "before write_file" and [a["tool"] for a in first["not_undone"]] == ["shell_command"]
    status, body = s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": first["id"]})
    assert status == 200 and body["restored"] == first["id"] and body["safety_checkpoint"]
    assert [a["tool"] for a in body["not_undone"]] == ["shell_command"]                 # said, not asked
    assert (ws / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": body["safety_checkpoint"]})[0] == 200        # the rewind is undone
    assert (ws / "a.txt").read_text(encoding="utf-8") == "two\n"


def test_going_back_while_the_agent_runs_stops_it_and_unknown_things_are_refused(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command="echo hi"), answer("x")])
    run_id = start(s["api"], ws, "wait", mode="default")[1]["run"]["id"]
    wait_for(lambda: s["api"]("GET", "/api/agent/approvals")[1]["pending"])
    status, body = s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": "cp_1_1"})
    assert status == 409 and body["error"]["code"] == "no_checkpoint"                  # not "busy": the run is stopped instead
    wait_for(lambda: finished(s["api"], run_id))
    assert s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": "cp_9_9"})[1]["error"]["code"] == "no_checkpoint"
    assert s["api"]("POST", f"/api/agent/runs/nope/rewind", {"checkpoint": "cp_1_1"})[1]["error"]["code"] == "gone"
    assert s["api"]("GET", "/api/agent/runs/nope/checkpoints")[0] == 404
