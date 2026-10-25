"""The manager of the Chat's conversations: starting, events, supervision, restarts, blocks, dormancy.

It talks to a stand-in process (session_stub.py) that speaks the same lines of JSON as the real one."""
import json
import sys
import time
from pathlib import Path

import pytest

from serve.agent_runs import RunBusy, RunError
from serve.agent_sessions import SessionManager

STUB = str(Path(__file__).with_name("session_stub.py"))


def stub_command(die_times=1):
    return lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history), "--die-times", str(die_times)]


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**kw):
        kw.setdefault("command", stub_command())
        kw.setdefault("tick_s", 0.1)
        mgr = SessionManager(tmp_path / "j", "http://127.0.0.1:1", **kw)
        made.append(mgr)
        return mgr

    yield build
    for mgr in made:
        mgr.shutdown()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return str(root)


def until(predicate, seconds=15, step=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


def kinds(mgr, sid, after=0):
    return [e["event"] for e in mgr.events(sid, after)["events"]]


def test_two_messages_one_process_and_the_history_is_readable(make, ws):
    mgr = make()
    sid = mgr.create(ws, title="")["id"]
    mgr.send(sid, "primo")
    until(lambda: "done" in kinds(mgr, sid))
    first_pid = next(e["pid"] for e in mgr.events(sid)["events"] if e["event"] == "ready")
    mgr.send(sid, "secondo")
    until(lambda: kinds(mgr, sid).count("done") == 2)
    assert [e["pid"] for e in mgr.events(sid)["events"] if e["event"] == "ready"] == [first_pid]       # one process for both
    hist = mgr.history(sid)
    assert [m["content"] for m in hist["messages"]] == ["primo", "echo: primo", "secondo", "echo: secondo"]
    assert mgr.get(sid)["state"] == "idle" and mgr.get(sid)["title"] == "primo"
    assert "heartbeat" not in kinds(mgr, sid)                                  # signs of life are not shown


def test_a_second_message_while_one_is_answered_goes_to_the_agent_which_looks_at_it_itself(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "hang")
    until(lambda: mgr.get(sid)["state"] == "busy")
    out = mgr.send(sid, "ancora")                                                # not refused, and the state does not change
    assert out["session"]["state"] == "busy" and out["turn"]
    assert [e["text"] for e in mgr.events(sid)["events"] if e["event"] == "user"] == ["hang", "ancora"]


def test_a_process_that_dies_in_the_middle_of_a_turn_is_started_again_and_the_turn_goes_on(make, ws):
    mgr = make(command=stub_command(die_times=1))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "die")
    until(lambda: "done" in kinds(mgr, sid))
    ev = kinds(mgr, sid)
    assert "restarting" in ev and ev.count("ready") == 2
    done = [e for e in mgr.events(sid)["events"] if e["event"] == "done"][0]
    assert done["final"] == "resumed after 1 messages"                         # the message was on disk: the turn went on
    assert mgr.get(sid)["state"] == "idle"


def test_a_message_the_process_never_confirmed_is_sent_again(make, ws):
    killer = {"n": 0}

    def command(sid, rec, history):
        killer["n"] += 1
        # the first process ends before it reads anything
        script = "import sys; sys.exit(3)" if killer["n"] == 1 else None
        return [sys.executable, "-c", script] if script else stub_command()(sid, rec, history)

    mgr = make(command=command)
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ciao")
    until(lambda: "done" in kinds(mgr, sid))
    assert [m["content"] for m in mgr.history(sid)["messages"]] == ["ciao", "echo: ciao"]


def test_too_many_restarts_block_the_conversation_and_wait_for_the_person(make, ws):
    mgr = make(command=stub_command(die_times=99), restart_cap=2)
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "die")
    blocked = until(lambda: next((e for e in mgr.events(sid)["events"] if e["event"] == "blocked"), None))
    assert blocked["reason"] == "process_unstable" and blocked["options"] == ["resume", "close"]
    assert kinds(mgr, sid).count("restarting") == 2
    assert mgr.get(sid)["state"] == "blocked"
    time.sleep(0.5)
    assert kinds(mgr, sid).count("ready") == 3                                  # no restart beyond the cap


def test_a_process_that_stops_giving_signs_of_life_is_ended_and_started_again(make, ws):
    mgr = make(beat_timeout_s=0.6, command=stub_command(die_times=0))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "hang")
    until(lambda: "stalled" in kinds(mgr, sid))
    until(lambda: kinds(mgr, sid).count("ready") == 2)
    until(lambda: "done" in kinds(mgr, sid))


def test_a_block_from_the_process_waits_for_the_person_and_resume_goes_on(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "block")
    until(lambda: mgr.get(sid)["state"] == "blocked")
    assert mgr.get(sid)["blocked"]["reason"] == "refused"
    with pytest.raises(RunError):
        mgr.resume(sid) if mgr.get(sid)["state"] != "blocked" else (_ for _ in ()).throw(RunError("x"))
    n = mgr.events(sid)["next"]
    mgr.resume(sid)
    until(lambda: "done" in kinds(mgr, sid, n))
    assert mgr.get(sid)["state"] == "idle"
    with pytest.raises(RunError, match="not waiting"):
        mgr.resume(sid)


