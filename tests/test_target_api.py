"""The Target Loop on the server: the conversation seen by the loop, the manager (persistence, history, the person's edits) and the HTTP routes."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo import target as tg
from serve import handler
from serve.agent_sessions import SessionManager
from serve.approvals import ApprovalBoard
from serve.tasks import ChangeJournal, TaskJournal
from serve.target_api import SessionConversation, TargetManager
from test_agent_gui_api import Api, wait_for
from test_e2e_mock_server import ROOT, ByteTokenizer, ChatTemplate, MockEngine, Service, _NoTelemetry, answer, serve
from test_target_loop import FakeConversation, FakeLLM, fix, plan, task, verdict, verdicts

STUB = str(Path(__file__).resolve().parents[1] / "serve" / "tests" / "session_stub.py")


def stub_command(sid, rec, history):
    return [sys.executable, STUB, "--session-id", sid, "--history-dir", str(history)]


def until(predicate, seconds=20, step=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("timed out waiting")


# -- the conversation as the loop sees it ------------------------------------------------------------------------------------

class ScriptedSessions:
    """A conversation's event stream, written by the test: `script(turn)` gives the events that follow the message."""

    def __init__(self, script, state="busy"):
        self.script, self.state, self.log, self.sent, self.cancelled = script, state, [], [], 0

    def events(self, sid, after=0):
        return {"events": [e for e in self.log if e["seq"] > after], "next": len(self.log), "state": self.state, "blocked": None}

    def emit(self, **event):
        self.log.append({**event, "seq": len(self.log) + 1})

    def send(self, sid, text, attachments=None, place=None):
        turn = f"T{len(self.sent) + 1}"
        self.sent.append(text)
        self.emit(event="user", turn=turn, text=text)
        threading.Thread(target=self.script, args=(self, turn), daemon=True).start()
        return {"turn": turn}

    def cancel(self, sid):
        self.cancelled += 1

    def get(self, sid):
        return {"id": sid, "pending": [{"question": "Call Anna?"}]}


def conversation(script, **kw):
    sessions = ScriptedSessions(script, **kw)
    return sessions, SessionConversation(sessions, "s1", poll_s=0.02, lost_s=0.4)


def tool(sessions, turn, name, args, output, status="completed"):
    sessions.emit(event="agent", turn=turn, data={"type": "tool_start", "call_id": "c" + turn, "name": name, "arguments": args})
    sessions.emit(event="agent", turn=turn, data={"type": "tool_end", "call_id": "c" + turn, "name": name, "status": status, "output": output})


def test_the_prompt_goes_in_as_a_message_and_the_answer_and_the_tools_come_back():
    def script(s, turn):
        s.emit(event="accepted", turn=turn)
        tool(s, turn, "write_file", {"path": "a.txt"}, "written")
        s.emit(event="done", turn=turn, stop_reason="final", final="all done", more=False)
    sessions, conv = conversation(script)
    result = conv.run("do it", threading.Event())
    assert sessions.sent == ["do it"] and result.final == "all done" and result.stop_reason == "final"
    assert result.tools == [{"name": "write_file", "arguments": {"path": "a.txt"}, "status": "completed", "output": "written"}]


def test_a_message_steered_into_a_running_task_is_finished_by_that_tasks_end():
    def script(s, turn):
        s.emit(event="queued", turn=turn, text="x")
        s.emit(event="routed", turn=turn, how="steer", position=0)
        time.sleep(0.1)
        s.emit(event="done", turn="T0", stop_reason="final", final="the other task and mine", more=False)
    sessions, conv = conversation(script)
    result = conv.run("do it", threading.Event())
    assert result.final == "the other task and mine"


def test_a_question_that_ends_the_turn_is_waited_for_until_the_person_answers():
    def script(s, turn):
        s.emit(event="done", turn=turn, stop_reason="asked", final="", more=False)
        time.sleep(0.15)
        s.emit(event="done", turn="T9", stop_reason="final", final="after the answer", more=False)
    sessions, conv = conversation(script, state="idle")
    result = conv.run("do it", threading.Event())
    assert result.final == "after the answer" and result.stop_reason == "final"


