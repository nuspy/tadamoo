import pytest
from pathlib import Path
from serve.tasks import TaskJournal


def test_task_journal_create_and_load(tmp_path: Path):
    journal = TaskJournal(tmp_path)
    data = journal.load()
    assert data["tasks"] == []

    task = journal.record("task-1", status="running", metadata={"goal": "test"})
    assert task["id"] == "task-1"
    assert task["status"] == "running"
    assert task["metadata"]["goal"] == "test"

    loaded = journal.load()
    assert len(loaded["tasks"]) == 1
    assert loaded["tasks"][0]["id"] == "task-1"


def test_task_journal_update(tmp_path: Path):
    journal = TaskJournal(tmp_path)
    journal.record("t-abc", status="running")
    journal.record("t-abc", status="done", result="computed 42")

    loaded = journal.load()
    assert len(loaded["tasks"]) == 1
    assert loaded["tasks"][0]["status"] == "done"
    assert loaded["tasks"][0]["result"] == "computed 42"


def test_task_journal_close_and_purge(tmp_path: Path):
    journal = TaskJournal(tmp_path)
    # t1 is running and depends on t2
    journal.record("t2", status="done", result="intermediate")
    journal.record("t1", status="running", children=["t2"])

    # Closing t2 while t1 is open will keep t2 because t1 lists it as child
    journal.close("t2")
    assert len(journal.load()["tasks"]) == 2

    # If t3 has no dependents, closing it purges it immediately
    journal.record("t3", status="done")
    assert len(journal.load()["tasks"]) == 3
    journal.close("t3")
    remaining = [t["id"] for t in journal.load()["tasks"]]
    assert "t3" not in remaining
