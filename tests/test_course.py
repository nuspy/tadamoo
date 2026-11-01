"""The watch on the course of a request: a note to the agent when a signal first appears, a card to the person when it comes back."""
from test_agent_loop import ScriptedClient, call, make, say

from harness import course
from harness.permissions import PermissionLevel, PermissionManager
from harness.tools.registry import EffectClass


def failed(name, text="Error: no such file 12"):
    return {"type": "tool_end", "name": name, "status": "failed", "decision": "executed", "output": text}


class Fake:
    _loop_strikes = 0
    max_turns = 0
    turns: list = []
    context = None
    messages: list = []


def test_the_same_failure_twice_is_a_note_and_twice_more_is_a_card():
    watcher = course.Watcher()
    watcher.observe(failed("read_file", "Error: no such file 12"))
    assert watcher.check(Fake(), 1) is None
    watcher.observe(failed("read_file", "Error: no such file 99"))                      # numbers do not make it another failure
    first = watcher.check(Fake(), 2)
    assert first.stage == "note" and first.kind == "repeat_error" and "read_file failed 2 times" in first.note
    assert watcher.check(Fake(), 3) is None                                              # the cooldown
    watcher.observe(failed("read_file"))
    assert watcher.check(Fake(), 5) is None
    watcher.observe(failed("read_file"))
    second = watcher.check(Fake(), 7)
    assert second.stage == "card" and second.card()["questions"][0]["options"][0]["recommended"]
    assert watcher.check(Fake(), 12) is None                                             # a card is made once for a signal


def test_a_different_tool_or_a_gate_or_a_loop_strike_each_has_its_own_signal():
    watcher = course.Watcher()
    watcher.observe(failed("read_file", "Error: a"))
    watcher.observe(failed("write_file", "Error: a"))
    assert watcher.check(Fake(), 1) is None
    watcher.observe({"type": "gate", "decision": "retry"})
    watcher.observe({"type": "gate", "decision": "retry"})
    assert watcher.check(Fake(), 3).kind == "gate"
    striking = Fake()
    striking._loop_strikes = 1
    assert watcher.check(striking, 6).kind == "loop"


def test_the_matching_lesson_goes_into_the_note_and_its_fix_into_the_first_option():
    watcher = course.Watcher(lambda q: "Lessons from your earlier work (advice):\n- reading a mistyped name. fix: list the folder first")
    watcher.observe(failed("read_file"))
    watcher.observe(failed("read_file"))
    note = watcher.check(Fake(), 1)
    assert "list the folder first" in note.note and note.fix == "list the folder first"
    assert note.card()["questions"][0]["options"][0]["label"] == "Try the lesson"


def test_a_near_fold_is_told_once():
    class Ctx:
        mask_at, budget_tokens = 0.7, 1000

        def estimate_tokens(self, messages):
            return 640

    agent = Fake()
    agent.context = Ctx()
    watcher = course.Watcher()
    got = watcher.check(agent, 1)
    assert got.kind == "fold" and "nearly full" in got.note and watcher.check(agent, 9) is None


def test_the_loop_tells_the_agent_and_then_asks_the_person_and_ends_the_turn_there(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    steps = [call("read_file", {"path": f"missing{i}.txt"}, f"c{i}") for i in range(8)] + [say("never reached")]
    client = ScriptedClient(*steps)
    agent, _ = make(ws, client, permissions=PermissionManager(level=PermissionLevel.AUTO))
    asked = []

    def ask_user(questions, intro=""):
        asked.append((questions, intro))
        agent.request_end_turn()
        return "The questions are on the user's screen."
    agent.tools.register("ask_user", "asks", {"type": "object", "properties": {}}, EffectClass.PURE, ask_user)
    agent.course = course.Watcher()
    seen = []
    agent.on_event = seen.append
    agent.run("read the missing files")
    kinds = [(e["kind"], e["stage"]) for e in seen if e.get("type") == "course_check"]
    assert kinds[0] == ("repeat_error", "note") and kinds[-1] == ("repeat_error", "card")
    assert agent.stop_reason == "asked" and asked and asked[0][1].startswith("I keep running into the same problem")
    assert any("Course check" in str(m.get("content")) for m in agent.messages if m.get("role") == "user")