def test_stopping_cancels_the_turn_in_the_conversation_and_returns_cancelled():
    cancel = threading.Event()

    def script(s, turn):
        cancel.wait(5)
        time.sleep(0.05)
        s.emit(event="done", turn=turn, stop_reason="cancelled", final="", more=False)
    sessions, conv = conversation(script)
    threading.Timer(0.1, cancel.set).start()
    result = conv.run("do it", cancel)
    assert sessions.cancelled == 1 and result.stop_reason == "cancelled"


def test_a_turn_that_never_answers_in_an_idle_conversation_is_taken_for_lost():
    sessions, conv = conversation(lambda s, turn: None, state="idle")
    result = conv.run("do it", threading.Event())
    assert result.stop_reason == "error" and "quiet" in result.error


def test_parked_questions_and_edits_since_a_time_are_handed_to_the_loop():
    sessions = ScriptedSessions(lambda s, t: None)
    edits = [{"file": "a", "time": 5}, {"file": "b", "time": 50}]
    conv = SessionConversation(sessions, "s1", changes=lambda: edits)
    assert conv.parked() == ["Call Anna?"] and [e["file"] for e in conv.changes_since(10)] == ["b"]


# -- the manager ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path):
    made = []

    def build(llm, conv_factory=None, journal=None):
        sessions = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=stub_command, tick_s=0.2)
        made.append(sessions)
        manager = TargetManager(sessions, journal or TaskJournal(tmp_path / "j"), llm_factory=lambda: llm, conversation=conv_factory or (lambda sid: FakeConversation()), waits=(0.01,))
        made.append(manager)
        folder = tmp_path / "ws"
        folder.mkdir(exist_ok=True)
        return sessions, manager, sessions.create(str(folder))["id"]
    yield build
    for m in reversed(made):
        (m.shutdown if hasattr(m, "shutdown") else m.close)()


ONE = dict(target_plan=[plan([("c1", "A"), ("c2", "B")], [task("a", "A", ["c1"]), task("b", "B", ["c2"])])],
           target_verdict=[verdicts(verdict("c1", "achieved"), verdict("c2", "achieved"))])


def test_a_target_runs_and_every_one_of_the_conversation_is_kept_for_the_dialog(world, tmp_path):
    sessions, manager, sid = world(FakeLLM(**ONE))
    first = manager.create(sid, "Make A and B.", {"stall_passes": 4})
    assert first["state"] == "running" and first["options"]["stall_passes"] == 4
    until(lambda: manager.get(sid, first["id"])["state"] == "achieved")
    second = manager.create(sid, "Another one.")
    until(lambda: manager.get(sid, second["id"])["state"] in ("achieved", "stopped"))
    listing = manager.listing(sid)
    assert [t["text"] for t in listing["targets"]] == ["Another one.", "Make A and B."] and listing["active"] is None
    assert listing["targets"][1]["counts"]["achieved"] == 2 and listing["targets"][1]["iterations"][0]["n"] == 1
    # a second manager over the same folder (a restart of the server) finds them again
    again = TargetManager(sessions, TaskJournal(tmp_path / "j"), llm_factory=lambda: FakeLLM(**ONE))
    assert [t["text"] for t in again.listing(sid)["targets"]] == ["Another one.", "Make A and B."]
    assert (sessions.history_dir(sid) / "targets.json").is_file()


def test_the_first_message_option_is_kept_with_the_target_and_listed_with_its_default(world):
    _, manager, sid = world(FakeLLM(**ONE))
    assert manager.listing(sid)["defaults"]["first_message"] == "joined"
    made = manager.create(sid, "Make A and B.", {"first_message": "separate"})
    assert made["options"]["first_message"] == "separate"
    until(lambda: manager.get(sid, made["id"])["state"] == "achieved")
    assert manager.get(sid, made["id"])["options"]["first_message"] == "separate"


def test_a_target_that_was_running_when_the_server_ended_comes_back_stopped(world, tmp_path):
    sessions, manager, sid = world(FakeLLM(**ONE))
    stuck = tg.new_target("Left running", session=sid)
    (sessions.history_dir(sid)).mkdir(parents=True, exist_ok=True)
    (sessions.history_dir(sid) / "targets.json").write_text(json.dumps({"targets": [stuck]}), encoding="utf-8")
    only = manager.listing(sid)["targets"][0]
    assert only["state"] == "stopped" and only["stop_reason"] == "interrupted" and manager.listing(sid)["active"] is None


