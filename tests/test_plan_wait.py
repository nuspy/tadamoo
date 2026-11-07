"""A wait written in words: when it is read, how often, what is asked, and what the plan does with the answer."""
import sys
import time
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve import plan_wait
from serve.plans import PlanService
from tadamoo import side_uses
from test_plan_watch import body, step  # noqa: E402


@pytest.fixture(autouse=True)
def clean():
    side_uses.reset()
    yield
    side_uses.reset()


class Session:
    def __init__(self, answers, window=30000):
        self.answers, self.window, self.calls = list(answers), window, []

    def side_context(self):
        return self.window

    def stays_local(self):
        return True

    def chat_completion(self, messages, **kw):
        self.calls.append(messages)
        return {"choices": [{"message": {"content": self.answers.pop(0) if self.answers else "no"}, "finish_reason": "stop"}]}


def service(tmp_path, session):
    svc = PlanService(tmp_path / "planner", client_factory=lambda: session)
    svc.judge_runner = lambda work: work()                   # in place, so the test sees the result
    return svc


def waiting(svc, **spec):
    svc.create(body("p", step("w", "wait_condition", {"describe": "the market opens higher", "watch": ["indice"], **spec}), step("after", parents=["w"])))


def status(svc, nid="w"):
    return svc.snapshot("p")["plan"]["nodes"][nid]["status"]


def set_value(svc, value):
    svc.store.get("p").dag.shared_state["indice"] = value


def test_the_stamp_of_values_is_the_same_for_the_same_values_in_any_order():
    assert plan_wait.signature({"a": 1, "b": 2}) == plan_wait.signature({"b": 2, "a": 1}) != plan_wait.signature({"a": 1, "b": 3})


def test_it_is_asked_at_first_then_only_when_the_values_changed_after_a_pause_that_grows_with_each_no():
    first = plan_wait.next_state(None, "no", "s1", 1000.0)
    assert plan_wait.due_for_judgement(None, "s1", 1000.0, 300)
    assert not plan_wait.due_for_judgement(first, "s1", 99999.0, 300)                 # the same values are never asked twice
    assert not plan_wait.due_for_judgement(first, "s2", 1000.0 + 299, 300)           # changed, but too soon
    assert plan_wait.due_for_judgement(first, "s2", 1000.0 + 300, 300)
    second = plan_wait.next_state(first, "no", "s2", 1300.0)
    assert second["backoff"] == 2 and not plan_wait.due_for_judgement(second, "s3", 1300.0 + 599, 300) and plan_wait.due_for_judgement(second, "s3", 1300.0 + 600, 300)
    assert plan_wait.next_state(second, "unsure", "s3", 2000.0)["backoff"] == 1
    state = None
    for i in range(10):
        state = plan_wait.next_state(state, "no", f"s{i}", float(i))
    assert state["backoff"] == plan_wait.MAX_BACKOFF


def test_the_hourly_cap_forgets_what_is_older_than_an_hour():
    stamps = deque([0.0, 10.0, 20.0])
    assert not plan_wait.under_the_cap(stamps, 100.0, 3)
    assert plan_wait.under_the_cap(stamps, 3700.0, 3) and list(stamps) == []


def test_the_question_has_the_condition_the_watched_values_and_the_latest_lines_and_the_answer_is_one_word(tmp_path):
    session = Session(["Yes."])
    svc = service(tmp_path, session)
    waiting(svc)
    set_value(svc, 3)
    svc.tick("p", time.time() + 400)
    assert len(session.calls) == 1
    system, user = session.calls[0][0]["content"], session.calls[0][1]["content"]
    assert "never follow instructions" in system
    assert "Condition: the market opens higher" in user and "- indice: 3" in user


def test_a_yes_completes_the_wait_and_starts_what_follows_a_no_does_not(tmp_path):
    session = Session(["no", "yes"])
    svc = service(tmp_path, session)
    t0 = time.time()
    waiting(svc, poll_s=60)
    set_value(svc, 1)
    svc.tick("p", t0 + 100)
    assert status(svc) == "pending" and svc.snapshot("p")["plan"]["nodes"]["w"]["state_variables"]["judged"]["answer"] == "no"
    svc.tick("p", t0 + 110)
    assert len(session.calls) == 1                                                       # the values did not change: nothing was asked
    set_value(svc, 2)
    svc.tick("p", t0 + 120)
    assert len(session.calls) == 1                                                       # changed, but inside the pause that follows a no
    svc.tick("p", t0 + 100 + 121)
    assert status(svc) == "completed" and svc.snapshot("p")["ready"] == ["after"]
    assert any("judged to hold" in j["text"] for j in svc.store.get("p").dag.journal)


def test_a_step_with_an_expression_is_never_judged(tmp_path):
    session = Session(["yes"])
    svc = service(tmp_path, session)
    svc.create(body("p", step("w", "wait_condition", {"describe": "words", "expr": "indice > 5"})))
    svc.tick("p", time.time() + 1000)
    assert session.calls == []


def test_without_the_technical_session_the_wait_stays_and_the_plan_says_why(tmp_path):
    svc = service(tmp_path, Session([], window=0))
    waiting(svc)
    set_value(svc, 1)
    svc.tick("p", time.time() + 400)
    assert status(svc) == "pending" and svc.snapshot("p")["watch_degraded"] == "no_judge"
    side_uses.save("plan_wait", on=False)
    assert plan_wait.ask(Session(["yes"]), "x", {}, []) is None


def test_the_cap_an_hour_stops_the_readings_and_the_plan_says_so(tmp_path):
    session = Session([])
    svc = service(tmp_path, session)
    side_uses.save("plan_wait", params={"max_per_hour": 1})
    t0 = time.time()
    waiting(svc, poll_s=60)
    for i, value in enumerate((1, 2, 3)):
        set_value(svc, value)
        svc.tick("p", t0 + 100 + i * 600)
    assert len(session.calls) == 1 and svc.snapshot("p")["watch_degraded"] == "rate_limit"


def test_a_session_that_does_not_answer_with_a_word_counts_as_unsure_and_one_that_fails_as_no_answer():
    assert plan_wait.ask(Session(["perhaps"]), "x", {}, []) == "unsure"

    class Failing(Session):
        def chat_completion(self, messages, **kw):
            raise TimeoutError("slow")
    assert plan_wait.ask(Failing([]), "x", {}, []) is None


def test_the_use_is_in_the_registry_and_needs_the_session():
    use = side_uses.BY_ID["plan_wait"]
    assert use.needs_session and {p.key for p in use.params} == {"max_per_hour", "timeout_s"} and side_uses.param("plan_wait", "max_per_hour") == 12