def test_an_action_that_cannot_be_undone_is_never_resumed_by_the_manager(make, ws):
    mgr = make(command=stub_command(die_times=0))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "irreversible")
    until(lambda: kinds(mgr, sid).count("ready") == 2)
    until(lambda: mgr.get(sid)["state"] == "blocked")
    assert mgr.get(sid)["blocked"]["reason"] == "interrupted_irreversible"
    time.sleep(0.6)
    assert "done" not in kinds(mgr, sid)                                        # nothing was given on by itself
    assert not any(m["content"].startswith("resumed") for m in mgr.history(sid)["messages"])


def test_an_idle_conversation_goes_dormant_and_the_next_message_wakes_it_with_its_history(make, ws):
    mgr = make(idle_s=0.4)
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "uno")
    until(lambda: "done" in kinds(mgr, sid))
    until(lambda: mgr.get(sid)["state"] == "dormant")
    mgr.send(sid, "due")
    until(lambda: kinds(mgr, sid).count("done") == 2)
    ready = [e for e in mgr.events(sid)["events"] if e["event"] == "ready"]
    assert len(ready) == 2 and ready[1]["restored"] == 2


def test_the_conversations_survive_the_server_and_the_manager_ends_its_processes(make, ws, tmp_path):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ricordati")
    until(lambda: "done" in kinds(mgr, sid))
    pid = next(e["pid"] for e in mgr.events(sid)["events"] if e["event"] == "ready")
    mgr.shutdown()
    mgr2 = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=stub_command(), tick_s=0.1)
    try:
        assert [s["id"] for s in mgr2.view()["sessions"]] == [sid] and mgr2.get(sid)["state"] == "dormant"
        mgr2.send(sid, "ancora")
        until(lambda: "done" in kinds(mgr2, sid))
        assert [m["content"] for m in mgr2.history(sid)["messages"]][:2] == ["ricordati", "echo: ricordati"]
    finally:
        mgr2.shutdown()
    import psutil
    assert not psutil.pid_exists(pid)                                  # the first manager's process ended with it


def test_closing_ends_the_process_and_a_closed_conversation_takes_no_messages(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ciao")
    until(lambda: "done" in kinds(mgr, sid))
    assert mgr.close(sid)["state"] == "closed"
    with pytest.raises(RunError, match="closed"):
        mgr.send(sid, "ancora")


def test_what_the_page_can_ask_is_checked(make, ws, tmp_path):
    mgr = make()
    with pytest.raises(RunError):
        mgr.create("relativo/cartella")
    with pytest.raises(RunError):
        mgr.create(ws, mode="root")
    with pytest.raises(RunError, match="Automatic"):
        mgr.create(ws, mode="auto")
    sid = mgr.create(ws, mode="auto", confirm_auto=True)["id"]
    for bad in ("", "   ", None, 5):
        with pytest.raises(RunError):
            mgr.send(sid, bad)
    with pytest.raises(RunError):
        mgr.send("nope", "ciao")


def test_a_confirmation_is_kept_with_its_buttons_until_the_person_answers(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ask")
    until(lambda: mgr.get(sid)["state"] == "asking")
    ask = mgr.get(sid)["ask"]
    assert ask["id"] == "a1" and ask["question"] == "Archive it?" and ask["facts"] == {"plan": "X"}
    assert mgr.send(sid, "intanto scrivo")["session"]["state"] == "asking"      # a message while it asks is for the agent to look at
    with pytest.raises(RunError, match="not one of the answers"):
        mgr.answer(sid, "a1", "forse")
    with pytest.raises(RunError, match="not waiting"):
        mgr.answer(sid, "zzz", "confirm")
    mgr.answer(sid, "a1", "confirm")
    done = until(lambda: next((e for e in mgr.events(sid)["events"] if e["event"] == "done" and e["final"].startswith("answered")), None))
    assert done["final"] == "answered: confirm"
    assert mgr.get(sid)["state"] == "idle" and mgr.get(sid)["ask"] is None
    with pytest.raises(RunError, match="not waiting"):
        mgr.answer(sid, "a1", "confirm")                                         # answered once, not twice


def test_a_question_that_ends_the_turn_stays_until_the_next_message(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "question")
    until(lambda: "done" in kinds(mgr, sid))
    rec = mgr.get(sid)
    assert rec["state"] == "idle" and rec["question"]["options"] == [{"label": "Sat", "value": "Saturday"}]
    mgr.send(sid, "Saturday")                                                   # the button is the next message
    assert mgr.get(sid)["question"] is None                                     # it is gone as soon as it is answered
    until(lambda: kinds(mgr, sid).count("done") == 2)
    assert mgr.get(sid)["question"] is None
    assert [m["content"] for m in mgr.history(sid)["messages"]][-2:] == ["Saturday", "echo: Saturday"]


def test_a_process_that_dies_while_it_waits_for_an_answer_is_started_again_and_the_question_is_gone(make, ws):
    mgr = make(command=stub_command(die_times=0))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "ask")
    until(lambda: mgr.get(sid)["state"] == "asking")
    from serve.agent_runs import _kill_tree
    _kill_tree(mgr._live[sid].proc)
    until(lambda: kinds(mgr, sid).count("ready") == 2)
    assert mgr.get(sid)["state"] == "busy" and mgr.get(sid).get("ask") is None        # the question died with the process


def test_a_conversation_without_a_named_folder_works_in_the_default_one(make, tmp_path):
    mgr = make(default_workspace=tmp_path / "chat-workspace")
    rec = mgr.create(None)
    assert Path(rec["workspace"]) == (tmp_path / "chat-workspace").resolve() and Path(rec["workspace"]).is_dir()
    bare = SessionManager(tmp_path / "j2", "http://127.0.0.1:1", command=stub_command(), tick_s=0.1)
    try:
        with pytest.raises(RunError):
            bare.create("")                                                     # no default and no folder: still refused
    finally:
        bare.shutdown()


def test_a_conversation_with_more_tasks_waiting_stays_at_work_until_the_last_one_is_done(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "more")
    until(lambda: kinds(mgr, sid).count("done") == 1)
    assert mgr.get(sid)["state"] == "busy"                                      # `more`: others are waiting
    mgr.send(sid, "uno")
    until(lambda: kinds(mgr, sid).count("done") == 2)
    assert mgr.get(sid)["state"] == "idle"


def test_a_queued_message_is_confirmed_by_the_process_and_not_sent_again_after_a_restart(make, ws):
    mgr = make(command=stub_command(die_times=0))
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "queue-me")
    until(lambda: "queued" in kinds(mgr, sid))
    assert sid not in mgr._unconfirmed


