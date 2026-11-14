"""The agent shows its run and its edits in the server's Agentic coding tab."""
import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.headless import main, run_headless
from tadamoo.reporting import PanelReporter
from serve import handler
from serve.changes import ChangeJournal
from tadamoo.taskboard import TaskBoard
from test_e2e_mock_server import answer, config, start_server, tool_call, ws  # noqa: F401  (fixtures)


@pytest.fixture
def journals(tmp_path, monkeypatch):
    """The register of the folder runs work in (a folder nobody added: the data folder's), what the reporter said to it, and the changes journal."""
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))
    changes = ChangeJournal(tmp_path / "j")
    monkeypatch.setattr(handler, "_CHANGES", changes)
    said, real = [], TaskBoard.report

    def report(self, task_id, status, result="", children=None, metadata=None, session="", by="agent"):
        said.append({"id": task_id, "status": status, "result": result, "metadata": metadata or {}})
        return real(self, task_id, status, result, children, metadata, session, by)
    monkeypatch.setattr(TaskBoard, "report", report)
    return TaskBoard(tmp_path / "data" / "journal"), said, changes


def test_a_run_is_a_task_while_it_works_and_its_end_deletes_it_with_its_edits_kept(start_server, ws, tmp_path, journals):  # noqa: F811
    board, said, changes = journals
    url = start_server([tool_call("write_file", path="hello.txt", content="one\ntwo\n"),
                        tool_call("edit_file", path="hello.txt", target="two", replacement="three"), answer("All done.")])
    result = run_headless(config(ws, tmp_path, url, report=True), "make hello.txt")
    assert result["status"] == "success"
    assert said[0]["status"] == "running" and said[0]["id"].startswith("agent-") and said[0]["metadata"]["goal"] == "make hello.txt"
    assert any("step" in s["result"] for s in said[1:-1])                                    # what is done so far
    assert said[-1]["status"] == "done" and said[-1]["result"] == "All done."
    assert board.view() == []                                                                # finished: deleted, nothing is kept
    edits = changes.load()["edits"]
    assert [(e["file"], e["kind"]) for e in edits] == [("hello.txt", "added"), ("hello.txt", "modified")]
    assert "+three" in edits[1]["diff"] and "old_text" not in edits[1]


def test_a_run_in_progress_is_a_running_task_with_its_goal_and_what_it_did(start_server, ws, tmp_path, journals):  # noqa: F811
    board, said, _ = journals
    seen = []
    real = TaskBoard.report

    def spy(self, task_id, status, *a, **k):
        out = real(self, task_id, status, *a, **k)
        seen.append([(r["title"], r["state"], r["done"]) for r in self.view()])
        return out
    TaskBoard.report = spy
    try:
        run_headless(config(ws, tmp_path, start_server([tool_call("write_file", path="a.txt", content="x"), answer("ok")]), report=True), "make a.txt")
    finally:
        TaskBoard.report = real
    assert seen[0] == [("make a.txt", "running", "make a.txt")] and any(r and r[0][2].startswith("make a.txt") and "step 1" in r[0][2] for r in seen) and seen[-1] == []


def test_a_failed_or_cancelled_run_is_not_left_running(start_server, ws, tmp_path, journals):  # noqa: F811
    board, said, _ = journals
    run_headless(config(ws, tmp_path, "http://127.0.0.1:59996", report=True), "go")          # the model is unreachable
    run_headless(config(ws, tmp_path, start_server([answer("x")]), max_turns=1, report=True), "go")
    # nothing about a run may be left running once it ended: it was deleted, or it stays blocked with the reason
    rows = board.view()
    assert all(r["state"] == "blocked" and r["missing"] for r in rows), rows
    assert said[-1]["status"] in ("done", "failed", "closed")


def test_a_run_with_the_wrong_key_still_reports_nothing_and_still_runs(start_server, ws, tmp_path, journals):  # noqa: F811
    board, said, _ = journals
    url = start_server([tool_call("write_file", path="a.txt", content="x"), answer("ok")], api_key="right")
    result = run_headless(config(ws, tmp_path, url, api_key="right", report=True), "go")
    assert result["status"] == "success" and (ws / "a.txt").exists()
    assert said[-1]["status"] == "done"                                                      # the key is sent along


def test_an_unreachable_server_stops_the_reporting_not_the_run(ws):
    class Box:
        changes = []

    r = PanelReporter("http://127.0.0.1:59996", None, Box(), "goal", timeout=0.2)
    r.start()
    r.on_event({"type": "tool_start", "name": "x"})
    assert not r.active and "URLError" in r.error or "Error" in r.error
    r.finish("final", "done")                                                               # no exception, no more attempts


def test_the_goal_and_result_are_kept_short(start_server, ws, tmp_path, journals):  # noqa: F811
    board, said, _ = journals
    url = start_server([answer("y" * 2000)])
    run_headless(config(ws, tmp_path, url, report=True), "g " * 500)
    assert said[-1]["status"] == "done" and len(said[-1]["result"]) <= 300 and len(said[0]["metadata"]["goal"]) <= 300


def test_the_command_line_reports_unless_told_not_to(start_server, ws, tmp_path, journals, capsys):  # noqa: F811
    board, said, _ = journals
    url = start_server([answer("one"), answer("two")])
    assert main(["-p", "first", "--workspace", str(ws), "--endpoint", url, "--no-memory", "--no-checkpoints"]) == 0
    assert sum(1 for s in said if s["status"] == "running") >= 1
    before = len(said)
    assert main(["-p", "second", "--workspace", str(ws), "--endpoint", url, "--no-memory", "--no-checkpoints", "--no-report"]) == 0
    assert len(said) == before


def test_an_edit_the_agent_made_can_be_undone_from_the_monitor(start_server, ws, tmp_path, journals):  # noqa: F811
    _, _, changes = journals
    original = "".join(f"row {i}\n" for i in range(1, 31))
    (ws / "data.txt").write_bytes(original.encode("utf-8"))
    rewritten = original.replace("row 2\n", "row two\n").replace("row 28\n", "row twenty-eight\n")
    url = start_server([tool_call("write_file", path="data.txt", content=rewritten, overwrite="true"), answer("Edited.")])
    run_headless(config(ws, tmp_path, url, report=True, permission_mode="auto"), "edit data.txt")
    (edit,) = changes.view()["edits"]
    assert edit["review"] == "pending" and edit["blocks"] == [False, False]
    assert (ws / "data.txt").read_bytes().decode("utf-8") == rewritten
    out = changes.review_action(edit["id"], "reject", 0)                  # undo the first block only
    assert (ws / "data.txt").read_bytes().decode("utf-8") == original.replace("row 28\n", "row twenty-eight\n")
    assert out["blocks"] == [False]
    assert changes.review_action(edit["id"], "keep_all")["review"] == "kept"
