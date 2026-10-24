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


def test_one_run_at_a_time(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command="echo hi"), answer("x")])
    first = start(s["api"], ws, "first", mode="default")[1]["run"]["id"]
    wait_for(lambda: s["api"]("GET", "/api/agent/approvals")[1]["pending"])
    status, body = start(s["api"], ws, "second")
    assert status == 409 and "already running" in body["error"]["message"]
    s["api"]("POST", f"/api/agent/runs/{first}/stop", {})


@pytest.mark.parametrize("payload,fragment", [
    ({"workspace": "", "goal": "x"}, "Choose the folder"),
    ({"workspace": "relative/dir", "goal": "x"}, "full path"),
    ({"workspace": "Z:\\nowhere\\at\\all", "goal": "x"}, "does not exist"),
    ({"goal": "x"}, "Choose the folder"),
    ({"workspace": "@WS", "goal": "   "}, "Write what"),
    ({"workspace": "@WS", "goal": "x" * 5000}, "too long"),
    ({"workspace": "@WS", "goal": "x", "mode": "yolo"}, "mode must be"),
    ({"workspace": "@WS", "goal": "x", "mode": "auto"}, "Confirm that you want that"),
    ({"workspace": "@WS", "goal": "x", "mode": "auto", "confirm_auto": "yes"}, "Confirm that you want that"),
    ({"workspace": "@WS", "goal": "x", "max_turns": 0}, "between 1 and 50"),
    ({"workspace": "@WS", "goal": "x", "allow": ["a\nb"]}, "Allow rules"),
    ({"workspace": "C:\\", "goal": "x"}, "not a whole drive"),
])
def test_what_cannot_be_started_is_refused_in_words(setup, ws, payload, fragment):  # noqa: F811
    s = setup([answer("x")])
    body = {k: (str(ws) if v == "@WS" else v) for k, v in payload.items()}
    status, out = s["api"]("POST", "/api/agent/runs", body)
    assert status == 400 and fragment in out["error"]["message"]
    assert s["api"]("GET", "/api/agent/runs")[1]["runs"] == []


def test_automatic_mode_needs_the_confirmation_and_then_runs(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command=f'{PY} -c "open(\'auto.txt\',\'w\').write(\'x\')"'), answer("Done.")])
    run_id = start(s["api"], ws, "do it", mode="auto", confirm_auto=True)[1]["run"]["id"]
    assert wait_for(lambda: finished(s["api"], run_id))["status"] == "done" and (ws / "auto.txt").exists()
    assert s["api"]("GET", "/api/agent/approvals")[1]["pending"] == []                      # nothing was asked


def test_plan_mode_changes_nothing(setup, ws):  # noqa: F811
    s = setup([tool_call("write_file", path="x.txt", content="x"), answer("Planned.")])
    run_id = start(s["api"], ws, "plan it", mode="plan")[1]["run"]["id"]
    wait_for(lambda: finished(s["api"], run_id))
    assert not (ws / "x.txt").exists()


