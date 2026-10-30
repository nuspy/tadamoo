"""A web read made twice in one request of the person is not made again; the second call is told where the first one's text is."""
import copy
import json

from harness import untrusted
from harness.agent_loop import AgentLoop, FINAL
from harness.audit import AuditLedger
from harness.context import ContextManager, TokenJuiceCompactor
from harness.permissions import PermissionLevel, PermissionManager
from harness.repeat_guard import WEB_READ_TOOLS, RepeatGuard
from harness.tools.registry import EffectClass, ToolRegistry


class Scripted:
    def __init__(self, *steps):
        self.steps, self.sent = list(steps), []

    def chat_completion(self, messages, tools=None, **kwargs):
        self.sent.append(copy.deepcopy(messages))
        return self.steps.pop(0)


def calls(*items):
    """One model message with these (call id, tool, arguments) calls."""
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}} for cid, name, args in items]}}]}


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def page(url, size=40):
    return f"URL: {url}\nTitle: Page of {url}\nText:\n" + "word " * size


def make(tmp_path, client, results=None, **kw):
    ran = []
    registry = ToolRegistry()

    def browser_open(url):
        ran.append(("browser_open", url))
        value = (results or {}).get(url)
        return value if value is not None else page(url)

    registry.register("browser_open", "opens a page", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                      EffectClass.PURE, browser_open, untrusted=True)
    registry.register("search_web", "searches", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
                      EffectClass.PURE, lambda query, limit=5: ran.append(("search_web", query, limit)) or [{"title": "t", "url": "u"}], untrusted=True)
    registry.register("probe", "records its argument", {"type": "object", "properties": {"n": {"type": "integer"}}}, EffectClass.PURE,
                      lambda n=0: ran.append(("probe", n)) or f"ok {n}")
    audit = AuditLedger(tmp_path / "audit.jsonl")
    agent = AgentLoop(client, registry, max_turns=12, audit=audit, **kw)
    return agent, ran, audit


def tool_texts(agent):
    return [m["content"] for m in agent.messages if m["role"] == "tool"]


def test_the_same_page_twice_in_one_request_is_read_once_and_the_second_result_names_the_first(tmp_path):
    client = Scripted(calls(("a", "browser_open", {"url": "https://n.example/1"})), calls(("b", "browser_open", {"url": "https://n.example/1"})), say("done"))
    agent, ran, _ = make(tmp_path, client)
    agent.run("news")
    assert ran == [("browser_open", "https://n.example/1")]
    first, second = tool_texts(agent)
    assert "Page of https://n.example/1" in first
    assert second == ("Not run again: browser_open was already called with these same arguments at step 1 of this request. "
                      "Its text is above in the conversation. The call was not repeated.")
    assert agent.stop_reason == FINAL


def test_the_sentence_for_the_person_does_not_make_a_call_different(tmp_path):
    client = Scripted(calls(("a", "browser_open", {"url": "u", "description": "reading the first page"})),
                      calls(("b", "browser_open", {"url": "u", "description": "reading it again"})), say("done"))
    agent, ran, _ = make(tmp_path, client)
    agent.run("news")
    assert len(ran) == 1 and "Not run again" in tool_texts(agent)[1]


def test_after_a_new_message_of_the_person_the_page_is_read_again(tmp_path):
    client = Scripted(calls(("a", "browser_open", {"url": "u"})), say("one"), calls(("b", "browser_open", {"url": "u"})), say("two"))
    agent, ran, _ = make(tmp_path, client)
    agent.run("first request")
    agent.run("second request")
    assert ran == [("browser_open", "u")] * 2
    assert "Not run again" not in " ".join(tool_texts(agent))


def test_a_different_address_or_other_arguments_run(tmp_path):
    client = Scripted(calls(("a", "browser_open", {"url": "u1"}), ("s", "search_web", {"query": "q", "limit": 5})),
                      calls(("b", "browser_open", {"url": "u2"}), ("t", "search_web", {"query": "q", "limit": 6}), ("c", "search_web", {"query": "q", "limit": 5})),
                      say("done"))
    agent, ran, _ = make(tmp_path, client)
    agent.run("go")
    assert ran == [("browser_open", "u1"), ("search_web", "q", 5), ("browser_open", "u2"), ("search_web", "q", 6)]
    assert "Not run again: search_web" in tool_texts(agent)[-1] and "at step 1 " in tool_texts(agent)[-1]


def test_the_step_is_the_step_of_this_request(tmp_path):
    client = Scripted(calls(("a", "probe", {"n": 1})), calls(("b", "probe", {"n": 2})), calls(("c", "browser_open", {"url": "u"})),
                      calls(("d", "probe", {"n": 3})), calls(("e", "browser_open", {"url": "u"})), say("done"))
    agent, ran, _ = make(tmp_path, client)
    agent.run("go")
    assert "at step 3 of this request" in tool_texts(agent)[-1]