def test_one_target_at_a_time_and_the_person_can_stop_it(world):
    gate = threading.Event()

    class Waiting(FakeConversation):
        def run(self, prompt, cancel):
            gate.set()
            cancel.wait(10)
            return tg.TurnResult(stop_reason="cancelled")
    waiting = Waiting()
    sessions, manager, sid = world(FakeLLM(**ONE), lambda s: waiting)
    t = manager.create(sid, "Slow")
    assert gate.wait(5)
    with pytest.raises(tg.TargetError) as busy:
        manager.create(sid, "Second")
    assert busy.value.code == "busy"
    manager.stop(sid, t["id"])
    until(lambda: manager.get(sid, t["id"])["state"] == "stopped")
    assert manager.get(sid, t["id"])["stop_reason"] == "person" and waiting.cancelled == 1
    with pytest.raises(tg.TargetError) as gone:
        manager.stop(sid, t["id"])
    assert gone.value.code == "not_running"


def test_the_person_edits_notes_and_status_and_the_judge_leaves_the_status_alone(world):
    llm = FakeLLM(target_plan=[plan([("c1", "A")], [task("a", "A", ["c1"])])], target_verdict=[verdicts(verdict("c1", "not_met", missing="x"))], target_fix=[fix(task("n", "again", ["c1"]))])
    sessions, manager, sid = world(llm)
    t = manager.create(sid, "Make A.", {"max_iterations": 1})
    until(lambda: manager.get(sid, t["id"])["state"] == "stopped")
    out = manager.annotate(sid, t["id"], "c1", note="  Done by hand, the client agreed to skip the tests.  ", status="achieved")
    c = out["criteria"][0]
    assert c["note"] == "Done by hand, the client agreed to skip the tests." and c["status"] == "achieved" and c["by"] == "person"
    back = manager.annotate(sid, t["id"], "c1", status="pending")["criteria"][0]
    assert back["status"] == "pending" and back["by"] == ""
    for bad in (dict(status="partial"), dict(note="x" * 5000)):
        with pytest.raises(tg.TargetError):
            manager.annotate(sid, t["id"], "c1", **bad)
    with pytest.raises(tg.TargetError) as gone:
        manager.annotate(sid, t["id"], "zz", note="n")
    assert gone.value.code == "gone"


def test_a_past_target_can_be_resumed_or_copied_and_forgotten(world):
    llm = FakeLLM(target_plan=[plan([("c1", "A")], [task("a", "A", ["c1"])])],
                  target_verdict=[verdicts(verdict("c1", "not_met", missing="x")), verdicts(verdict("c1", "achieved"))], target_fix=[fix(task("n", "again", ["c1"]))])
    sessions, manager, sid = world(llm)
    t = manager.create(sid, "Make A.", {"max_iterations": 1})
    until(lambda: manager.get(sid, t["id"])["state"] == "stopped")
    assert manager.get(sid, t["id"])["stop_reason"] == "limit"
    resumed = manager.rerun(sid, t["id"], "resume")
    assert resumed["id"] == t["id"] and resumed["state"] == "running" and resumed["options"]["max_iterations"] == 1
    until(lambda: manager.get(sid, t["id"])["state"] == "achieved")
    assert len(manager.get(sid, t["id"])["iterations"]) == 2
    with pytest.raises(tg.TargetError) as done:
        manager.rerun(sid, t["id"], "resume")
    assert done.value.code == "achieved"
    copy = manager.rerun(sid, t["id"], "copy")
    assert copy["id"] != t["id"] and copy["text"] == "Make A." and copy["origin"] == t["id"]
    until(lambda: manager.get(sid, copy["id"])["state"] != "running")
    assert [x["id"] for x in manager.forget(sid, t["id"])["targets"]] == [copy["id"]]


def test_a_conversation_that_is_not_there_has_no_targets(world):
    sessions, manager, sid = world(FakeLLM(**ONE))
    from serve.agent_runs import RunError
    with pytest.raises(RunError):
        manager.listing("nosuch")
    with pytest.raises(RunError):
        manager.create("nosuch", "x")