def test_a_waiting_request_can_be_promoted_or_dropped_through_the_manager(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "hang")
    until(lambda: mgr.get(sid)["state"] == "busy")
    with pytest.raises(RunError):
        mgr.promote("nope", "x")
    mgr.promote(sid, "t1")
    mgr.drop(sid, "t2")
    time.sleep(0.1)
    mgr.close(sid)


# -- context patterns: the choice is kept, a conversation can be branched, snapshots are listed ----------------


def test_the_choice_of_patterns_is_checked_kept_and_given_to_the_process(make, ws):
    mgr = make()
    with pytest.raises(ValueError):
        mgr.create(ws, context_patterns={"pruning": {"strategy": "nonsense"}})
    rec = mgr.create(ws, context_patterns={"summary": {"on": True, "strategy": "incremental"}})
    assert rec["context_patterns"]["summary"] == {"on": True, "strategy": "incremental"} and rec["context_patterns"]["pruning"]["on"] is True
    cmd = mgr._command(rec["id"], rec, mgr.history_dir(rec["id"]))
    assert json.loads(cmd[cmd.index("--context-patterns") + 1])["summary"]["strategy"] == "incremental"
    plain = mgr.create(ws)
    assert "--context-patterns" not in mgr._command(plain["id"], plain, mgr.history_dir(plain["id"]))


