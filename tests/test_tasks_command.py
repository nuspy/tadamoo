"""`/tasks`, answered by the conversation's own process: the open tasks of the project (this conversation's marked), and the person's own task added to this conversation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo import commands
from tadamoo.taskboard import TaskBoard
from test_commands import make
from test_session import Scripted, say
from test_session import _no_clock  # noqa: F401  (autouse fixture)


def test_tasks_lists_the_open_tasks_of_the_project_and_adds_the_persons_own_to_this_conversation(tmp_path):
    session, ev, built = make(tmp_path, Scripted(say("ok")))
    board = TaskBoard(tmp_path / "ws")
    board.add("Rebuild the site", session="s1", missing="the footer", subtasks=[{"title": "Header"}])
    board.add("Someone else's", session="s2")
    out = commands.run("tasks", "", session)
    assert out["ok"] and out["data"]["available"] and out["data"]["count"] == 3
    rows = {r["title"]: r for r in out["data"]["tasks"]}
    assert rows["Rebuild the site"]["mine"] and rows["Rebuild the site"]["missing"] == "the footer" and rows["Header"]["depth"] == 1 and not rows["Someone else's"]["mine"]
    added = commands.run("tasks", "add Write the report", session)["data"]
    task = board.get(added["added"])
    assert task["title"] == "Write the report" and task["by"] == "user" and task["links"]["session"] == "s1"
    assert commands.run("tasks", "add", session)["data"] == {"available": True, "error": "usage"}
    session.agent.tools.task_tools = None
    assert commands.run("tasks", "", session)["data"] == {"available": False}