def test_the_loop_works_with_the_real_manager_and_a_stand_in_process(world, tmp_path):
    from serve.target_api import SessionConversation as Real
    sessions, manager, sid = world(FakeLLM(target_plan=[plan([("c1", "answered")], [task("a", "say hi", ["c1"])])],
                                           target_verdict=[verdicts(verdict("c1", "achieved", refs=("E1",)))]),
                                   lambda s: Real(sessions, s, poll_s=0.05))
    t = manager.create(sid, "ciao")
    until(lambda: manager.get(sid, t["id"])["state"] == "achieved", seconds=30)
    texts = [e["text"] for e in sessions.events(sid)["events"] if e["event"] == "user"]
    assert len(texts) == 1 and texts[0].startswith("ciao\n\n---\nTarget Loop") and "Do this now:\ndo say hi" in texts[0]
    notes = [e["what"] for e in sessions.events(sid)["events"] if e["event"] == "target"]
    assert notes[0] == "planned" and notes[-1] == "achieved" and "task" in notes and "pass" in notes


# -- the HTTP routes -----------------------------------------------------------------------------------------------------------------

def test_routes_create_read_annotate_stop_rerun_and_forget(tmp_path, monkeypatch):
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, answer("ok"), max_context=65536), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    svc.telemetry = _NoTelemetry()
    httpd = serve(svc, port=0)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    board, tasks = ApprovalBoard(), TaskJournal(tmp_path / "j")
    sessions = SessionManager(tmp_path / "j", url, "", board=board, command=stub_command, tick_s=0.2)
    llm = FakeLLM(**ONE)
    targets = TargetManager(sessions, tasks, llm_factory=lambda: llm, conversation=lambda sid: FakeConversation(), waits=(0.01,))
    for name, value in (("_TASKS", tasks), ("_APPROVALS", board), ("_SESSIONS", sessions), ("_TARGETS", targets), ("_CHANGES", ChangeJournal(tmp_path / "j"))):
        monkeypatch.setattr(handler, name, value)
    api = Api(url)
    try:
        folder = tmp_path / "ws"
        folder.mkdir()
        sid = api("POST", "/api/agent/sessions", {"workspace": str(folder)})[1]["session"]["id"]
        base = f"/api/agent/sessions/{sid}/targets"
        assert api("GET", base)[1]["targets"] == []
        code, body = api("POST", base, {"text": "Make A and B.", "options": {"verify": "", "stall_passes": 2}})
        assert code == 200 and body["target"]["state"] == "running"
        tid = body["target"]["id"]
        wait_for(lambda: api("GET", f"{base}/{tid}")[1]["target"]["state"] == "achieved", seconds=20)
        view = api("GET", f"{base}/{tid}")[1]["target"]
        assert view["counts"]["achieved"] == 2 and [t["id"] for t in view["tasks"]] == ["t1", "t2"]
        registry = api("GET", "/api/tasks")[1]["tasks"]
        assert sorted(t["id"] for t in registry) == [f"tg-{tid}-t1", f"tg-{tid}-t2"]
        assert api("POST", f"{base}/{tid}/notes", {"criterion": "c1", "note": "checked by hand"})[1]["target"]["criteria"][0]["note"] == "checked by hand"
        assert api("POST", f"{base}/{tid}/notes", {"criterion": "c1", "status": "partial"})[0] == 400
        assert api("POST", f"{base}/{tid}/stop", {})[0] == 409                                  # it is not running
        assert api("POST", f"{base}/{tid}/rerun", {"mode": "resume"})[0] == 409                  # achieved already
        assert api("POST", f"{base}/{tid}/rerun", {"mode": "sideways"})[0] == 400
        code, again = api("POST", f"{base}/{tid}/rerun", {"mode": "copy"})
        assert code == 200 and again["target"]["origin"] == tid
        wait_for(lambda: api("GET", f"{base}/{again['target']['id']}")[1]["target"]["state"] != "running", seconds=20)
        assert api("GET", f"{base}/nosuch")[0] == 404
        assert api("POST", base, {"text": "  "})[0] == 400
        assert api("GET", "/api/agent/sessions/nosuch/targets")[0] == 404
        listing = api("POST", f"{base}/{tid}/forget", {})[1]
        assert [t["id"] for t in listing["targets"]] == [again["target"]["id"]]
    finally:
        targets.shutdown()
        sessions.shutdown()
        httpd.shutdown()
        httpd.server_close()
