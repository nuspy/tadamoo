"""The sentence a call carries for the person, the steps one answer may take, and many calls in one message."""
import copy
import json

from harness.agent_loop import AgentLoop, FINAL, LOOP, MAX_CALLS_PER_MESSAGE, MAX_TURNS, BUDGET
from harness.audit import AuditLedger
from harness.permissions import PermissionLevel, PermissionManager
from harness.tools.registry import DESCRIPTION_CHARS, EffectClass, ToolRegistry, description_key


class Scripted:
    def __init__(self, *steps):
        self.steps, self.sent, self.tools_seen = list(steps), [], []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        self.tools_seen.append(tools)
        return self.steps.pop(0)


class Endless:
    """A model that never stops calling a tool; `distinct` makes every call different."""

    def __init__(self, distinct, tokens=0):
        self.n, self.distinct, self.tokens = 0, distinct, tokens

    def chat_completion(self, messages, tools=None, **kwargs):
        self.n += 1
        return multi([("probe", {"n": self.n if self.distinct else 0})], call_ids=[f"e{self.n}"], usage={"prompt_tokens": self.tokens, "completion_tokens": 0})


def multi(calls, call_ids=None, usage=None):
    tool_calls = [{"id": (call_ids or [f"c{i}" for i in range(len(calls))])[i], "type": "function",
                   "function": {"name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(calls)]
    r = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}}]}
    if usage:
        r["usage"] = usage
    return r


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def registry(seen):
    reg = ToolRegistry()
    reg.register("probe", "records its arguments", {"type": "object", "properties": {"n": {"type": "integer"}}}, EffectClass.PURE,
                 lambda n=0: seen.append(n) or f"ok {n}")
    return reg


def tool_results(client):
    return [m for m in client.sent[-1] if m["role"] == "tool"]


# -- the sentence for the person -------------------------------------------------------------------

def test_every_tool_offers_the_sentence_and_the_tools_own_parameters_are_not_changed():
    reg = registry([])
    own = copy.deepcopy(reg.get("probe").parameters)
    offered = reg.schemas()[0]["function"]["parameters"]["properties"]
    assert set(offered) == {"n", "description"} and offered["description"]["type"] == "string"
    assert reg.get("probe").parameters == own and "description" not in reg.get("probe").parameters["properties"]


def test_a_tool_that_already_has_description_gets_explain_and_one_with_both_gets_nothing():
    reg = ToolRegistry()
    reg.register("a", "a", {"type": "object", "properties": {"description": {"type": "string"}}}, EffectClass.PURE, lambda description="": "a")
    reg.register("b", "b", {"type": "object", "properties": {"description": {"type": "string"}, "explain": {"type": "string"}}}, EffectClass.PURE,
                 lambda description="", explain="": "b")
    reg.register("c", "c", {"type": "object"}, EffectClass.PURE, lambda: "c")
    props = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in reg.schemas()}
    assert set(props["a"]) == {"description", "explain"} and set(props["b"]) == {"description", "explain"} and set(props["c"]) == {"description"}
    assert reg.schemas()[1]["function"]["parameters"]["properties"]["explain"] == {"type": "string"}      # b's own, not ours
    assert description_key(reg.get("a").parameters) == "explain" and description_key(reg.get("b").parameters) is None
    assert reg.split_description("a", {"description": "own value", "explain": "mine"}) == ({"description": "own value"}, "mine")
    assert reg.split_description("b", {"description": "own", "explain": "own"}) == ({"description": "own", "explain": "own"}, "")