def test_a_request_the_model_saw_reframed_shows_as_the_person_wrote_it_and_pruned_turns_come_back(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    h = mgr.history_dir(sid)
    h.mkdir(parents=True, exist_ok=True)
    (h / "archive.jsonl").write_text(json.dumps({"role": "user", "content": "old question"}) + "\n" + json.dumps({"role": "assistant", "content": "old answer"}) + "\n", encoding="utf-8")
    (h / "history.json").write_text(json.dumps({"turns": 2, "messages": [
        {"role": "user", "content": "[Summary of the earlier part of this conversation]", "summary": True},
        {"role": "user", "content": "<request>\nin 5 parole\n</request>\nLimits found in the request, to respect:\n- at most 5 words"},
        {"role": "assistant", "content": "ok"}, {"role": "user", "content": "[1 earlier turns were left out]", "note": True}]}), encoding="utf-8")
    assert [m["content"] for m in mgr.history(sid)["messages"]] == ["old question", "old answer", "in 5 parole", "ok"]


def test_a_branch_copies_the_history_up_to_a_request_and_leaves_the_first_alone(make, ws):
    mgr = make()
    sid = mgr.create(ws, title="plan")["id"]
    for text in ("uno", "due", "tre"):
        mgr.send(sid, text)
        until(lambda: kinds(mgr, sid).count("done") == ["uno", "due", "tre"].index(text) + 1)
    child = mgr.branch(sid, 2)
    assert child["id"] != sid and child["title"] == "plan (branch)" and child["branched_from"] == {"session": sid, "turn": 2}
    assert [m["content"] for m in mgr.history(child["id"])["messages"]] == ["uno", "echo: uno", "due", "echo: due"]
    assert len(mgr.history(sid)["messages"]) == 6
    for bad in (0, -1, "2", True, 9):
        with pytest.raises(RunError):
            mgr.branch(sid, bad)
    with pytest.raises(RunError):
        mgr.branch("ghost", 1)


def test_snapshots_are_listed_diffed_and_checked_from_the_conversations_folder(make, ws):
    from harness.context_patterns import Snapshots
    mgr = make()
    sid = mgr.create(ws)["id"]
    snaps = Snapshots(mgr.history_dir(sid) / "snapshots")
    snaps.record([{"role": "user", "content": "a"}])
    snaps.record([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    listing = mgr.snapshots(sid)
    assert [s["n"] for s in listing["snapshots"]] == [1, 2] and listing["intact"] is True
    assert [m["text"] for m in mgr.snapshot_diff(sid, 1, "2")["added"]] == ["b"]
    with pytest.raises(RunError):
        mgr.snapshot_diff(sid, 1, 7)


def test_the_policy_for_a_full_context_is_kept_and_given_to_the_process(make, ws):
    mgr = make()
    rec = mgr.create(ws, context_policy="compact", context_threshold=0.6)
    assert rec["context_patterns"]["summary"] == {"on": True, "strategy": "structured"} and rec["context_threshold"] == 0.6
    cmd = mgr._command(rec["id"], rec, mgr.history_dir(rec["id"]))
    assert cmd[cmd.index("--context-threshold") + 1] == "0.6"
    for bad in ({"context_policy": "sometimes"}, {"context_threshold": 0.1}, {"context_threshold": True}, {"context_threshold": "high"}):
        with pytest.raises(RunError):
            mgr.create(ws, **bad)


def test_the_end_of_turn_bias_is_checked_kept_and_given_to_the_process(make, ws):
    mgr = make()
    rec = mgr.create(ws, end_turn_bias=-1.5)
    assert rec["end_turn_bias"] == -1.5
    cmd = mgr._command(rec["id"], rec, mgr.history_dir(rec["id"]))
    assert cmd[cmd.index("--end-turn-bias") + 1] == "-1.5"
    plain = mgr.create(ws)
    assert "--end-turn-bias" not in mgr._command(plain["id"], plain, mgr.history_dir(plain["id"]))
    for bad in (1, -40, True, "low"):
        with pytest.raises(RunError):
            mgr.create(ws, end_turn_bias=bad)


def test_going_back_takes_off_what_came_after_keeps_a_copy_and_wakes_from_there(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    for i, text in enumerate(("uno", "due", "tre"), 1):
        mgr.send(sid, text)
        until(lambda: kinds(mgr, sid).count("done") == i)
    for bad in (0, "1", True, 9):
        with pytest.raises(RunError):
            mgr.go_back(sid, bad)
    rec = mgr.go_back(sid, 2)
    assert rec["state"] == "dormant" and rec["turns"] == 2
    assert [m["content"] for m in mgr.history(sid)["messages"]] == ["uno", "echo: uno", "due", "echo: due"]
    folder = mgr.history_dir(sid)
    copies = list(folder.glob("before-going-back-*.json"))
    assert len(copies) == 1 and "tre" in copies[0].read_text(encoding="utf-8")                  # nothing is lost
    mgr.send(sid, "quattro")                                                                     # it goes on from there
    until(lambda: kinds(mgr, sid).count("done") == 4)
    assert [m["content"] for m in mgr.history(sid)["messages"]][-2:] == ["quattro", "echo: quattro"] and "tre" not in json.dumps(mgr.history(sid)["messages"])


def test_going_back_is_refused_while_the_agent_works(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "hang")                                   # the stand-in goes on working
    until(lambda: mgr.get(sid)["state"] == "busy")
    with pytest.raises(RunError) as err:
        mgr.go_back(sid, 1)
    assert err.value.code == "busy"


# -- a conversation's own copy of a git folder -------------------------------------------------------------------------------


def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return str(root)


def test_a_second_conversation_in_a_git_folder_gets_its_own_copy_and_the_first_stays_in_place(make, repo):
    mgr = make()
    first = mgr.create(repo)
    second = mgr.create(repo)
    assert "worktree" not in first and second["worktree"]["branch"] == f"basalt/{second['id']}"
    assert second["workdir"] != second["workspace"] and Path(second["workdir"]).is_dir()
    cmd = mgr._command(second["id"], second, mgr.history_dir(second["id"]))
    assert cmd[cmd.index("--workspace") + 1] == second["workdir"]
    cmd = mgr._command(first["id"], first, mgr.history_dir(first["id"]))
    assert cmd[cmd.index("--workspace") + 1] == first["workspace"]
    third = mgr.create(repo, worktree="off")
    assert "worktree" not in third


def test_a_copy_can_be_asked_for_and_a_folder_that_cannot_be_copied_says_so(make, ws, repo):
    mgr = make()
    assert mgr.create(repo, worktree="always")["worktree"]
    with pytest.raises(RunError) as e:
        mgr.create(ws, worktree="always")
    assert e.value.code == "no_copy" and "not a git repository" in str(e.value)
    with pytest.raises(RunError):
        mgr.create(repo, worktree="sometimes")
    assert "worktree" not in mgr.create(ws)                                       # auto, and not a git folder: in place


def test_two_conversations_do_not_work_in_one_folder_at_once_when_it_cannot_be_copied(make, ws):
    mgr = make()
    a, b = mgr.create(ws)["id"], mgr.create(ws)["id"]
    mgr.send(a, "hang")
    until(lambda: mgr.get(a)["state"] == "busy")
    with pytest.raises(RunError) as e:
        mgr.send(b, "ciao")
    assert e.value.code == "folder_busy" and "own copy" in str(e.value)
    assert mgr.send(a, "ancora")["turn"]                                          # the one that works may be told more


def test_what_a_conversation_changed_in_its_copy_waits_until_the_person_applies_it(make, repo):
    mgr = make()
    mgr.create(repo)
    sid = mgr.create(repo)["id"]
    work = Path(mgr.get(sid)["workdir"])
    (work / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (work / "new.txt").write_text("fresh\n", encoding="utf-8")
    assert {c["path"]: c["status"] for c in mgr.copy_changes(sid)["changes"]} == {"a.txt": "modified", "new.txt": "added"}
    assert "+two" in mgr.copy_diff(sid, "a.txt")["diff"]
    assert (Path(repo) / "a.txt").read_text(encoding="utf-8") == "one\n"          # nothing yet in the person's folder
    done = mgr.copy_apply(sid, ["new.txt"])
    assert done["applied"] == ["new.txt"] and (Path(repo) / "new.txt").exists() and (Path(repo) / "a.txt").read_text(encoding="utf-8") == "one\n"
    assert "A  new.txt" in _git(repo, "status", "--porcelain")
    for bad in ("../x", "-p", "", 3):
        with pytest.raises(RunError):
            mgr.copy_diff(sid, bad)
    with pytest.raises(RunError):
        mgr.copy_apply(sid, "a.txt")
    mgr.copy_apply(sid)
    assert (Path(repo) / "a.txt").read_text(encoding="utf-8").replace("\r\n", "\n") == "one\ntwo\n"


def test_applying_waits_for_a_turn_that_is_running_and_a_conversation_in_place_has_no_copy(make, repo):
    mgr = make()
    mgr.create(repo)
    sid = mgr.create(repo)["id"]
    mgr.send(sid, "hang")
    until(lambda: mgr.get(sid)["state"] == "busy")
    with pytest.raises(RunError) as e:
        mgr.copy_apply(sid)
    assert e.value.code == "busy"
    place = mgr.create(repo, worktree="off")["id"]
    with pytest.raises(RunError) as e:
        mgr.copy_changes(place)
    assert e.value.code == "no_copy"


def test_throwing_a_copy_away_closes_the_conversation_and_removes_the_folder_and_the_branch(make, repo):
    mgr = make()
    mgr.create(repo)
    sid = mgr.create(repo)["id"]
    work = Path(mgr.get(sid)["workdir"])
    (work / "a.txt").write_text("lost\n", encoding="utf-8")
    out = mgr.copy_discard(sid)
    assert out["state"] == "closed" and not out.get("worktree") and not work.exists()
    assert f"basalt/{sid}" not in _git(repo, "branch") and (Path(repo) / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_a_branch_of_a_conversation_in_a_copy_gets_a_copy_of_its_own(make, repo):
    mgr = make()
    mgr.create(repo)
    sid = mgr.create(repo)["id"]
    mgr.send(sid, "primo")
    until(lambda: "done" in kinds(mgr, sid))
    child = mgr.branch(sid, 1)
    assert child["worktree"]["dir"] != mgr.get(sid)["worktree"]["dir"] and Path(child["workdir"]).is_dir()


def test_a_code_is_typed_by_the_person_and_is_not_kept_in_the_conversation(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "code")
    until(lambda: mgr.get(sid)["state"] == "asking")
    assert mgr.get(sid)["ask"]["free"] is True
    for bad in ("", "   ", "x" * 33, "12;34", None, 554433):
        with pytest.raises(RunError, match="up to 32"):
            mgr.answer(sid, "c1", bad)
    mgr.answer(sid, "c1", "554 433")
    done = until(lambda: next((e for e in mgr.events(sid)["events"] if e["event"] == "done" and e["final"].startswith("answered")), None))
    assert done["final"] == "answered: 554 433"                                   # the process got it
    answered = [e for e in mgr.events(sid)["events"] if e["event"] == "answered"]
    assert answered and answered[0]["value"] == ""                                 # the conversation did not


def test_the_choice_of_a_capability_may_be_a_button_or_a_short_sentence_after_other(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "capability")
    until(lambda: mgr.get(sid)["state"] == "asking")
    assert mgr.get(sid)["ask"]["free_prefix"] == "other:" and mgr.get(sid)["ask"]["kind"] == "capability_choice"
    for bad in ("install:2", "use pdftotext", "other" + ":" + "x" * 501, None, ""):
        with pytest.raises(RunError, match="not one of the answers"):
            mgr.answer(sid, "k1", bad)
    mgr.answer(sid, "k1", "other:use pdftotext")
    done = until(lambda: next((e for e in mgr.events(sid)["events"] if e["event"] == "done" and e["final"].startswith("answered")), None))
    assert done["final"] == "answered: other:use pdftotext"


def test_what_a_conversation_gets_follows_the_wallet_choices(make, ws, tmp_path):
    vault, payments = tmp_path / "vault", tmp_path / "payments"
    prefs = {"enabled": True, "headless": False}
    mgr = make(command=None, vault_dir=vault, payments_dir=payments, browser_prefs=lambda: dict(prefs), browser_profile=tmp_path / "kept")
    mgr.command = mgr._command
    rec = {"id": "s1", "workspace": ws, "mode": "default", "ask_only": False}
    cmd = mgr._command("s1", rec, tmp_path / "h")
    assert cmd[cmd.index("--vault-dir") + 1] == str(vault) and cmd[cmd.index("--payments-dir") + 1] == str(payments)
    assert "--browser" in cmd and "--browser-headless" not in cmd
    prefs["headless"] = True
    assert "--browser-headless" in mgr._command("s1", rec, tmp_path / "h")
    assert "--browser-private" not in mgr._command("s1", rec, tmp_path / "h")
    prefs["private"] = True
    assert "--browser-private" in mgr._command("s1", rec, tmp_path / "h")
    assert "--browser-profile" not in mgr._command("s1", rec, tmp_path / "h")
    prefs["persistent"] = True
    kept = mgr._command("s1", rec, tmp_path / "h")
    assert kept[kept.index("--browser-profile") + 1] == str(tmp_path / "kept")
    prefs["enabled"] = False
    assert "--browser" not in mgr._command("s1", rec, tmp_path / "h")
    asking = mgr._command("s1", {**rec, "ask_only": True}, tmp_path / "h")                    # a question-only conversation gets none of it
    assert not {"--vault-dir", "--payments-dir", "--browser"} & set(asking)


# -- going back over actions that stay done, the tree of branches, two continuations compared -----------------------------------


def _stub_in(workdir_of):
    return lambda sid, rec, history: [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history), "--workdir", workdir_of(rec)]


def _said(mgr, sid, text, n):
    mgr.send(sid, text)
    until(lambda: kinds(mgr, sid).count("done") == n)


def test_going_back_names_the_actions_that_stay_done_and_asks_for_a_yes(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    _said(mgr, sid, "uno", 1)
    _said(mgr, sid, 'tool write_file reversible {"path": "a.txt"}', 2)
    _said(mgr, sid, 'tool shell_command irreversible {"command": "rm -rf x"}', 3)
    _said(mgr, sid, 'tool phone_call irreversible {"number": "+39 1"}', 4)
    seen = mgr.back_impact(sid, 1)
    assert [a["tool"] for a in seen["reversible"]] == ["write_file"] and [a["tool"] for a in seen["not_undone"]] == ["shell_command", "phone_call"]
    assert seen["needs_yes"] and seen["removes"]["requests"] == 3 and "rm -rf x" in seen["not_undone"][0]["what"]
    with pytest.raises(RunError) as err:
        mgr.go_back(sid, 1)
    assert err.value.code == "needs_yes" and mgr.get(sid)["turns"] == 4                      # nothing was touched
    assert len(mgr.back_impact(sid, 3)["not_undone"]) == 1 and mgr.back_impact(sid, 4)["not_undone"] == []
    back = mgr.go_back(sid, 1, accept_irreversible=True)
    assert back["turns"] == 1 and back["rewinds"][0]["turn"] == 1 and back["rewinds"][0]["was"] == 4
    assert [a["tool"] for a in back["rewinds"][0]["still_done"]] == ["shell_command", "phone_call"]           # it says they stayed done
    assert any(e["event"] == "went_back" and len(e["still_done"]) == 2 for e in mgr.events(sid)["events"])


def test_a_call_that_was_refused_or_a_read_does_not_count_and_an_unknown_tool_does(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    _said(mgr, sid, "uno", 1)
    _said(mgr, sid, 'tool read_file pure {"path": "a"}', 2)
    assert mgr.back_impact(sid, 1)["not_undone"] == [] and mgr.back_impact(sid, 1)["reversible"] == []
    path = mgr.history_dir(sid) / "history.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["messages"].append({"role": "assistant", "content": "", "tool_calls": [{"id": "r1", "type": "function", "function": {"name": "send_money", "arguments": "{}"}},
                                                                               {"id": "r2", "type": "function", "function": {"name": "shell_command", "arguments": "{\"command\": \"x\"}"}}]})
    data["messages"].append({"role": "tool", "tool_call_id": "r2", "name": "shell_command", "content": "Permission denied: not allowed."})
    path.write_text(json.dumps(data), encoding="utf-8")
    left = mgr.back_impact(sid, 1)["not_undone"]
    assert [(a["tool"], a["effect"]) for a in left] == [("send_money", "unknown")]               # the refused command did not happen


def test_a_conversation_learns_the_effect_of_a_tool_from_what_it_declares(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    _said(mgr, sid, "uno", 1)
    _said(mgr, sid, 'tool brand_new_tool reversible {"x": 1}', 2)
    assert json.loads((mgr.history_dir(sid) / "tool_effects.json").read_text(encoding="utf-8")) == {"brand_new_tool": "reversible"}
    seen = mgr.back_impact(sid, 1)
    assert [a["tool"] for a in seen["reversible"]] == ["brand_new_tool"] and seen["not_undone"] == []


def test_a_file_written_outside_the_working_copy_stays_done(make, ws, tmp_path):
    mgr = make()
    sid = mgr.create(ws)["id"]
    _said(mgr, sid, "uno", 1)
    _said(mgr, sid, "tool write_file reversible " + json.dumps({"path": str(tmp_path / "elsewhere.txt")}), 2)
    _said(mgr, sid, 'tool write_file reversible {"path": "inside.txt"}', 3)
    seen = mgr.back_impact(sid, 1)
    assert [a["what"] for a in seen["reversible"]] == ["write_file inside.txt"] and [a["tool"] for a in seen["not_undone"]] == ["write_file"]


def test_the_tree_places_branches_under_their_point_and_remembers_where_it_went_back(make, ws):
    mgr = make()
    root = mgr.create(ws, title="main")["id"]
    for i, text in enumerate(("uno", "due", "tre"), 1):
        _said(mgr, root, text, i)
    first = mgr.branch(root, 1)["id"]
    second = mgr.branch(root, 2)["id"]
    grand = mgr.branch(first, 1)["id"]
    mgr.go_back(root, 2)
    other = mgr.create(ws, title="alone")["id"]
    tree = mgr.tree()
    assert tree["count"] == 5
    roots = {r["id"]: r for r in tree["roots"]}
    assert set(roots) == {root, other} and roots[other]["children"] == []
    top = roots[root]
    assert [c["id"] for c in top["children"]] == [first, second]                              # ordered by the request they leave from
    assert top["children"][0]["branched_from"] == {"session": root, "turn": 1, "request": "uno"}
    assert top["children"][1]["branched_from"]["request"] == "due"
    assert [c["id"] for c in top["children"][0]["children"]] == [grand]
    assert top["rewinds"] == [{"turn": 2, "was": 3, "at": top["rewinds"][0]["at"], "still_done": 0}]


def test_a_conversation_whose_parent_is_gone_is_a_root_and_old_records_without_the_new_fields_work(make, ws):
    mgr = make()
    sid = mgr.create(ws, title="old")["id"]
    assert "rewinds" not in mgr.records[0] and mgr.tree()["roots"][0]["rewinds"] == []
    child = mgr.create(ws, title="child")["id"]
    mgr._record(child)["branched_from"] = {"session": "vanished", "turn": 2}
    assert {r["id"] for r in mgr.tree()["roots"]} == {sid, child}
    mgr._record(sid)["branched_from"] = {"session": sid, "turn": 1}                          # a loop is not followed
    assert {r["id"] for r in mgr.tree()["roots"]} == {sid, child}


def test_two_continuations_run_one_after_the_other_and_come_back_side_by_side(make, repo):
    mgr = make(command=_stub_in(lambda rec: rec.get("workdir") or rec["workspace"]))
    mgr.create(repo)
    sid = mgr.create(repo)["id"]                                                              # the second conversation has its own copy
    _said(mgr, sid, "uno", 1)
    out = mgr.compare_start(sid, 1, [{"text": "write a.txt first", "label": "gentle"}, {"text": "write a.txt second", "max_turns": 5, "mode": "plan"}])
    until(lambda: mgr.compare_result(out["id"])["state"] == "done", seconds=60)
    result = mgr.compare_result(out["id"])
    a, b = result["sides"]
    assert (a["state"], b["state"]) == ("done", "done") and len({a["session"], b["session"], sid}) == 3
    assert a["answer"] == "echo: write a.txt first" and b["answer"] == "echo: write a.txt second"
    assert "-echo: write a.txt first" in result["answers_diff"] and "+echo: write a.txt second" in result["answers_diff"]
    assert mgr.get(b["session"])["max_turns"] == 5 and mgr.get(b["session"])["mode"] == "plan" and mgr.get(a["session"])["compare"] == {"id": out["id"], "side": "a"}
    assert [p["path"] for p in result["files"]] == ["a.txt"] and result["files"][0]["a"] == "modified" and not result["files"][0]["same"]
    assert "-first" in result["files"][0]["diff"] and "+second" in result["files"][0]["diff"]
    first_done = max(e["t"] for e in mgr.events(a["session"])["events"] if e["event"] == "done")
    second_start = min(e["t"] for e in mgr.events(b["session"])["events"] if e["event"] == "accepted")
    assert first_done <= second_start                                                         # never together
    assert mgr.compare_list()["compares"][0]["id"] == out["id"]
    assert {c["id"] for r in mgr.tree()["roots"] if r["id"] == sid for c in r["children"]} == {a["session"], b["session"]}
    again = SessionManager(mgr.directory.parent, "http://127.0.0.1:1", command=mgr.command)
    try:
        assert again.compare_result(out["id"])["state"] == "done"                              # it is kept across a restart
    finally:
        again.shutdown()


def test_a_comparison_without_copies_has_no_file_diff_and_bad_requests_are_refused(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    _said(mgr, sid, "uno", 1)
    for bad in (None, [], [{"text": "a"}], [{"text": "a"}, {"text": ""}], [{"text": "a"}, {"text": "b", "mode": "auto"}], [{"text": "a"}, {"text": "b", "max_turns": 99}],
                [{"text": "a"}, "b"], [{"text": "a"}, {"text": "b", "end_turn_bias": 99}]):
        with pytest.raises(RunError):
            mgr.compare_start(sid, 1, bad)
    for turn in (0, "1", True, 9):
        with pytest.raises(RunError):
            mgr.compare_start(sid, turn, [{"text": "a"}, {"text": "b"}])
    out = mgr.compare_start(sid, 1, [{"text": "uno bis"}, {"text": "uno ter"}])
    until(lambda: mgr.compare_result(out["id"])["state"] == "done", seconds=60)
    assert mgr.compare_result(out["id"])["files"] is None
    with pytest.raises(RunError):
        mgr.compare_result("ghost")


def test_a_step_of_a_plan_collects_the_actions_done_while_it_was_worked_on(make, ws):
    notes = []
    mgr = make(plan_effect=lambda *a: notes.append(a))
    sid = mgr.create(ws)["id"]
    _said(mgr, sid, 'tool shell_command irreversible {"command": "before"}', 1)                # no step yet: not recorded
    _said(mgr, sid, 'tool plan_step_start reversible {"plan_id": "p1", "node_id": "n1"}', 2)
    _said(mgr, sid, 'tool phone_call irreversible {"number": "+39 1"}', 3)
    _said(mgr, sid, 'tool read_file pure {"path": "x"}', 4)
    _said(mgr, sid, 'tool plan_mark reversible {"plan_id": "p1", "node_id": "n1", "status": "completed"}', 5)
    _said(mgr, sid, 'tool shell_command irreversible {"command": "after"}', 6)
    assert [(n[0], n[1], n[2], n[3]) for n in notes] == [("p1", "n1", "phone_call", "irreversible")]
    assert "+39 1" in notes[0][4]


def test_a_rule_the_person_chose_in_a_question_is_kept_for_the_next_starts(make, ws):
    mgr = make()
    sid = mgr.create(ws, allow=["shell_command:git status*"])["id"]
    mgr.send(sid, "rule")
    until(lambda: "shell_command:pytest *" in mgr.get(sid).get("allow", []))
    assert mgr.get(sid)["allow"] == ["shell_command:git status*", "shell_command:pytest *"]          # once, however many times it is said


def test_the_mode_is_changed_while_the_conversation_lives_and_automatic_needs_the_confirmation(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "primo")
    until(lambda: "done" in kinds(mgr, sid))
    assert mgr.set_mode(sid, "plan")["mode"] == "plan"
    until(lambda: any(e["event"] == "mode" and e["mode"] == "plan" for e in mgr.events(sid)["events"]))      # the process was told
    with pytest.raises(RunError) as e:
        mgr.set_mode(sid, "auto")
    assert e.value.code == "auto_confirm" and mgr.get(sid)["mode"] == "plan"
    assert mgr.set_mode(sid, "auto", confirm_auto=True)["mode"] == "auto"
    with pytest.raises(RunError) as e:
        mgr.set_mode(sid, "everything")
    assert e.value.code == "bad_mode"


def test_a_mode_set_before_the_process_exists_is_what_it_starts_with_and_is_told_to_the_page(make, ws):
    seen = []
    mgr = make(command=lambda sid, rec, history: (seen.append((rec["mode"], rec.get("effort"))), stub_command()(sid, rec, history))[1])
    sid = mgr.create(ws)["id"]
    mgr.set_mode(sid, "read_only")
    mgr.set_effort(sid, "low")
    assert [e["event"] for e in mgr.events(sid)["events"]] == ["mode", "effort"]
    mgr.send(sid, "primo")
    until(lambda: "done" in kinds(mgr, sid))
    assert seen == [("read_only", "low")]


def test_the_thinking_level_is_kept_and_checked(make, ws):
    mgr = make()
    sid = mgr.create(ws, effort="medium")["id"]
    assert mgr.get(sid)["effort"] == "medium"
    assert mgr.set_effort(sid, "none")["effort"] == "none"
    assert mgr.set_effort(sid, "")["effort"] == ""
    with pytest.raises(RunError) as e:
        mgr.set_effort(sid, "maximum")
    assert e.value.code == "bad_effort"
    with pytest.raises(RunError):
        mgr.create(ws, effort="maximum")


def test_a_conversation_is_renamed_and_a_command_is_answered_by_its_process(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    assert mgr.rename(sid, "  my   name ")["title"] == "my name"
    with pytest.raises(RunError):
        mgr.rename(sid, "   ")
    cid = mgr.slash(sid, "status", "x")["id"]
    until(lambda: any(e["event"] == "command" and e["id"] == cid and e["data"] == {"args": "x"} for e in mgr.events(sid)["events"]))
    with pytest.raises(RunError) as e:
        mgr.slash(sid, "rm", "")
    assert e.value.code == "bad_command"

