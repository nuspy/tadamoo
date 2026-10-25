"""The council: members that may be different models, a chair, and a tool that exists only when the person set it up."""
import threading

import pytest

from harness.client import BasaltError
from harness.council import CHAIR, MEMBERS, Council, configured, offer_council, render
from harness.llm.credentials import FileCredentials
from harness.llm.store import SecureRegistry
from harness.tools import ToolRegistry


class Fake:
    """A model that answers with a line of its own and remembers what it was asked."""

    def __init__(self, name, fail=None, empty=False):
        self.name, self.fail, self.empty, self.asked = name, fail, empty, []

    def chat_completion(self, messages, **kw):
        self.asked.append((messages, kw))
        if self.fail:
            raise self.fail
        return {"choices": [{"message": {"content": "" if self.empty else f"{self.name} says: use the cache"}}]}


def world(**kw):
    clients = {f: Fake(f, **kw.get(f, {})) for f in MEMBERS + (CHAIR,)}
    return clients, Council(lambda function: clients[function])


def test_three_members_answer_in_their_own_roles_and_the_chair_sums_up():
    clients, council = world()
    out = council.convene("Should we cache?", "the table has 5 rows")
    assert [a["member"] for a in out["answers"]] == list(MEMBERS) and all(a["text"].endswith("use the cache") for a in out["answers"])
    assert [a["role"] for a in out["answers"]] == ["answers", "reviews", "proposes another way"]
    system, user = clients["council_2"].asked[0][0]
    assert "what is wrong" in system["content"] and "<question>\nShould we cache?" in user["content"] and "<context>\nthe table has 5 rows" in user["content"]
    chair_system, chair_user = clients[CHAIR].asked[0][0]
    assert "untrusted" in chair_system["content"] and chair_user["content"].count("<answer from=") == 3
    assert out["verdict"] == "council_chair says: use the cache"


def test_the_members_work_at_the_same_time():
    gate = threading.Barrier(3, timeout=10)

    class Slow(Fake):
        def chat_completion(self, messages, **kw):
            if self.name in MEMBERS:
                gate.wait()                                         # all three must be inside at once, or this times out
            return super().chat_completion(messages, **kw)
    clients = {f: Slow(f) for f in MEMBERS + (CHAIR,)}
    assert len(Council(lambda f: clients[f]).convene("q")["answers"]) == 3


def test_a_member_that_fails_is_reported_and_the_rest_go_on():
    clients, council = world(council_2={"fail": BasaltError(503, "the provider is down")})
    out = council.convene("q")
    assert out["answers"][1]["error"] == "the provider is down" and out["verdict"]
    text = render(out)
    assert "council_2 (reviews): [no answer: the provider is down]" in text and "Chair:" in text


def test_with_fewer_than_two_answers_there_is_no_chair():
    clients, council = world(council_2={"fail": RuntimeError("x")}, council_3={"empty": True})
    out = council.convene("q")
    assert out["verdict"] == "" and clients[CHAIR].asked == []
    assert "Fewer than two members answered" in render(out) and "[no answer: no answer]" in render(out)


def test_a_chair_that_fails_does_not_lose_the_answers():
    clients, council = world(council_chair={"fail": RuntimeError("boom")})
    out = council.convene("q")
    assert "the chair could not answer: RuntimeError" in out["verdict"] and len([a for a in out["answers"] if a.get("text")]) == 3


def test_a_long_question_and_context_are_cut_and_what_members_wrote_is_not_followed_by_the_chair():
    clients, council = world()
    council.convene("q" * 5000, "c" * 9000)
    user = clients["council_1"].asked[0][0][1]["content"]
    assert user.count("q") <= 2000 + 20 and user.count("c") <= 6000 + 20


@pytest.fixture
def llm(tmp_path):
    return SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))


def test_the_tool_exists_only_when_the_person_pointed_a_council_function_somewhere(llm):
    tools = ToolRegistry()
    assert configured(llm) is False and offer_council(tools, lambda f: Fake(f), llm) is False and tools.get("ask_council") is None
    pid = llm.add({"type": "basalt"})["id"]
    llm.assign_checked("council_2", pid)
    assert configured(llm) is True and offer_council(tools, lambda f: Fake(f), llm) is True
    assert tools.get("ask_council").untrusted and tools.get("ask_council").effect.value == "pure"


def test_the_tool_answers_with_the_chair_and_the_members_and_asks_for_a_question(llm):
    pid = llm.add({"type": "basalt"})["id"]
    llm.assign_checked("council_1", pid)
    tools = ToolRegistry()
    clients = {f: Fake(f) for f in MEMBERS + (CHAIR,)}
    offer_council(tools, lambda f: clients[f], llm)
    out = tools.execute("ask_council", {"question": "Which index?", "context": "a table"})
    assert out.startswith("Chair:\ncouncil_chair says") and "council_1 (answers)" in out and "council_3 (proposes another way)" in out
    assert "Give the question" in tools.execute("ask_council", {"question": "  "})


def test_the_comparison_set_loads_and_its_checks_tell_a_flaw_from_a_sound_file():
    from harness.eval.tasks import load_task_set, run_checks
    meta, tasks = load_task_set("council")
    assert meta["name"] == "council" and len(tasks) == 6
    by_id = {t.id: t for t in tasks}
    sound, flawed = by_id["sound_code"], by_id["retry_without_idempotency"]
    ok = lambda task, answer: run_checks(task, __import__("pathlib").Path("."), answer, []) == []
    assert ok(sound, "DEPLOY\nThe function is small and tested.") and not ok(sound, "HOLD\nNeeds more tests.")
    assert ok(flawed, "HOLD\nA timeout can charge twice: no idempotency key.") and not ok(flawed, "DEPLOY\nLooks fine.")
    assert not ok(flawed, "HOLD\nIt is untidy.")                                    # holding for the wrong reason does not count


def test_the_agent_gets_the_tool_only_with_the_flag_and_a_council_set_up(tmp_path, monkeypatch):
    from harness.stack import AgentConfig, build_agent
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    monkeypatch.setattr("harness.llm.registry", lambda path=None: reg)
    ws = tmp_path / "ws"
    ws.mkdir()

    def built(**kw):
        return build_agent(AgentConfig(workspace=str(ws), endpoint="http://127.0.0.1:1", data_dir=str(tmp_path / "d"), use_memory=False, **kw))
    assert "ask_council" not in built(council=True).agent.tools.names()               # asked for, but nothing is set up
    reg.assign_checked("council_1", reg.add({"type": "basalt"})["id"])
    assert "ask_council" not in built().agent.tools.names()                          # set up, but not asked for
    assert "ask_council" in built(council=True).agent.tools.names()