def test_a_masked_first_result_is_named_by_the_file_that_holds_it(tmp_path):
    big = page("https://n.example/big", size=4000)
    client = Scripted(calls(("a", "browser_open", {"url": "https://n.example/big"})), calls(("b", "browser_open", {"url": "https://n.example/other"})),
                      calls(("c", "browser_open", {"url": "https://n.example/big"})), say("done"))
    mgr = ContextManager(TokenJuiceCompactor(tmp_path / "artifacts", display_dir=".basalt/artifacts"), budget_tokens=2000, mask_at=0.5, keep_recent=1)
    agent, ran, _ = make(tmp_path, client, results={"https://n.example/other": page("https://n.example/other", size=4000)}, context=mgr)
    agent.run("go")
    assert [r[1] for r in ran] == ["https://n.example/big", "https://n.example/other"]
    third = tool_texts(agent)[-1]
    assert third.startswith("Not run again: browser_open was already called with these same arguments at step 1 of this request. "
                            "Its text was masked to save context and is saved at .basalt/artifacts/browser_open_")
    assert third.endswith(".txt. The call was not repeated.")
    saved = third.split("saved at ")[1].split(". The call")[0].split("/")[-1]
    text = (tmp_path / "artifacts" / saved).read_text(encoding="utf-8")
    assert untrusted.normalize(text).startswith('<untrusted_data source="browser_open">\nURL: https://n.example/big') and "word word" in text


def test_a_first_call_that_failed_is_not_counted_as_a_read(tmp_path):
    client = Scripted(calls(("a", "browser_open", {"url": "bad"})), calls(("b", "browser_open", {"url": "bad"})), say("done"))
    agent, ran, _ = make(tmp_path, client, results={"bad": {"error": "the site could not be found"}})
    agent.run("go")
    assert len(ran) == 2 and "Not run again" not in " ".join(tool_texts(agent))


def test_a_first_result_that_left_the_conversation_lets_the_call_run(tmp_path):
    agent, ran, _ = make(tmp_path, Scripted(calls(("a", "browser_open", {"url": "u"})), say("done")))
    agent.run("go")
    guard = agent.repeat_guard
    assert guard.again("browser_open", {"url": "u"}, []) is None             # no such message any more
    assert guard.again("browser_open", {"url": "never read"}, agent.messages) is None


def test_a_repeat_is_not_a_loop_strike_and_does_not_end_the_run(tmp_path):
    steps = [calls(("a", "browser_open", {"url": "u"}))] + [calls((f"r{i}", "browser_open", {"url": "u"})) for i in range(6)] + [say("answer")]
    agent, ran, _ = make(tmp_path, Scripted(*steps))
    agent.run("go")
    assert len(ran) == 1 and agent.stop_reason == FINAL and agent._loop_strikes == 0
    assert len(agent.loop_detector.history) == 1                              # the repeats never reached the detector


def test_a_repeat_is_recorded_as_refused_with_its_reason_and_the_permission_gate_is_not_reached_again(tmp_path):
    asked = []
    permissions = PermissionManager(level=PermissionLevel.AUTO)
    real = permissions.evaluate
    permissions.evaluate = lambda name, effect, args, **kw: asked.append(name) or real(name, effect, args, **kw)
    client = Scripted(calls(("a", "browser_open", {"url": "u"})), calls(("b", "browser_open", {"url": "u"})), say("done"))
    agent, ran, audit = make(tmp_path, client, permissions=permissions)
    agent.run("go")
    assert asked == ["browser_open"] and len(ran) == 1
    by_action = {}
    for rec in audit.records:
        by_action.setdefault(rec["action"], []).append(rec["details"])
    assert [d["outcome"] for d in by_action["tool_executed"]] == ["ok"] and len(by_action["permission_granted"]) == 1
    (repeat,) = by_action["tool_repeat"]
    assert repeat["tool"] == "browser_open" and repeat["outcome"] == "refused" and "Not run again" in repeat["reason"]
    assert permissions.denied == 0
    assert audit.verify_integrity() == (True, None)


def test_the_result_of_a_repeat_is_a_skipped_call_that_is_not_an_error(tmp_path):
    client = Scripted(calls(("a", "browser_open", {"url": "u"})), calls(("b", "browser_open", {"url": "u"})), say("done"))
    agent, ran, _ = make(tmp_path, client)
    agent.run("go")
    repeat = agent.turns[1].tool_calls[0]
    assert repeat.decision == "skipped" and repeat.is_error is False and repeat.arguments == {"url": "u"}


def test_other_tools_are_not_guarded(tmp_path):
    client = Scripted(calls(("a", "probe", {"n": 1})), calls(("b", "probe", {"n": 1})), say("done"))
    agent, ran, _ = make(tmp_path, client)
    agent.run("go")
    assert ran == [("probe", 1)] * 2


def test_a_call_made_for_the_person_goes_through_unguarded(tmp_path):
    agent, ran, _ = make(tmp_path, Scripted())
    agent.add_user_message("go")
    assert agent.call_tool("browser_open", {"url": "u"})[0] and agent.call_tool("browser_open", {"url": "u"})[0]
    assert len(ran) == 2


def test_a_fork_has_a_guard_of_its_own(tmp_path):
    agent, ran, _ = make(tmp_path, Scripted(calls(("a", "browser_open", {"url": "u"})), say("done")))
    agent.run("go")
    assert agent.fork().repeat_guard is not agent.repeat_guard and agent.fork().repeat_guard.again("browser_open", {"url": "u"}, agent.messages) is None


def test_the_covered_tools_are_the_web_reads():
    assert WEB_READ_TOOLS == {"browser_open", "browse_url", "search_web", "find_online_capability"}
    guard = RepeatGuard()
    assert guard.signature("read_file", {"path": "x"}) == "" and guard.signature("browse_url", {"url": "x"}) != ""
    assert guard.signature("browse_url", {"url": "x", "a": 1}) == guard.signature("browse_url", {"a": 1, "url": "x"})
