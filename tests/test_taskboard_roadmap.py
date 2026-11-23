"""The register as the map of the project's development: closed tasks go to a history and can be reopened, deleting removes for good, a node can be hidden and shown,
tasks belong to groups with their own progress, three notes and two more states, and what a program reports is still simply deleted when it ends."""
import json

import pytest

from tadamoo.taskboard import TaskBoard, TaskError


@pytest.fixture
def board(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    return TaskBoard(folder)


def titles(rows):
    return ["  " * r.get("depth", 0) + r["title"] for r in rows]


def test_an_old_file_without_groups_notes_or_history_is_read_as_it_was(board):
    board.path.parent.mkdir(parents=True)
    board.path.write_text(json.dumps({"schema": "1.0", "tasks": [{"id": "t-1", "title": "Old one", "state": "running", "done": "half", "missing": "half"}]}), encoding="utf-8")
    row = board.view()[0]
    assert row["title"] == "Old one" and row["group"] == "" and row["hidden"] is False and row["notes"] == {"solved": "", "decisions": "", "risks": ""}
    assert board.groups() == [] and board.history() == []
    board.update("t-1", "user", title="Renamed")
    doc = json.loads(board.path.read_text(encoding="utf-8"))
    assert doc["schema"] == "1.1" and doc["groups"] == [] and doc["history"] == [] and doc["tasks"][0]["title"] == "Renamed"


def test_the_two_development_states_are_taken_by_the_person_and_the_agents_own_states_stay(board):
    a = board.add("To commit", state="review", by="user")
    b = board.add("To decide", state="decision", by="user")
    assert [board.get(a)["state"], board.get(b)["state"]] == ["review", "decision"]
    board.update(b, "user", state="blocked")
    assert board.get(b)["state"] == "blocked"
    with pytest.raises(TaskError):
        board.update(a, "user", state="finished")
    with pytest.raises(TaskError):
        board.add("x", state="nope")


def test_the_three_notes_are_kept_and_every_task_made_by_a_person_takes_the_next_number(board):
    one = board.add("One", by="user", notes={"solved": "a\nb", "decisions": "c", "risks": "d"})
    two = board.add("Two", by="user")
    sub = board.add("Sub", parent=two)
    assert [board.get(i)["num"] for i in (one, two, sub)] == [1, 2, 0]
    assert board.get(one)["notes"] == {"solved": "a\nb", "decisions": "c", "risks": "d"}
    board.update(two, "user", solved="done well", risks="may break")
    assert board.get(two)["notes"] == {"solved": "done well", "decisions": "", "risks": "may break"}
    board.close(one)
    assert board.get(two)["num"] == 2 and board.get(board.add("Three", by="user"))["num"] == 3          # numbers are not reused when a task is closed
    assert board.history()[0]["num"] == 1


def test_closing_keeps_the_task_with_its_notes_and_the_date_and_a_programs_own_task_is_deleted(board):
    mine = board.add("Write the chapter", by="user", notes={"solved": "outline done"}, subtasks=[{"title": "Draft"}])
    board.report("agent-r1", "running", metadata={"title": "A run"})
    board.report("req-t1", "running", metadata={"title": "A part"})
    board.report("tg-x-1", "running", metadata={"title": "A target task"})
    board.report("agent-r1", "done")
    board.report("req-t1", "done")
    board.report("tg-x-1", "completed")
    assert board.history() == []                                                  # nothing of what a program reported is kept
    assert board.close(mine, "user") and titles(board.view()) == []
    rows = {r["title"]: r for r in board.history()}
    assert set(rows) == {"Write the chapter", "Draft"} and rows["Write the chapter"]["notes"]["solved"] == "outline done" and rows["Write the chapter"]["closed_by"] == "user"
    assert rows["Write the chapter"]["closed"] > 0 and rows["Draft"]["parent"] == mine


def test_a_closed_task_can_be_reopened_with_its_subtasks_and_a_row_can_be_deleted_for_good(board):
    top = board.add("Job", subtasks=[{"title": "One"}, {"title": "Two"}])
    board.close(top)
    assert titles(board.view()) == []
    board.reopen(top)
    assert titles(board.view()) == ["Job", "  One", "  Two"] and board.history() == [] and board.get(top)["state"] == "queued"
    board.close(top)
    board.delete(top)
    assert board.history() == [] and board.view() == []
    with pytest.raises(TaskError):
        board.delete(top)
    with pytest.raises(TaskError):
        board.reopen("t-nope")


def test_the_history_is_the_newest_first_limited_and_asked_by_conversation(board):
    ids = [board.add(f"Task {i}", session="s1" if i % 2 else "s2") for i in range(6)]
    for i in ids:
        board.close(i)
    assert len(board.history()) == 6 and [r["title"] for r in board.history(limit=2)] == ["Task 5", "Task 4"]
    assert {r["title"] for r in board.history(session="s1")} == {"Task 1", "Task 3", "Task 5"}


def test_a_hidden_node_leaves_the_persons_view_with_what_hangs_from_it_and_stays_for_the_agent(board):
    top = board.add("Parent", session="s1", subtasks=[{"title": "Child"}])
    other = board.add("Other", session="s1")
    board.hide(top)
    assert titles(board.view(hidden=False)) == ["Other"]
    everything = {r["title"]: r for r in board.view()}
    assert everything["Parent"]["hidden"] and not everything["Parent"]["hidden_by_parent"] and everything["Child"]["hidden"] and everything["Child"]["hidden_by_parent"]
    assert "Parent" in board.summary("s1") and board.next_task("s1")["title"] in ("Child", "Parent", "Other")        # the agent still sees it
    board.hide(top, False)
    assert titles(board.view(hidden=False)) == ["Parent", "  Child", "Other"] and other
    board.update(top, "user", hidden=True)
    assert board.get(top)["hidden"] is True


def test_groups_hold_tasks_open_and_closed_and_tell_their_progress_and_a_deleted_group_frees_them(board):
    g1, g2 = board.add_group("The engine"), board.add_group("Android")
    a, b, c = board.add("A", group=g1), board.add("B", group=g1), board.add("C", group=g2)
    board.close(a)
    assert board.groups() == [{"id": g1, "title": "The engine", "order": 1, "open": 1, "closed": 1}, {"id": g2, "title": "Android", "order": 2, "open": 1, "closed": 0}]
    board.rename_group(g2, "The phone")
    board.update(c, "user", group=g1)
    assert [g["open"] for g in board.groups()] == [2, 0] and board.groups()[1]["title"] == "The phone"
    board.delete_group(g1)
    assert board.get(b)["group"] == "" and board.history()[0]["group"] == "" and [g["id"] for g in board.groups()] == [g2]
    with pytest.raises(TaskError):
        board.add("x", group="g-nope")
    with pytest.raises(TaskError):
        board.update(b, "user", group="g-nope")
    with pytest.raises(TaskError):
        board.add_group("  ")
    with pytest.raises(TaskError):
        board.delete_group("g-nope")


def test_the_history_keeps_at_most_five_hundred_rows(board, monkeypatch):
    monkeypatch.setattr("tadamoo.taskboard.MAX_HISTORY", 5)
    for i in range(8):
        board.close(board.add(f"T{i}"))
    assert [r["title"] for r in board.history()] == ["T7", "T6", "T5", "T4", "T3"]


def test_the_shell_closes_deletes_and_lists_the_history(tmp_path, capsys):
    from tadamoo.taskboard import main
    project = str(tmp_path / "p")
    (tmp_path / "p").mkdir()
    main(["--project", project, "add", "Shell task"])
    task_id = capsys.readouterr().out.strip()
    assert main(["--project", project, "close", task_id]) == 0
    capsys.readouterr()
    assert main(["--project", project, "history"]) == 0 and "Shell task" in capsys.readouterr().out
    assert main(["--project", project, "delete", task_id]) == 0 and main(["--project", project, "delete", task_id]) == 1
