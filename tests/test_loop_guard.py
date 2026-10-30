"""The loop guard: what calls bring back, cycles of calls, progress that clears the count, and long results that repeat."""
import copy
import json

from harness.agent_loop import LOOP, AgentLoop, LoopDetector
from harness.loop_guard import CHURN_STOP, CYCLE_ROUNDS, FAILURE_STOP, STUB_MIN_CHARS, OutcomeGuard, outcome_hash
from harness.tools.registry import EffectClass, ToolRegistry


def feed(guard, calls, **kw):
    """Plays (name, args, text) calls into the guard; returns the verdicts."""
    return [guard.observe(f"id{i}", name, args, text, kw.get("error", False), pure=kw.get("pure", False)) for i, (name, args, text) in enumerate(calls)]


# -- the hash of an outcome ---------------------------------------------------------------------------------------------

def test_what_varies_from_run_to_run_is_not_part_of_the_hash():
    a = outcome_hash("done in 0.12s at 2026-10-30 12:00:01 pid 4411 at 0x7ffd1234abcd", False)
    b = outcome_hash("done in 3.50s at 2026-10-31 09:15:59 pid 9 at 0x7ffd9999eeee", False)
    assert a == b
    assert outcome_hash("done", False) != outcome_hash("done", True) != outcome_hash("other", False)


# -- the detectors ------------------------------------------------------------------------------------------------------

def test_a_cycle_of_two_calls_with_the_same_results_is_told_then_counts_as_a_strike():
    guard = OutcomeGuard()
    cycle = [("read_file", {"path": "a"}, "A text"), ("search_code", {"q": "x"}, "no match")] * 6
    verdicts = feed(guard, cycle)
    notes = [(i, v.note) for i, v in enumerate(verdicts) if v.note]
    assert len(notes) == 2 and "pattern of 2" in notes[0][1]                            # one note when it is seen, one more when it goes on
    assert [i for i, v in enumerate(verdicts) if v.strike][0] == 2 * (CYCLE_ROUNDS + 1) - 1
    assert not any(v.strike for v in verdicts[:2 * CYCLE_ROUNDS])


def test_a_cycle_whose_results_change_is_work_and_a_change_to_the_files_clears_the_count():
    guard = OutcomeGuard()
    moving = [(n, {"x": 1}, f"text {i}") for i, n in enumerate(["read_file", "search_code"] * 8)]
    assert not any(v.note or v.strike for v in feed(guard, moving))
    guard = OutcomeGuard()
    feed(guard, [("read_file", {"path": "a"}, "A"), ("search_code", {"q": "x"}, "none")] * 2)
    guard.observe("w", "edit_file", {"path": "a"}, "ok", False, mutated=True)
    more = feed(guard, [("read_file", {"path": "a"}, "A"), ("search_code", {"q": "x"}, "none")] * 2)
    assert not any(v.note or v.strike for v in more)                                    # the earlier turns of the cycle no longer count


def test_one_result_for_many_different_calls_is_told_and_in_the_end_a_strike():
    guard = OutcomeGuard()
    verdicts = feed(guard, [("search_code", {"q": f"term{i}"}, "no matches") for i in range(CHURN_STOP + 1)])
    assert verdicts[3].note and "different calls" in verdicts[3].note
    assert verdicts[CHURN_STOP - 1].strike and not any(v.strike for v in verdicts[:CHURN_STOP - 1])


def test_a_shell_is_told_of_many_failures_but_never_counted_against_and_another_tool_is():
    guard = OutcomeGuard()
    shell = feed(guard, [("shell_command", {"command": f"pytest {i}"}, f"failure {chr(97 + i)}") for i in range(FAILURE_STOP + 2)], error=True)
    assert any("failed 5 times" in v.note for v in shell) and not any(v.strike for v in shell)
    other = feed(OutcomeGuard(), [("edit_file", {"n": i}, f"nope {chr(97 + i)}") for i in range(FAILURE_STOP + 2)], error=True)
    assert any(v.strike for v in other)


def test_a_long_result_that_is_the_same_as_an_earlier_one_becomes_a_stub():
    guard = OutcomeGuard()
    text = "x" * (STUB_MIN_CHARS + 10)
    first = guard.observe("c1", "read_file", {"path": "a"}, text, False, pure=True, held=lambda cid: True)
    second = guard.observe("c2", "read_file", {"path": "a"}, text, False, pure=True, held=lambda cid: True)
    assert first.stub == "" and "call c1" in second.stub
    masked = guard.observe("c3", "read_file", {"path": "a"}, text, False, pure=True, held=lambda cid: False)       # the first text is no longer there
    assert masked.stub == ""
    short = OutcomeGuard()
    short.observe("c1", "read_file", {"path": "b"}, "short", False, pure=True)
    assert short.observe("c2", "read_file", {"path": "b"}, "short", False, pure=True).stub == ""
    shell = OutcomeGuard()
    shell.observe("c1", "shell_command", {"command": "ls"}, text, False, pure=False)
    assert shell.observe("c2", "shell_command", {"command": "ls"}, text, False, pure=False).stub == ""     # only reads are stubbed


