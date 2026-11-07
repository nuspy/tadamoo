"""A conversation's day budget (serve/session_budget.py) and its digest: the last things that happened to or from it."""
import pytest

from serve.agent_runs import RunError
from serve.tests.test_agent_sessions import kinds, make, own_data_folder, until, ws  # noqa: F401  (fixtures and helpers)


def test_a_budget_is_set_at_creation_or_changed_later(make, ws):
    mgr = make()
    sid = mgr.create(ws, budget_day_tokens=1000)["id"]
    assert mgr.get(sid)["budget_day_tokens"] == 1000
    assert mgr.set_budget(sid, 500)["budget_day_tokens"] == 500
    assert mgr.set_budget(sid, None)["budget_day_tokens"] is None
    for bad in (0, -1, True, 1.5, "100"):
        with pytest.raises(RunError) as e:
            mgr.set_budget(sid, bad)
        assert e.value.code == "bad_budget"


def test_a_conversation_without_a_budget_is_never_over(make, ws):
    mgr = make()
    sid = mgr.create(ws)["id"]
    mgr.send(sid, "hello")
    until(lambda: "done" in kinds(mgr, sid))
    assert mgr.over_budget(sid) is False


def test_a_delivery_to_a_conversation_over_its_budget_is_dead(make, ws):
    mgr = make()
    sid = mgr.create(ws, alias="night", budget_day_tokens=1)["id"]
    mgr.send(sid, "hello")
    until(lambda: "done" in kinds(mgr, sid))
    assert mgr.over_budget(sid) is True
    item = mgr.deliver("night", "check in", provenance="cron")["delivery"]
    assert item["state"] == "dead" and item["reason"] == "budget"


def test_the_status_shows_the_budget_and_what_was_used_today(make, ws):
    mgr = make()
    sid = mgr.create(ws, alias="night", budget_day_tokens=5000)["id"]
    assert mgr.status("night")["budget_day_tokens"] == 5000 and mgr.status("night")["used_today"] == 0
    mgr.send(sid, "hello")
    until(lambda: "done" in kinds(mgr, sid))
    assert mgr.status("night")["used_today"] > 0


def test_the_digest_lines_up_what_the_conversation_said_and_received_in_order(make, ws):
    mgr = make()
    boss = mgr.create(ws, alias="boss")["id"]
    log = mgr.create(ws, alias="log")["id"]
    mgr.send(log, "primo")
    until(lambda: "done" in kinds(mgr, log))
    item = mgr.deliver("log", "append: sold", sender=boss)["delivery"]
    until(lambda: mgr.deliveries.get(item["id"])["state"] == "delivered")
    until(lambda: kinds(mgr, log).count("done") == 2)
    digest = mgr.digest("log")
    assert digest["alias"] == "log"
    assert any("you: primo" in line for line in digest["lines"])
    assert any("done: echo: primo" in line for line in digest["lines"])
    assert any("from agent:boss: append: sold" in line for line in digest["lines"])
    assert digest["pending_messages"] == 0 and digest["dead_letters"] == 0


def test_the_digest_shows_a_message_this_conversation_sent_and_its_outcome(make, ws):
    mgr = make()
    boss = mgr.create(ws, alias="boss")["id"]
    mgr.create(ws, alias="log")
    mgr.deliver("log", "x", sender=boss)
    digest = mgr.digest("boss")
    assert any("sent to log" in line for line in digest["lines"])


def test_the_digest_is_cut_to_the_count_asked_and_refuses_an_unknown_conversation(make, ws):
    mgr = make()
    sid = mgr.create(ws, alias="night")["id"]
    for i in range(5):
        mgr.send(sid, f"m{i}")
        until(lambda i=i: kinds(mgr, sid).count("done") == i + 1)
    assert len(mgr.digest("night", 3)["lines"]) == 3
    with pytest.raises(RunError) as e:
        mgr.digest("nowhere")
    assert e.value.code == "gone"
