"""What the programs report to the register (agent runs, a target and its tasks, a large request and its parts): the old statuses map onto states and notes, ending deletes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.taskboard import TaskBoard, TaskError
from test_taskboard import titles


@pytest.fixture
def board(tmp_path):
    return TaskBoard(tmp_path / "project", tmp_path / "plans")


def test_a_report_opens_a_task_under_the_reporters_id_and_maps_the_old_statuses(board):
    row = board.report("agent-ab12", "running", "step 1: write_file", metadata={"goal": "Make hello.txt", "kind": "agent"}, session="s1")
    assert (row["id"], row["title"], row["state"], row["done"], row["links"]["session"], row["meta"]) == ("agent-ab12", "Make hello.txt", "running", "step 1: write_file", "s1", {"kind": "agent"})
    board.report("agent-ab12", "running", "step 2")
    assert board.get("agent-ab12")["done"] == "step 2" and len(board.view()) == 1
    for status, state in (("pending", "queued"), ("in_progress", "running"), ("waiting", "waiting"), ("blocked", "blocked")):
        assert board.report("agent-ab12", status, "x")["state"] == state
    failed = board.report("agent-ab12", "failed", "the model was not reachable")
    assert failed["state"] == "blocked" and failed["missing"] == "the model was not reachable" and failed["done"] == "x"


def test_a_report_that_ends_deletes_the_task_with_its_subtasks_and_ending_what_is_not_there_is_no_error(board):
    board.report("parent", "running", metadata={"title": "A request"}, children=["kid"])
    board.report("kid", "pending", metadata={"title": "A part"})
    assert [(r["id"], r["depth"]) for r in board.view()] == [("parent", 0), ("kid", 1)]
    for ending in ("done", "closed", "completed", "skipped"):
        board.report("kid", "pending", metadata={"title": "A part"})
        assert board.report("kid", ending) is None and [r["id"] for r in board.view()] == ["parent"]
    board.report("kid", "pending", metadata={"title": "A part"})
    board.report("parent", "done")
    assert board.view() == [] and board.report("parent", "done") is None


def test_a_parent_that_names_its_subtasks_is_their_parent_whichever_was_reported_first(board):
    board.report("kid.2", "pending", metadata={"title": "Two"})
    board.report("p", "running", children=["kid.1", "kid.2"], metadata={"title": "P"})
    board.report("kid.1", "pending", metadata={"title": "One"})
    assert titles(board) == ["P", "  Two", "  One"]
    board.report("p", "running", "going")                                  # a later report without children leaves them as they are
    assert board.get("p")["children"] == 2 and board.get("p")["meta"]["children"] == ["kid.1", "kid.2"]


def test_a_report_with_a_bad_id_or_status_is_refused_and_changes_nothing(board):
    for call in (lambda: board.report("bad id!", "running"), lambda: board.report("x" * 100, "running"), lambda: board.report("ok", "nonsense")):
        with pytest.raises(TaskError):
            call()
    assert board.view() == []
