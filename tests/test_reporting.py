"""The agent shows its run and its edits in the server's Monitor."""
import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.headless import main, run_headless
from harness.reporting import PanelReporter
from serve import handler
from serve.tasks import ChangeJournal, TaskJournal
from test_e2e_mock_server import answer, config, start_server, tool_call, ws  # noqa: F401  (fixtures)


@pytest.fixture
def journals(tmp_path, monkeypatch):
    tasks, changes = TaskJournal(tmp_path / "j"), ChangeJournal(tmp_path / "j")
    monkeypatch.setattr(handler, "_TASKS", tasks)
    monkeypatch.setattr(handler, "_CHANGES", changes)
    return tasks, changes


def test_a_run_appears_as_a_task_with_its_edits(start_server, ws, tmp_path, journals):  # noqa: F811
    tasks, changes = journals
    url = start_server([tool_call("write_file", path="hello.txt", content="one\ntwo\n"),
                        tool_call("edit_file", path="hello.txt", target="two", replacement="three"), answer("All done.")])
    result = run_headless(config(ws, tmp_path, url, report=True), "make hello.txt")
    assert result["status"] == "success"
    (task,) = tasks.load()["tasks"]
    assert task["id"].startswith("agent-") and task["status"] == "done" and task["result"] == "All done."
    assert task["metadata"]["goal"] == "make hello.txt"
    edits = changes.load()["edits"]
    assert [(e["file"], e["kind"]) for e in edits] == [("hello.txt", "added"), ("hello.txt", "modified")]
    assert "+three" in edits[1]["diff"] and "old_text" not in edits[1]


def test_a_failed_or_cancelled_run_is_not_left_running(start_server, ws, tmp_path, journals):  # noqa: F811
    tasks, _ = journals
    run_headless(config(ws, tmp_path, "http://127.0.0.1:59996", report=True), "go")          # the model is unreachable
    run_headless(config(ws, tmp_path, start_server([answer("x")]), max_turns=1, report=True), "go")
    # nothing about a run may be left in an open state once it ended
    statuses = [t["status"] for t in tasks.load()["tasks"]]
    assert statuses and all(s in ("done", "failed", "closed") for s in statuses), statuses


def test_a_run_with_the_wrong_key_still_reports_nothing_and_still_runs(start_server, ws, tmp_path, journals):  # noqa: F811
    tasks, _ = journals
    url = start_server([tool_call("write_file", path="a.txt", content="x"), answer("ok")], api_key="right")
    result = run_headless(config(ws, tmp_path, url, api_key="right", report=True), "go")
    assert result["status"] == "success" and (ws / "a.txt").exists()
    assert tasks.load()["tasks"][0]["status"] == "done"                                     # the key is sent along


def test_an_unreachable_server_stops_the_reporting_not_the_run(ws):
    class Box:
        changes = []

    r = PanelReporter("http://127.0.0.1:59996", None, Box(), "goal", timeout=0.2)
    r.start()
    r.on_event({"type": "tool_start", "name": "x"})
    assert not r.active and "URLError" in r.error or "Error" in r.error
    r.finish("final", "done")                                                               # no exception, no more attempts


def test_the_goal_and_result_are_kept_short(start_server, ws, tmp_path, journals):  # noqa: F811
    tasks, _ = journals
    url = start_server([answer("y" * 2000)])
    run_headless(config(ws, tmp_path, url, report=True), "g " * 500)
    (task,) = tasks.load()["tasks"]
    assert len(task["result"]) <= 300 and len(task["metadata"]["goal"]) <= 300


def test_the_command_line_reports_unless_told_not_to(start_server, ws, tmp_path, journals, capsys):  # noqa: F811
    tasks, _ = journals
    url = start_server([answer("one"), answer("two")])
    assert main(["-p", "first", "--workspace", str(ws), "--endpoint", url, "--no-memory", "--no-checkpoints"]) == 0
    assert len(tasks.load()["tasks"]) == 1
    assert main(["-p", "second", "--workspace", str(ws), "--endpoint", url, "--no-memory", "--no-checkpoints", "--no-report"]) == 0
    assert len(tasks.load()["tasks"]) == 1


def test_an_edit_the_agent_made_can_be_undone_from_the_monitor(start_server, ws, tmp_path, journals):  # noqa: F811
    _, changes = journals
    original = "".join(f"row {i}\n" for i in range(1, 31))
    (ws / "data.txt").write_bytes(original.encode("utf-8"))
    rewritten = original.replace("row 2\n", "row two\n").replace("row 28\n", "row twenty-eight\n")
    url = start_server([tool_call("write_file", path="data.txt", content=rewritten), answer("Edited.")])
    run_headless(config(ws, tmp_path, url, report=True, permission_mode="auto"), "edit data.txt")
    (edit,) = changes.view()["edits"]
    assert edit["review"] == "pending" and edit["blocks"] == [False, False]
    assert (ws / "data.txt").read_bytes().decode("utf-8") == rewritten
    out = changes.review_action(edit["id"], "reject", 0)                  # undo the first block only
    assert (ws / "data.txt").read_bytes().decode("utf-8") == original.replace("row 28\n", "row twenty-eight\n")
    assert out["blocks"] == [False]
    assert changes.review_action(edit["id"], "keep_all")["review"] == "kept"
