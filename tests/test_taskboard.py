"""The register of open work: a tree of tasks for each project in the project's own folder, closed means deleted, a plan is one line read from the plan, and several
processes write it without losing each other's changes."""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tadamoo.taskboard import TaskBoard, TaskError, plan_state

ROOT = Path(__file__).resolve().parents[1]


def make_plan(plans: Path, plan_id="newscast", nodes=None):
    nodes = nodes or {"start": {"type": "start", "status": "completed"},
                      "a": {"type": "action", "status": "completed", "assignee": "agent", "title": "Research"},
                      "b": {"type": "action", "status": "in_progress", "assignee": "agent", "title": "Write the script"},
                      "c": {"type": "action", "status": "in_progress", "assignee": "user", "title": "Approve"}}
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{plan_id}.json").write_text(json.dumps({"plan": {"plan_id": plan_id, "title": "Newscast", "nodes": nodes}}), encoding="utf-8")


@pytest.fixture
def board(tmp_path):
    return TaskBoard(tmp_path / "project", tmp_path / "plans")


def titles(board, **kw):
    return [("  " * r["depth"]) + r["title"] for r in board.view(**kw)]


def test_the_register_is_a_tree_in_the_project_folder_with_states_notes_links_and_who_changed_it(board, tmp_path):
    top = board.add("Move the site", "to the new host", session="s1", subtasks=[{"title": "Copy files", "subtasks": [{"title": "Images"}, {"title": "Pages"}]}, {"title": "Change DNS", "state": "waiting",
                                                                                   "missing": "the registrar login"}])
    assert titles(board) == ["Move the site", "  Copy files", "    Images", "    Pages", "  Change DNS"]
    row = board.get(top)
    assert row["state"] == "queued" and row["by"] == "agent" and row["links"]["session"] == "s1" and row["description"] == "to the new host" and row["created"] and row["children"] == 2
    dns = next(r for r in board.view() if r["title"] == "Change DNS")
    assert dns["state"] == "waiting" and dns["missing"] == "the registrar login" and dns["depth"] == 1 and dns["project"] == str(tmp_path / "project")
    board.update(top, by="user", title="Move the website", state="running", done="nothing yet", files=["site/index.html"])
    row = board.get(top)
    assert (row["title"], row["state"], row["by"], row["done"], row["links"]["files"]) == ("Move the website", "running", "user", "nothing yet", ["site/index.html"])
    assert (tmp_path / "project" / ".tadamoo" / "tasks.json").is_file()


def test_bad_input_is_refused_in_words_and_changes_nothing(board):
    t = board.add("A")
    for call in (lambda: board.add(""), lambda: board.add("x", state="done"), lambda: board.update(t, state="finished"), lambda: board.update(t, colour="red"),
                 lambda: board.update("t-nope", title="x"), lambda: board.close("t-nope"), lambda: board.add("x", parent="t-nope")):
        with pytest.raises(TaskError):
            call()
    assert titles(board) == ["A"]


def test_closing_deletes_the_task_with_its_subtasks_and_nothing_of_it_is_kept(board, tmp_path):
    top = board.add("Big job", subtasks=[{"title": "Step one"}, {"title": "Step two"}])
    other = board.add("Other")
    gone = board.close(top)
    assert len(gone) == 3 and titles(board) == ["Other"]
    text = (tmp_path / "project" / ".tadamoo" / "tasks.json").read_text(encoding="utf-8")
    assert "Big job" not in text and "Step one" not in text and board.get(other)["title"] == "Other"
    board.close(other)
    assert board.view() == [] and json.loads((tmp_path / "project" / ".tadamoo" / "tasks.json").read_text(encoding="utf-8"))["tasks"] == []


def test_the_subtasks_are_rewritten_freely_keeping_what_is_named_and_closing_what_is_left_out(board):
    top = board.add("Job", subtasks=[{"title": "One"}, {"title": "Two", "subtasks": [{"title": "Two-a"}]}, {"title": "Three"}])
    one, two, three = [r["id"] for r in board.view() if r["depth"] == 1]
    ids = board.set_subtasks(top, [{"id": three, "title": "Three, done another way", "state": "running"}, {"title": "Brand new"}, {"id": one, "title": "One"}])
    assert titles(board) == ["Job", "  Three, done another way", "  Brand new", "  One"] and ids[0] == three and ids[2] == one
    assert two not in {r["id"] for r in board.view()} and "Two-a" not in titles(board)           # what was left out is deleted with what hung from it
    assert board.get(three)["state"] == "running"
    board.set_subtasks(top, [])
    assert titles(board) == ["Job"]