def test_tools_that_are_asked_again_by_design_are_left_alone():
    guard = OutcomeGuard()
    assert not any(v.note or v.strike for v in feed(guard, [("todo_write", {"todos": []}, "Checklist saved")] * 10))


# -- in the loop --------------------------------------------------------------------------------------------------------

def tools(log):
    registry = ToolRegistry()
    registry.register("probe", "p", {"type": "object", "properties": {"n": {"type": "integer"}}}, EffectClass.PURE, lambda n=0: log.append(("probe", n)) or "probe says same")
    registry.register("peek", "p", {"type": "object", "properties": {"n": {"type": "integer"}}}, EffectClass.PURE, lambda n=0: log.append(("peek", n)) or "peek says same")
    registry.register("poke", "p", {"type": "object", "properties": {}}, EffectClass.REVERSIBLE, lambda: log.append(("poke", 0)) or "poked")
    registry.register("big", "p", {"type": "object", "properties": {}}, EffectClass.PURE, lambda: "line\n" * 300)
    return registry


def msg(name, args, call_id):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


class Scripted:
    def __init__(self, *steps, then=None):
        self.steps, self.sent, self.then = list(steps), [], then

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        return self.steps.pop(0) if self.steps else self.then()


def test_a_run_going_round_in_a_cycle_with_changing_arguments_is_stopped():
    log, n = [], [0]

    def again():
        n[0] += 1
        return msg("probe" if n[0] % 2 else "peek", {"n": n[0]}, f"c{n[0]}")           # the arguments vary, the results do not
    client = Scripted(then=again)
    agent = AgentLoop(client, tools(log), max_turns=60, budget_grace=False)
    agent.run("go")
    assert agent.stop_reason == LOOP and len(agent.turns) < 30
    told = [m["content"] for m in agent.messages if m["role"] == "tool" and "[harness]" in m["content"]]
    assert told and any("different calls" in t or "pattern of" in t for t in told)


def test_the_same_call_after_a_change_is_not_a_repeat_but_after_nothing_it_is():
    log = []
    steps = [msg("probe", {"n": 1}, "a"), msg("probe", {"n": 1}, "b"), msg("poke", {}, "c"), msg("probe", {"n": 1}, "d"), msg("probe", {"n": 1}, "e"),
             {"choices": [{"message": {"role": "assistant", "content": "done"}}]}]
    agent = AgentLoop(Scripted(*steps), tools(log), max_turns=10, budget_grace=False)
    agent.run("go")
    assert [e[0] for e in log] == ["probe", "probe", "poke", "probe", "probe"] and agent.stop_reason == "final"      # the third and fourth probe ran
    log2 = []
    steps2 = [msg("probe", {"n": 1}, "a"), msg("probe", {"n": 1}, "b"), msg("probe", {"n": 1}, "c"), {"choices": [{"message": {"role": "assistant", "content": "done"}}]}]
    again = AgentLoop(Scripted(*steps2), tools(log2), max_turns=10, budget_grace=False)
    again.run("go")
    assert [e[0] for e in log2] == ["probe", "probe"]                                    # the third one was refused as before


def test_a_long_result_read_twice_is_not_sent_twice():
    log = []
    steps = [msg("big", {}, "a"), msg("big", {}, "b"), {"choices": [{"message": {"role": "assistant", "content": "done"}}]}]
    client = Scripted(*steps)
    agent = AgentLoop(client, tools(log), max_turns=10)
    agent.run("go")
    results = [m["content"] for m in client.sent[-1] if m["role"] == "tool"]
    assert results[0].count("line") == 300 and "Same result as call a above" in results[1] and len(results[1]) < 200


def test_the_detector_forgets_its_history_on_progress():
    detector = LoopDetector()
    assert not detector.check_and_record("t", {}) and not detector.check_and_record("t", {})
    detector.progress()
    assert not detector.check_and_record("t", {}) and not detector.check_and_record("t", {})
    assert detector.check_and_record("t", {})


def test_two_calls_taking_turns_with_the_same_results_are_a_loop_the_old_detector_does_not_see():
    log, n = [], [0]

    def again():
        n[0] += 1
        return msg("probe" if n[0] % 2 else "peek", {"n": 1}, f"c{n[0]}")
    agent = AgentLoop(Scripted(then=again), tools(log), max_turns=60, budget_grace=False)
    agent.run("go")
    assert agent.stop_reason == LOOP and len(agent.turns) <= 2 * (CYCLE_ROUNDS + 1) + 4
    assert any("pattern of 2" in m["content"] for m in agent.messages if m["role"] == "tool")