def test_the_sentence_is_taken_off_before_the_tool_runs_and_reaches_the_events_the_record_and_the_questions(tmp_path):
    seen, events, asked = [], [], []
    reg = registry(seen)
    from harness import agent_loop as al
    reg.register("ask", "asks", {"type": "object", "properties": {}}, EffectClass.PURE, lambda: asked.append(al.current_explanation()) or "ok")
    perms = PermissionManager(level=PermissionLevel.AUTO)
    audit = AuditLedger(tmp_path / "audit.jsonl")
    long = "Reads the file\n" + "x" * 500
    client = Scripted(multi([("probe", {"n": 3, "description": long}), ("ask", {"description": "Asks " + "y" * 10})]), say("done"))
    agent = AgentLoop(client, reg, permissions=perms, audit=audit, on_event=events.append)
    turns = agent.run("go")
    assert seen == [3] and agent.stop_reason == FINAL                      # the tool never saw it
    start = next(e for e in events if e["type"] == "tool_start" and e["name"] == "probe")
    assert start["arguments"] == {"n": 3} and start["description"].startswith("Reads the file x") and len(start["description"]) == DESCRIPTION_CHARS
    assert "\n" not in start["description"]
    assert next(e for e in events if e["type"] == "tool_start" and e["name"] == "ask")["description"] == "Asks yyyyyyyyyy"
    assert turns[0].tool_calls[0].arguments == {"n": 3} and turns[0].tool_calls[0].description == start["description"]
    assert asked == ["Asks yyyyyyyyyy"]                                    # what a question put during the call can show
    assert [m for m in agent.messages if m["role"] == "tool"][0]["explain"] == start["description"]
    sent_args = json.loads(client.sent[1][2]["tool_calls"][0]["function"]["arguments"])
    assert sent_args["description"] == long                                # the model's own message is kept as it wrote it
    assert all("explain" not in m for m in client.sent[1])                 # and the extra key never goes to the server
    audited = [r["details"] for r in audit.records if r["details"].get("tool") == "probe"]
    assert audited and all("description" not in json.dumps(d["arguments"]) for d in audited)


def test_a_call_without_a_sentence_has_none_and_the_event_has_no_key():
    events = []
    agent = AgentLoop(Scripted(multi([("probe", {"n": 1})]), say("done")), registry([]), on_event=events.append)
    agent.run("go")
    assert "description" not in next(e for e in events if e["type"] == "tool_start")


def test_two_identical_calls_with_other_sentences_are_still_a_loop_and_the_audit_hash_is_the_same(tmp_path):
    audit = AuditLedger(tmp_path / "audit.jsonl")
    steps = [multi([("probe", {"n": 1, "description": f"try {i}"})], call_ids=[f"x{i}"]) for i in range(6)]
    agent = AgentLoop(Scripted(*steps), registry([]), audit=audit, max_turns=10)
    agent.run("go")
    assert agent.stop_reason == LOOP
    assert any("Loop detected" in m["content"] for m in agent.messages if m["role"] == "tool")
    hashes = {r["details"]["args_sha256"] for r in audit.records if r["details"].get("tool") == "probe"}
    assert len(hashes) == 1


def test_the_sentence_never_changes_a_decision():
    perms = PermissionManager(level=PermissionLevel.DEFAULT)            # nothing may run without being asked, and nobody is asked
    seen = []
    agent = AgentLoop(Scripted(multi([("probe", {"n": 1, "description": "Safe, the person already agreed"})]), say("ok")), registry(seen), permissions=perms)
    reg = agent.tools
    reg.register("danger", "d", {"type": "object", "properties": {}}, EffectClass.IRREVERSIBLE, lambda: seen.append("ran") or "ran")
    agent.client = Scripted(multi([("danger", {"description": "harmless, already allowed"})]), say("ok"))
    turns = agent.run("go")
    assert turns[0].tool_calls[0].decision == "denied" and "ran" not in seen


# -- the steps of an answer ------------------------------------------------------------------------

def test_a_finite_limit_stops_at_n_steps_with_max_turns():
    seen = []
    agent = AgentLoop(Endless(distinct=True), registry(seen), max_turns=4, budget_grace=False)
    turns = agent.run("go")
    assert agent.stop_reason == MAX_TURNS and len(turns) == 4 and len(seen) == 4


