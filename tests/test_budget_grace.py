"""A budget that runs out warns first and allows one last report with no tools."""
import copy
import json

from tadamoo.agent_loop import BUDGET, FINAL, MAX_TURNS, AgentLoop
from tadamoo.tools.registry import EffectClass, ToolRegistry


def registry():
    r = ToolRegistry()
    r.register("probe", "p", {"type": "object", "properties": {"n": {"type": "integer"}}}, EffectClass.PURE, lambda n=0: f"result {n}")
    return r


def call(n, usage=None):
    out = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{n}", "type": "function", "function": {"name": "probe", "arguments": json.dumps({"n": n})}}]}}]}
    if usage:
        out["usage"] = usage
    return out


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class Scripted:
    def __init__(self, *steps):
        self.steps, self.sent = list(steps), []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        return self.steps.pop(0)


def harness_notes(messages):
    return [m["content"] for m in messages if m["role"] == "user" and m["content"].startswith("[harness]")]


def test_the_step_limit_warns_near_the_end_and_gives_one_last_reply_without_tools():
    client = Scripted(call(1), call(2), call(3), call(4), call(5), say("Report: steps 1 to 5 done, step 6 not."))
    events = []
    agent = AgentLoop(client, registry(), max_turns=5, on_event=events.append)
    turns = agent.run("go")
    assert agent.stop_reason == MAX_TURNS and len(turns) == 6 and len(client.sent) == 6
    notes = harness_notes(client.sent[-1])
    assert sum("nearly used up" in n for n in notes) == 1 and "Do not call any tool" in notes[-1]
    assert [m for m in client.sent[4] if m["role"] == "user" and "nearly used up" in m["content"]]          # said before the last step, not only at the end
    assert "nearly used up" not in " ".join(harness_notes(client.sent[2]))
    assert agent.messages[-1] == {"role": "assistant", "content": "Report: steps 1 to 5 done, step 6 not."}
    assert [e["type"] for e in events if e["type"] in ("budget_warning", "budget_grace")] == ["budget_warning", "budget_grace"]


def test_a_call_in_the_last_reply_is_not_run_and_its_text_is_kept():
    ran = []
    r = ToolRegistry()
    r.register("probe", "p", {"type": "object", "properties": {"n": {"type": "integer"}}}, EffectClass.PURE, lambda n=0: ran.append(n) or "ok")
    steps = [call(1), call(2), {"choices": [{"message": {"role": "assistant", "content": "Status so far.", "tool_calls": [
        {"id": "z", "type": "function", "function": {"name": "probe", "arguments": '{"n": 9}'}}]}}]}]
    agent = AgentLoop(Scripted(*steps), r, max_turns=2)
    agent.run("go")
    assert agent.stop_reason == MAX_TURNS and ran == [1, 2] and agent.messages[-1]["content"] == "Status so far."
    assert not [m for m in agent.messages if m.get("tool_calls") and m["tool_calls"][0]["id"] == "z"]


def test_a_token_budget_gets_the_warning_and_the_last_report_too():
    usage = {"prompt_tokens": 440, "completion_tokens": 20}
    client = Scripted(call(1, usage), call(2, usage), call(3, usage), say("Report."))
    agent = AgentLoop(client, registry(), max_total_tokens=1000)
    agent.run("go")
    assert agent.stop_reason == BUDGET and len(client.sent) == 4
    assert not any("nearly used up" in n for n in harness_notes(client.sent[1])) and any("nearly used up" in n for n in harness_notes(client.sent[2]))
    assert "Do not call any tool" in harness_notes(client.sent[3])[-1] and agent.messages[-1]["content"] == "Report."


def test_the_last_report_is_only_one_call_and_can_be_turned_off():
    agent = AgentLoop(Scripted(call(1), call(2), call(3)), registry(), max_turns=2, budget_grace=False)
    turns = agent.run("go")
    assert agent.stop_reason == MAX_TURNS and len(turns) == 2 and not harness_notes(agent.messages)


def test_a_subagent_gets_neither_the_warning_nor_the_last_report():
    agent = AgentLoop(Scripted(call(1), call(2), call(3)), registry(), max_turns=2, subagent=True)
    turns = agent.run("go")
    assert agent.stop_reason == MAX_TURNS and len(turns) == 2 and not harness_notes(agent.messages)


def test_a_run_that_finishes_inside_its_budget_is_not_told_anything_it_does_not_need():
    agent = AgentLoop(Scripted(call(1), say("Done.")), registry(), max_turns=20)
    agent.run("go")
    assert agent.stop_reason == FINAL and not harness_notes(agent.messages)