def test_reordering_and_moving_under_another_task_and_a_task_cannot_go_inside_itself(board):
    a, b, c = board.add("A"), board.add("B"), board.add("C")
    board.move(c, before=a)
    assert titles(board) == ["C", "A", "B"]
    board.move(a)                                             # to the end of its own family
    assert titles(board) == ["C", "B", "A"]
    board.move(b, parent=a)
    assert titles(board) == ["C", "A", "  B"]
    with pytest.raises(TaskError):
        board.move(a, parent=b)
    board.move(b, parent="")
    assert titles(board) == ["C", "A", "B"]
    deep = board.add("Deep")
    for _ in range(10):
        deep = board.add("level", parent=deep)
    with pytest.raises(TaskError):
        board.add("one too deep", parent=deep)


def test_a_conversation_sees_only_its_own_tasks_and_another_one_can_take_a_task_over(board):
    mine = board.add("Mine", session="s1", subtasks=[{"title": "Mine-sub"}])
    board.add("Theirs", session="s2")
    board.add("Nobody's by the person", by="user")
    assert titles(board, session="s1") == ["Mine", "  Mine-sub"] and titles(board, session="s2") == ["Theirs"]
    assert [r["title"] for r in board.view() if r["depth"] == 0] == ["Mine", "Theirs", "Nobody's by the person"]
    assert board.view(state="running") == []
    board.take(mine, "s2")
    assert titles(board, session="s2") == ["Theirs", "Mine", "  Mine-sub"] or sorted(titles(board, session="s2")) == sorted(["Theirs", "Mine", "  Mine-sub"])
    assert board.get(mine)["state"] == "running" and board.get(mine)["links"]["session"] == "s2" and titles(board, session="s1") == []


def test_the_next_task_is_the_first_queued_leaf_of_the_conversation_in_order(board):
    board.add("Job", session="s1", subtasks=[{"title": "One", "state": "running"}, {"title": "Two"}, {"title": "Three"}])
    assert board.next_task("s1")["title"] == "Two" and board.next_task("s9") is None


def test_the_summary_is_short_stable_and_only_the_conversations_own(board):
    for i in range(15):
        board.add(f"Task {i}", session="s1", missing="a long note " * 30)
    board.add("Not mine", session="s2")
    text = board.summary("s1")
    assert text == board.summary("s1") and "Not mine" not in text and text.count("\n") == 12 and text.endswith("- and 3 more: task_list shows all of them")
    assert all(len(line) < 220 for line in text.splitlines()) and board.summary("s3") == ""


def test_the_signature_changes_when_a_task_is_changed_and_not_with_what_a_plan_reports(board, tmp_path):
    t = board.add("Job", session="s1")
    before = board.signature("s1")
    assert before and board.signature("s2") == ""
    board.update(t, missing="the key")
    assert board.signature("s1") != before
    make_plan(tmp_path / "plans")
    board.link_plan(t, "newscast")
    linked = board.signature("s1")
    make_plan(tmp_path / "plans", nodes={"a": {"type": "action", "status": "completed"}, "b": {"type": "action", "status": "completed"}, "c": {"type": "action", "status": "in_progress", "assignee": "agent"}})
    assert board.signature("s1") == linked


# -- plans ------------------------------------------------------------------------------------------------------------

def test_a_plan_is_read_for_its_progress_and_what_waits_for_the_person(tmp_path):
    make_plan(tmp_path)
    p = plan_state(tmp_path, "newscast")
    assert p == {"title": "Newscast", "state": "waiting", "done": 1, "total": 3, "next": ["Write the script", "Approve"]}
    assert plan_state(tmp_path, "nothing") is None
    make_plan(tmp_path, nodes={"a": {"type": "action", "status": "failed"}, "b": {"type": "action", "status": "pending"}})
    assert plan_state(tmp_path, "newscast")["state"] == "blocked"
    make_plan(tmp_path, nodes={"a": {"type": "action", "status": "completed"}, "b": {"type": "action", "status": "skipped"}})
    assert plan_state(tmp_path, "newscast") is None                                           # finished


