"""The agent's side of the register: the task_* tools, and what the conversation is told of it (a short list at the start and after a compaction, one line when someone
else changed it, nothing otherwise) without touching the cached start of the request."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.taskboard import TaskBoard
from tadamoo.tools.registry import STABLE
from test_session import Scripted, make, say  # noqa: F401
from test_session import _no_clock  # noqa: F401  (autouse fixture)

TOOLS = ("task_add", "task_update", "task_subtasks", "task_move", "task_close", "task_list", "task_next", "task_resume", "task_link_plan")


def board_of(tmp_path):
    return TaskBoard(tmp_path / "ws")


def run(built, name, **args):
    return built.agent.tools.get(name).handler(**args)


def test_the_tools_are_in_the_sessions_own_group_after_the_fixed_ones(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted())
    reg = built.agent.tools
    names = [s["function"]["name"] for s in reg.schemas()]
    assert all(n in names for n in TOOLS) and all(reg.get(n).tier != STABLE for n in TOOLS)
    assert names.index("read_file") < min(names.index(n) for n in TOOLS)


def test_the_agent_opens_a_long_job_with_subtasks_changes_them_and_closes_what_is_done(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted())
    out = run(built, "task_add", title="Rebuild the site", description="new theme", subtasks=[{"title": "Header"}, {"title": "Footer", "subtasks": [{"title": "Links"}]}])
    top = out.split()[1]
    board = board_of(tmp_path)
    assert [r["title"] for r in board.view(session="s1")] == ["Rebuild the site", "Header", "Footer", "Links"] and "Footer" in out
    run(built, "task_update", id=top, state="running", done="header drafted", missing="footer and links")
    sub = {r["title"]: r["id"] for r in board.view()}
    run(built, "task_subtasks", id=top, subtasks=[{"id": sub["Header"], "title": "Header", "state": "running"}, {"title": "Footer, another way"}])
    assert [r["title"] for r in board.view()] == ["Rebuild the site", "Header", "Footer, another way"]
    listing = run(built, "task_list")
    assert "done: header drafted" in listing and "missing: footer and links" in listing and "what: new theme" in listing
    assert run(built, "task_next").count("Footer, another way") == 1
    run(built, "task_close", id=top)
    assert board.view() == [] and run(built, "task_list") == "No open tasks in this conversation."


def test_a_mistake_of_the_agent_comes_back_as_words_it_can_act_on(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted())
    with pytest.raises(ValueError) as e:
        run(built, "task_close", id="t-gone")
    assert "task_list" in str(e.value)
    with pytest.raises(ValueError):
        run(built, "task_list", scope="everything")


def test_resume_hands_a_task_of_another_conversation_over_with_its_notes_and_subtasks(tmp_path):
    session, ev, store, built = make(tmp_path, Scripted())
    board = board_of(tmp_path)
    t = board.add("Migrate data", "from old db", session="s-old", state="blocked", done="schema copied", missing="rows 1000+", subtasks=[{"title": "Copy rows"}], files=["db/dump.sql"])
    assert run(built, "task_list") == "No open tasks in this conversation." and "Migrate data" in run(built, "task_list", scope="project")
    out = run(built, "task_resume", id=t)
    assert "now carried by this conversation" in out and "done: schema copied" in out and "missing: rows 1000+" in out and "Copy rows" in out and "db/dump.sql" in out
    assert board.get(t)["links"]["session"] == "s1" and [r["title"] for r in board.view(session="s1")] == ["Migrate data", "Copy rows"]


def test_a_task_is_handed_to_a_plan_and_from_then_on_it_is_that_plans_line(tmp_path, monkeypatch):
    session, ev, store, built = make(tmp_path, Scripted())
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "newscast.json").write_text('{"plan": {"plan_id": "newscast", "title": "Newscast", "nodes": {"a": {"type": "action", "status": "pending", "title": "Research"}}}}', encoding="utf-8")
    t = run(built, "task_add", title="Make the newscast", subtasks=[{"title": "mine"}]).split()[1]
    built.agent.tools.task_tools.board.plans_dir = plans
    assert "one line for the plan newscast" in run(built, "task_link_plan", id=t, plan_id="newscast")
    rows = board_of(tmp_path).__class__(tmp_path / "ws", plans).view()
    assert len(rows) == 1 and rows[0]["plan"]["title"] == "Newscast"
    with pytest.raises(ValueError):
        run(built, "task_link_plan", id=t, plan_id="nothing")


# -- what the conversation is told ------------------------------------------------------------------------------------

def user_texts(client, n):
    return [m["content"] for m in client.sent[n] if m["role"] == "user"]


def test_the_open_tasks_come_as_a_short_list_at_the_start_once_and_never_again_while_nothing_changed(tmp_path):
    client = Scripted(say("Uno."), say("Due."))
    session, ev, store, built = make(tmp_path, client)
    board = board_of(tmp_path)
    board.add("Long job", session="s1", missing="the last chapter", subtasks=[{"title": "Chapter 1"}])
    board.add("Another conversation's", session="s2")
    session.send("m1", "ciao")
    ev.wait("done")
    first = user_texts(client, 0)
    assert first[0].startswith("[harness] Your open tasks") and "Long job" in first[0] and "Chapter 1" in first[0] and "Another conversation" not in first[0] and first[1] == "ciao"
    n = len(ev.items)
    session.send("m2", "ancora")
    ev.wait("done", after=n)
    assert user_texts(client, 1) == first + ["ancora"]                                       # nothing added: the register is as the conversation knows it
    assert client.sent[1][:len(client.sent[0])] == client.sent[0]                             # and the cached start did not move


def test_a_conversation_with_no_open_tasks_is_told_nothing(tmp_path):
    client = Scripted(say("Uno."))
    session, ev, store, built = make(tmp_path, client)
    session.send("m1", "ciao")
    ev.wait("done")
    assert user_texts(client, 0) == ["ciao"]


def test_what_the_person_or_another_conversation_changed_is_one_line_and_the_agents_own_change_is_not_news(tmp_path):
    client = Scripted(say("Uno."), say("Due."), say("Tre."), say("Quattro."))
    session, ev, store, built = make(tmp_path, client)
    board = board_of(tmp_path)
    t = board.add("Long job", session="s1")
    session.send("m1", "a")
    ev.wait("done")
    run(built, "task_update", id=t, state="running")                                          # the agent's own change
    n = len(ev.items)
    session.send("m2", "b")
    ev.wait("done", after=n)
    assert user_texts(client, 1)[-1] == "b" and len(user_texts(client, 1)) == len(user_texts(client, 0)) + 1
    board.update(t, by="user", missing="also the index")                                       # the person, in the Tasks tab
    n = len(ev.items)
    session.send("m3", "c")
    ev.wait("done", after=n)
    third = user_texts(client, 2)
    assert third[-2] == "[harness] Your open tasks were changed outside you: task_list shows them now." and third[-1] == "c"
    board.close(t)
    n = len(ev.items)
    session.send("m4", "d")
    ev.wait("done", after=n)
    assert user_texts(client, 3)[-2] == "[harness] You have no open tasks now."


def test_after_a_compaction_the_list_is_given_again(tmp_path):
    client = Scripted(say("Uno."), say("Due."), say("Tre."))
    session, ev, store, built = make(tmp_path, client)
    board_of(tmp_path).add("Long job", session="s1", done="half")
    session.send("m1", "a")
    ev.wait("done")
    n = len(ev.items)
    session.send("m2", "b")
    ev.wait("done", after=n)
    built.agent.on_compaction()                                                              # what the loop calls when it folded the conversation
    n = len(ev.items)
    session.send("m3", "c")
    ev.wait("done", after=n)
    third = user_texts(client, 2)
    assert third[-2].startswith("[harness] Your open tasks") and "done: half" in third[-2] and third[-1] == "c"


def test_a_new_process_for_the_same_conversation_reads_the_register_again(tmp_path):
    board_of(tmp_path).add("Survives a restart", session="s1")
    for _ in range(2):                                                                        # two processes in a row: the work is in the project folder, not in a conversation
        client = Scripted(say("Uno."))
        session, ev, store, built = make(tmp_path, client)
        session.send("m1", "ciao")
        ev.wait("done")
        assert "Survives a restart" in user_texts(client, 0)[0]
