"""A task teaches the next one: the loop records how a request went, hands the outcome and the lessons to the writer, and shows the matching lessons at the start."""
from test_agent_loop import ScriptedClient, call, make, say

from tadamoo import lessons
from tadamoo.permissions import PermissionLevel, PermissionManager


def agent_for(ws, client):
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    return agent


def test_a_failed_read_followed_by_a_good_one_becomes_a_lesson_and_the_outcome_is_logged(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello", encoding="utf-8")
    client = ScriptedClient(call("read_file", {"path": "note.txt"}, "a"), call("read_file", {"path": "notes.txt"}, "b"), say("Done: it says hello."))
    agent = agent_for(ws, client)
    got = []
    agent.learn = lambda record, candidates: got.append((record, candidates))
    agent.run("read my notes")
    record, candidates = got[0]
    assert record["result"] == "done" and record["steps"] == 3 and record["request"].startswith("read my notes") and record["errors"].get("read_file") == 1
    assert [c["kind"] for c in candidates] == ["error_fix"] and "note.txt" in str(candidates[0]["facts"]) or candidates[0]["facts"]
    store = lessons.LessonStore(ws)
    assert store.add_candidate(candidates[0]).ok
    assert lessons.OutcomeLog(ws).append(record)["id"]


def test_the_lessons_that_match_a_request_are_added_after_it_and_a_request_that_matches_none_gets_nothing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = lessons.LessonStore(ws)
    store.add("reading a notes file with a mistyped name", "read_file failed on note.txt", "list the folder first, then read notes.txt", signature="x")
    seen = []
    for request in ("please read my notes file", "write a poem about the sea"):
        client = ScriptedClient(say("ok"))
        agent = agent_for(ws, client)
        agent.lessons_for = lambda text: lessons.block_for(text, store)
        agent.run(request)
        seen.append([m["content"] for m in client.sent[0] if m.get("role") == "user" and str(m.get("content")).startswith("Lessons from your earlier work")])
    assert len(seen[0]) == 1 and "list the folder first" in seen[0][0] and seen[1] == []


def test_a_request_with_no_learning_attached_runs_as_before(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(say("fine"))
    agent = agent_for(ws, client)
    agent.run("hello")
    assert agent._recorder is None and "lessons" not in agent.mission


def test_the_stack_attaches_the_learning_only_when_it_is_given_a_lessons_folder(tmp_path):
    from tadamoo.stack import AgentConfig, build_agent
    ws = tmp_path / "ws"
    ws.mkdir()
    base = dict(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False)
    off = build_agent(AgentConfig(**base), client=object()).agent
    on = build_agent(AgentConfig(lessons_dir=str(tmp_path / "g"), **base), client=object()).agent
    assert off.learn is None and off.lessons_for is None and on.learn is not None and on.lessons_for is not None


def test_a_failure_brings_the_lesson_about_the_same_failure_with_its_result_once(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = lessons.LessonStore(ws)
    store.add("read_file call failed: Tool execution error: FileNotFoundError: File not found: notes_2.txt", "read_file failed with a FileNotFoundError",
              "The tool succeeded when the path was corrected to notes-2.txt.", signature="error_fix|read_file|filenotfound")
    client = ScriptedClient(call("read_file", {"path": "notes_3.txt"}, "a"), call("read_file", {"path": "notes_4.txt"}, "b"), say("could not find them"))
    agent = agent_for(ws, client)
    agent.lessons_for = lambda text: lessons.block_for(text, store)
    agent.run("open the files")
    results = [m["content"] for m in client.sent[2] if m.get("role") == "tool"]
    assert "corrected to notes-2.txt" in results[0] and "corrected to notes-2.txt" not in results[1]


def test_a_message_that_says_the_last_answer_was_wrong_is_kept_as_a_correction_with_the_words_masked(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(say("The answer is 4."), say("Sorry, 5."), say("Fine."))
    agent = agent_for(ws, client)
    got = []
    agent.learn = lambda record, candidates: got.append(record)
    agent.run("what is 2+2?")
    agent.run("No, that is wrong, try again")
    agent.run("thanks, now something else")
    corrections = [r for r in got if r.get("result") == "corrected"]
    assert len(corrections) == 1 and corrections[0]["corrected_by_person"] and corrections[0]["request"].startswith("what is 2+2")
    assert corrections[0]["correction"].startswith("No, that is wrong")


def test_after_a_fold_the_agent_gets_a_mission_card_with_the_request_the_open_items_and_the_files(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    client = ScriptedClient(call("write_file", {"path": "a.txt", "content": "x"}, "w"), say("done"))
    agent = agent_for(ws, client)
    agent.run("write a.txt")
    note = agent._mission_note()
    assert "Mission card" in note and "write a.txt" in note and "a.txt" in note.split("Files written or edited so far:")[1]