def test_a_task_linked_to_a_plan_is_one_line_whose_state_comes_from_the_plan_and_the_nodes_are_never_copied(board, tmp_path):
    make_plan(tmp_path / "plans")
    t = board.add("Make the newscast", session="s1", subtasks=[{"title": "Own subtask"}])
    board.link_plan(t, "newscast")
    rows = board.view()
    assert len(rows) == 1 and rows[0]["state"] == "waiting" and rows[0]["done"] == "1/3 steps" and rows[0]["plan"]["title"] == "Newscast" and rows[0]["children"] == 0
    assert 'Plan "Newscast" (newscast)' in board.summary("s1") and "Write the script" in rows[0]["missing"]
    stored = (tmp_path / "project" / ".tadamoo" / "tasks.json").read_text(encoding="utf-8")
    assert "Write the script" not in stored and "Approve" not in stored and "Research" not in stored                       # nothing of the plan is copied
    with pytest.raises(TaskError):
        board.add("a subtask of it", parent=t)
    with pytest.raises(TaskError):
        board.set_subtasks(t, [{"title": "x"}])
    with pytest.raises(TaskError):
        board.link_plan(board.add("Other"), "no-such-plan")


def test_a_plan_that_finishes_takes_its_line_with_it(board, tmp_path):
    make_plan(tmp_path / "plans")
    t = board.add("Make the newscast", session="s1")
    board.link_plan(t, "newscast")
    keep = board.add("Free work", session="s1")
    make_plan(tmp_path / "plans", nodes={"a": {"type": "action", "status": "completed"}})
    assert [r["id"] for r in board.view()] == [keep]
    assert board.summary("s1").count("Plan") == 0


# -- the file ---------------------------------------------------------------------------------------------------------

def test_a_damaged_file_is_an_empty_register_and_a_task_whose_parent_is_gone_goes_to_the_top(board, tmp_path):
    f = tmp_path / "project" / ".tadamoo" / "tasks.json"
    f.parent.mkdir(parents=True)
    f.write_text("{not json", encoding="utf-8")
    assert board.view() == []
    f.write_text(json.dumps({"tasks": [{"id": "t-1", "title": "Orphan", "parent": "t-gone"}, {"id": "t-2", "title": "Fine", "state": "weird"}, {"nope": 1}]}), encoding="utf-8")
    assert [(r["title"], r["depth"], r["state"]) for r in board.view()] == [("Orphan", 0, "queued"), ("Fine", 0, "queued")]


def test_threads_and_processes_write_the_register_without_losing_each_other(board, tmp_path):
    def add_many(tag):
        for i in range(10):
            board.add(f"{tag}-{i}")
    threads = [threading.Thread(target=add_many, args=(f"th{n}",)) for n in range(3)]
    for t in threads:
        t.start()
    code = "import sys; from tadamoo.taskboard import TaskBoard; b = TaskBoard(sys.argv[1]); [b.add(f'{sys.argv[2]}-{i}') for i in range(10)]"
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "project"), f"pr{n}"], cwd=str(ROOT)) for n in range(2)]
    for t in threads:
        t.join()
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    found = {r["title"] for r in board.view()}
    assert len(found) == 50 and {f"th{n}-{i}" for n in range(3) for i in range(10)} <= found and {f"pr{n}-{i}" for n in range(2) for i in range(10)} <= found


def test_the_shell_lists_adds_updates_and_closes(tmp_path, capsys):
    from tadamoo.taskboard import main
    project = str(tmp_path / "proj")
    assert main(["--project", project, "list"]) == 0 and "No open tasks." in capsys.readouterr().out
    assert main(["--project", project, "add", "Ship it"]) == 0
    task_id = capsys.readouterr().out.strip()
    assert main(["--project", project, "update", task_id, "--state", "running", "--missing", "the tests"]) == 0
    capsys.readouterr()
    assert main(["--project", project, "list"]) == 0
    listed = capsys.readouterr().out
    assert "Ship it" in listed and "[running]" in listed and "missing: the tests" in listed
    assert main(["--project", project, "update", task_id, "--state", "finished"]) == 1
    assert main(["--project", project, "close", task_id]) == 0 and main(["--project", project, "close", task_id]) == 1