def test_only_this_pc_and_this_page_can_use_it_and_the_key_is_needed(setup, ws):  # noqa: F811
    s = setup([answer("x")], api_key="k")
    api = s["api"]
    assert api("GET", "/api/agent/runs", key="")[0] == 401 and api("POST", "/api/agent/runs", {"workspace": str(ws), "goal": "x"}, key="bad")[0] == 401
    assert api("POST", "/api/agent/runs", {"workspace": str(ws), "goal": "x"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert api("POST", "/api/agent/approvals/abc", {"decision": "approve"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert api("GET", "/api/agent/approvals", key="")[0] == 401
    assert api("GET", "/api/agent/nothing")[0] == 404
    assert api("GET", "/api/agent/runs/zzz/log")[0] == 404


def test_a_run_that_was_going_when_the_server_ended_is_marked_interrupted(tmp_path):
    first = AgentRunner(tmp_path, "http://127.0.0.1:1")
    first.runs.append({"id": "abc123", "workspace": "x", "goal": "g", "mode": "default", "status": "running", "started": 1.0, "ended": None})
    first._save()
    again = AgentRunner(tmp_path, "http://127.0.0.1:1")
    assert again.view()["runs"][0]["status"] == "interrupted" and again.view()["active"] is None


def test_the_approval_board_expires_and_answers_once():
    now = [1000.0]
    board = ApprovalBoard(ttl=10, clock=lambda: now[0])
    a = board.request("r1", "shell_command", {"command": "x" * 5000}, "irreversible")
    assert len(a["arguments"]) <= 2000
    board.decide(a["id"], True)
    with pytest.raises(ApprovalError, match="already answered"):
        board.decide(a["id"], False)
    b = board.request("r1", "write_file", {})
    now[0] += 11
    assert board.get(b["id"])["status"] == "expired"
    with pytest.raises(ApprovalError):
        board.decide(b["id"], True)
    with pytest.raises(ApprovalError):
        board.request("r1", "  ", {})
    c = board.request("r2", "t", {})
    board.refuse_all("r2")
    assert board.get(c["id"])["status"] == "expired" and board.view()["pending"] == []
    for _ in range(60):
        try:
            board.request("r3", "t", {})
        except ApprovalError as e:
            assert "too many" in str(e)
            break
    else:
        raise AssertionError("pending approvals are not capped")


def test_going_back_from_the_page_restores_files_and_asks_before_leaving_actions_done(setup, ws):  # noqa: F811
    (ws / "a.txt").write_text("one\n", encoding="utf-8")
    s = setup([tool_call("write_file", path="a.txt", content="two\n"),
               tool_call("shell_command", command=f'{PY} -c "print(1)"'), answer("Done.")])
    run_id = start(s["api"], ws, "change and run", mode="auto", confirm_auto=True)[1]["run"]["id"]
    wait_for(lambda: finished(s["api"], run_id))
    status, body = s["api"]("GET", f"/api/agent/runs/{run_id}/checkpoints")
    first = body["checkpoints"][0]
    assert status == 200 and first["description"] == "before write_file" and [a["tool"] for a in first["not_undone"]] == ["shell_command"]
    status, body = s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": first["id"]})
    assert status == 409 and body["error"]["code"] == "needs_yes"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "two\n"                       # nothing was touched
    status, body = s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": first["id"], "accept_irreversible": True})
    assert status == 200 and body["restored"] == first["id"] and body["safety_checkpoint"]
    assert (ws / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": body["safety_checkpoint"]})[0] == 200        # the rewind is undone
    assert (ws / "a.txt").read_text(encoding="utf-8") == "two\n"


def test_going_back_is_refused_while_the_agent_runs_and_for_unknown_things(setup, ws):  # noqa: F811
    s = setup([tool_call("shell_command", command="echo hi"), answer("x")])
    run_id = start(s["api"], ws, "wait", mode="default")[1]["run"]["id"]
    wait_for(lambda: s["api"]("GET", "/api/agent/approvals")[1]["pending"])
    status, body = s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": "cp_1_1"})
    assert status == 409 and body["error"]["code"] == "busy"
    s["api"]("POST", f"/api/agent/runs/{run_id}/stop", {})
    wait_for(lambda: finished(s["api"], run_id))
    assert s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": "cp_9_9"})[1]["error"]["code"] == "no_checkpoint"
    assert s["api"]("POST", f"/api/agent/runs/nope/rewind", {"checkpoint": "cp_1_1"})[1]["error"]["code"] == "gone"
    assert s["api"]("GET", "/api/agent/runs/nope/checkpoints")[0] == 404
    assert s["api"]("POST", f"/api/agent/runs/{run_id}/rewind", {"checkpoint": "cp_1_1"}, headers={"Origin": "http://evil.example"})[0] == 403