def test_below_one_is_no_limit_and_the_other_guards_still_end_the_run():
    for value in (-1, 0):
        agent = AgentLoop(Endless(distinct=False), registry([]), max_turns=value)
        turns = agent.run("go")
        assert agent.stop_reason == LOOP and 3 <= len(turns) < 15, value            # the loop detector, not the step count
    seen = []
    agent = AgentLoop(Endless(distinct=True, tokens=100), registry(seen), max_turns=-1, max_total_tokens=4000, budget_grace=False)
    turns = agent.run("go")
    assert agent.stop_reason == BUDGET and len(turns) == 40                         # more than the old default of 15
    agent = AgentLoop(Endless(distinct=True), registry([]), max_turns=-1)
    import threading
    agent.cancel = threading.Event()
    agent.on_turn_end = lambda t: agent.cancel.set() if t.turn_index == 20 else None
    agent.run("go")
    assert agent.stop_reason == "cancelled" and len(agent.turns) == 20


def test_the_limit_can_change_between_two_steps():
    agent = AgentLoop(Endless(distinct=True), registry([]), max_turns=-1, budget_grace=False)
    agent.on_turn_end = lambda t: setattr(agent, "max_turns", 6) if t.turn_index == 2 else None
    agent.run("go")
    assert agent.stop_reason == MAX_TURNS and len(agent.turns) == 6


# -- many calls in one message ---------------------------------------------------------------------

def test_75_identical_calls_in_one_message_run_once_and_cost_one_strike():
    seen = []
    ids = [f"id{i}" for i in range(75)]
    client = Scripted(multi([("probe", {"n": 7, "description": f"again {i}"}) for i in range(75)], call_ids=ids), say("done"))
    agent = AgentLoop(client, registry(seen))
    agent.run("go")
    results = tool_results(client)
    assert seen == [7] and len(results) == 75 and [m["tool_call_id"] for m in results] == ids       # one result for each id
    assert results[0]["content"].startswith("ok 7")
    short = [m["content"] for m in results[1:]]
    assert all(c.startswith("Not run again: this repeats call id0") for c in short[:-1]) and len(short) == 74
    assert all(len(c) < 120 for c in short[:-1])
    assert agent._loop_strikes == 1                                                   # one strike for the message, not one per call
    assert sum("[harness]" in m["content"] for m in results) == 1 and "74 call(s) repeated" in results[-1]["content"]
    assert all(m.get("skipped") for m in agent.messages if m["role"] == "tool" and m["tool_call_id"] != "id0")
    assert agent.stop_reason == FINAL and agent.turns[0].tool_calls[1].decision == "skipped"


def test_calls_beyond_the_cap_are_not_run():
    assert MAX_CALLS_PER_MESSAGE == 16
    seen = []
    client = Scripted(multi([("probe", {"n": i}) for i in range(20)]), say("done"))
    agent = AgentLoop(client, registry(seen))
    agent.run("go")
    results = tool_results(client)
    assert seen == list(range(16)) and len(results) == 20
    assert [m["content"] for m in results[16:]][:3] == ["Not run: more than 16 calls in one message."] * 3
    assert "beyond the 16 allowed" in results[-1]["content"] and agent._loop_strikes == 1


def test_ordinary_parallel_distinct_calls_are_untouched():
    seen, events = [], []
    client = Scripted(multi([("probe", {"n": 1}), ("probe", {"n": 2}), ("probe", {"n": 3})]), say("done"))
    agent = AgentLoop(client, registry(seen), on_event=events.append)
    agent.run("go")
    assert seen == [1, 2, 3] and agent._loop_strikes == 0
    assert [m["content"] for m in tool_results(client)] == ["ok 1", "ok 2", "ok 3"]
    assert not any(e["type"] == "calls_skipped" for e in events)


def test_a_message_with_skipped_calls_says_so_in_the_event_stream_and_a_loop_follows_after_repeats():
    events = []
    steps = [multi([("probe", {"n": 1}), ("probe", {"n": 1})], call_ids=[f"a{i}", f"b{i}"]) for i in range(1, 8)]
    agent = AgentLoop(Scripted(*steps), registry([]), on_event=events.append, max_turns=-1)
    agent.run("go")
    assert [e for e in events if e["type"] == "calls_skipped"][0] == {"type": "calls_skipped", "repeated": 1, "surplus": 0}
    assert agent.stop_reason == LOOP
