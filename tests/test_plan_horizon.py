"""Long-term plans against short ones: what each says about time and money, what the arithmetic finds, and what the model is only asked."""
import datetime
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.planner import PlanDAG, PlanProfile, alignment
from harness.planner.alignment import meaning_conflicts, rule_conflicts, view
from serve.plans import PlanService

LONG = {"goals": ["Keep a healthy knee", "Save for a house"], "review_every_days": 14, "last_review": "2026-10-20",
        "budget": {"amount": "500", "currency": "eur", "period": "month"},
        "commitments": [{"text": "Physiotherapy course", "from": "2026-11-02", "to": "2026-11-27"}]}


def plan(plan_id, title, kind, **profile):
    return view({"plan_id": plan_id, "title": title, "target_type": kind, "nodes": {"a": {"title": "Book it"}}}, PlanProfile.from_dict(profile).to_dict())


# -- what a plan says ----------------------------------------------------------------------------------

def test_a_profile_is_checked_and_kept_as_written():
    p = PlanProfile.from_dict(LONG)
    assert p.budget == {"amount": "500.00", "currency": "EUR", "period": "month"} and p.review_every_days == 14
    assert PlanProfile.from_dict(p.to_dict()).to_dict() == p.to_dict()
    assert PlanProfile.from_dict(None).to_dict()["goals"] == []


@pytest.mark.parametrize("bad", [
    {"goals": "get rich"}, {"goals": ["x"] * 13}, {"review_every_days": 31}, {"review_every_days": 0}, {"review_every_days": True},
    {"budget": {"amount": "-5", "currency": "EUR"}}, {"budget": {"amount": "10.005", "currency": "EUR"}}, {"budget": {"amount": "10", "currency": "euro"}},
    {"budget": {"amount": "10", "currency": "EUR", "period": "year"}}, {"cost": 5}, {"starts_on": "tomorrow"}, {"starts_on": "2026-12-02", "ends_on": "2026-12-01"},
    {"commitments": [{"text": "x", "from": "2026-11-05", "to": "2026-11-01"}]}, {"commitments": [{"from": "2026-11-01", "to": "2026-11-02"}]}, {"nothing": 1}])
def test_a_wrong_profile_says_what_to_change(bad):
    with pytest.raises(ValueError):
        PlanProfile.from_dict(bad)


def test_a_long_plan_is_due_when_not_looked_at_for_longer_than_it_said():
    p = PlanProfile.from_dict({"review_every_days": 14, "last_review": "2026-10-01"})
    assert p.review_due(datetime.date(2026, 10, 20)) and not p.review_due(datetime.date(2026, 10, 15))
    assert PlanProfile.from_dict({}).review_due()                                    # never looked at


# -- the arithmetic ------------------------------------------------------------------------------------

def test_days_that_fall_in_a_period_already_taken_are_a_conflict():
    found = rule_conflicts([plan("health", "Health", "constant", **LONG), plan("ski", "Ski weekend", "occasional", starts_on="2026-11-20", ends_on="2026-11-22"),
                            plan("rome", "Rome", "occasional", starts_on="2026-12-05", ends_on="2026-12-07")])
    assert [(c["kind"], c["plan_id"], c["with_plan_id"], c["severity"], c["source"]) for c in found] == [("time", "ski", "health", "conflict", "rules")]
    assert "Physiotherapy course" in found[0]["text"] and "2026-11-20" in found[0]["text"]


def test_the_edges_of_a_period_count():
    assert rule_conflicts([plan("h", "H", "constant", **LONG), plan("a", "A", "occasional", starts_on="2026-11-27", ends_on="2026-11-30")])
    assert not rule_conflicts([plan("h", "H", "constant", **LONG), plan("a", "A", "occasional", starts_on="2026-11-28", ends_on="2026-11-30")])


def test_costs_in_one_month_are_added_against_the_monthly_budget():
    views = [plan("h", "H", "constant", **LONG),
             plan("a", "A", "occasional", starts_on="2026-12-01", ends_on="2026-12-02", cost={"amount": "300", "currency": "EUR"}),
             plan("b", "B", "occasional", starts_on="2026-12-20", ends_on="2026-12-21", cost={"amount": "250", "currency": "EUR"}),
             plan("c", "C", "occasional", starts_on="2027-01-10", ends_on="2027-01-11", cost={"amount": "250", "currency": "EUR"})]
    found = rule_conflicts(views)
    assert sorted(c["plan_id"] for c in found) == ["a", "b"] and all(c["kind"] == "budget" for c in found)            # December: 550 over 500; January alone is fine
    assert "550.00 EUR in 2026-12" in found[0]["text"]


def test_a_total_budget_adds_everything_and_other_currencies_and_undated_plans_are_not_mixed_in():
    total = {**LONG, "budget": {"amount": "500", "currency": "EUR", "period": "total"}, "commitments": []}
    views = [plan("h", "H", "constant", **total),
             plan("a", "A", "occasional", starts_on="2026-12-01", ends_on="2026-12-02", cost={"amount": "300", "currency": "EUR"}),
             plan("b", "B", "occasional", starts_on="2027-05-01", ends_on="2027-05-02", cost={"amount": "250", "currency": "EUR"}),
             plan("usd", "Usd", "occasional", starts_on="2027-05-01", ends_on="2027-05-02", cost={"amount": "9000", "currency": "USD"})]
    assert sorted(c["plan_id"] for c in rule_conflicts(views)) == ["a", "b"]
    monthly = [plan("h", "H", "constant", **LONG), plan("u1", "U1", "occasional", cost={"amount": "300", "currency": "EUR"}),
               plan("u2", "U2", "occasional", cost={"amount": "300", "currency": "EUR"}), plan("u3", "U3", "occasional", cost={"amount": "600", "currency": "EUR"})]
    assert [c["plan_id"] for c in rule_conflicts(monthly)] == ["u3"]                   # undated plans are judged one by one


def test_without_a_long_plan_or_a_budget_or_dates_nothing_is_found():
    assert rule_conflicts([plan("a", "A", "occasional", starts_on="2026-11-20", ends_on="2026-11-22")]) == []
    assert rule_conflicts([plan("h", "H", "constant", goals=["x"]), plan("a", "A", "occasional", cost={"amount": "9", "currency": "EUR"})]) == []


# -- what the model is asked ---------------------------------------------------------------------------

def test_the_model_is_given_the_aims_and_the_other_plan_and_its_checks_are_only_checks():
    views = [plan("health", "Health", "constant", **LONG), plan("ski", "Ski weekend", "occasional", starts_on="2026-12-05", ends_on="2026-12-07")]
    seen = []

    def ask(messages):
        seen.append(messages)
        return 'Sure.\n```json\n{"conflicts": [{"with_plan_id": "health", "goal": "Keep a healthy knee", "text": "Skiing risks the knee."}, ' \
               '{"with_plan_id": "ghost", "text": "x"}, {"with_plan_id": "health", "text": ""}]}\n```'
    found = meaning_conflicts(views[1], views, ask)
    assert [(c["kind"], c["severity"], c["source"], c["with_plan_id"]) for c in found] == [("meaning", "check", "model", "health")]
    prompt = seen[0][1]["content"]
    assert "Keep a healthy knee" in prompt and "Ski weekend" in prompt and "2026-12-05" in prompt and "Physiotherapy course" in prompt


@pytest.mark.parametrize("answer", ["", "no json here", "{broken", '{"conflicts": "none"}', '{"conflicts": [1, 2]}'])
def test_an_answer_that_cannot_be_used_gives_no_checks(answer):
    views = [plan("h", "H", "constant", **LONG), plan("a", "A", "occasional")]
    assert meaning_conflicts(views[1], views, lambda m: answer) == []


def test_with_no_long_plan_the_model_is_not_asked():
    asked = []
    assert meaning_conflicts(plan("a", "A", "occasional"), [plan("a", "A", "occasional")], lambda m: asked.append(m)) == [] and asked == []


# -- the service ---------------------------------------------------------------------------------------

def dag(plan_id, title, kind):
    return {"plan_id": plan_id, "title": title, "target_type": kind, "nodes": {}}


class FakeClient:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def chat_completion(self, messages, **kw):
        self.calls += 1
        return {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}


@pytest.fixture
def service(tmp_path):
    return PlanService(tmp_path / "planner", client_factory=lambda: FakeClient(json.dumps(
        {"conflicts": [{"with_plan_id": "health", "goal": "Keep a healthy knee", "text": "Skiing risks the knee."}]})))


def test_plans_are_made_with_a_profile_and_listed_with_what_they_are_and_what_clashes(service):
    service.create({"plan": dag("health", "Health", "constant"), "profile": LONG})
    snap = service.create({"plan": dag("ski", "Ski weekend", "occasional"), "profile": {"starts_on": "2026-11-20", "ends_on": "2026-11-22"}})
    assert snap["profile"]["starts_on"] == "2026-11-20" and snap["review_due"] is False
    rows = {r["plan_id"]: r for r in service.list()}
    assert rows["ski"]["target_type"] == "occasional" and rows["health"]["target_type"] == "constant"
    assert rows["ski"]["conflicts"][0]["with_plan_id"] == "health" and rows["health"]["conflicts"][0]["plan_id"] == "ski"
    assert rows["health"]["review_due"] == ((datetime.date.today() - datetime.date(2026, 10, 20)).days > 14)


def test_a_conflict_is_announced_on_the_plans_stream_when_the_plan_is_made_or_its_days_change(service):
    service.create({"plan": dag("health", "Health", "constant"), "profile": LONG})
    service.create({"plan": dag("ski", "Ski weekend", "occasional"), "profile": {"starts_on": "2026-12-05", "ends_on": "2026-12-07"}})
    assert not [e for e in service.events_since("ski", 0) if e["type"] == "plan:alignment"]
    result = service.handle_event("ski", {"type": "plan:update_meta", "changes": {"profile": {"starts_on": "2026-11-20", "ends_on": "2026-11-22"}}})
    assert result["ok"]
    told = [e for e in service.events_since("ski", 0) if e["type"] == "plan:alignment"]
    assert len(told) == 1 and told[0]["conflicts"][0]["kind"] == "time"


def test_a_bad_profile_is_refused_and_changes_nothing(service):
    service.create({"plan": dag("ski", "Ski weekend", "occasional")})
    result = service.handle_event("ski", {"type": "plan:update_meta", "changes": {"profile": {"cost": {"amount": "x", "currency": "EUR"}}}})
    assert result["ok"] is False and service.snapshot("ski")["profile"]["cost"] is None


def test_the_model_is_asked_only_on_request_and_its_reading_goes_out_of_date(service):
    service.create({"plan": dag("health", "Health", "constant"), "profile": LONG})
    service.create({"plan": dag("ski", "Ski weekend", "occasional"), "profile": {"starts_on": "2026-12-05", "ends_on": "2026-12-07"}})
    asked_of = []

    def factory():
        client = FakeClient(json.dumps({"conflicts": [{"with_plan_id": "health", "text": "Skiing risks the knee."}]}))
        asked_of.append(client)
        return client
    service.client_factory = factory
    questions = lambda: sum(c.calls for c in asked_of)
    plain = service.alignment("ski")
    assert plain["conflicts"] == [] and plain["checks"] == [] and questions() == 0 and plain["out_of_date"] is False
    asked = service.alignment("ski", model=True)
    assert questions() == 1 and asked["checks"][0]["severity"] == "check" and asked["checked_at"] and asked["out_of_date"] is False
    assert service.alignment("ski")["checks"] == asked["checks"] and questions() == 1                  # kept, not asked again
    service.handle_event("ski", {"type": "plan:update_meta", "changes": {"title": "Ski weekend 2"}})
    assert service.alignment("ski")["out_of_date"] is True


def test_asking_the_model_needs_a_model_and_an_unknown_plan_is_not_found(tmp_path):
    bare = PlanService(tmp_path / "p2")
    bare.create({"plan": dag("a", "A", "occasional")})
    with pytest.raises(ValueError, match="no model"):
        bare.alignment("a", model=True)
    with pytest.raises(KeyError):
        bare.alignment("nothing")


def test_the_profile_survives_a_restart(service, tmp_path):
    service.create({"plan": dag("health", "Health", "constant"), "profile": LONG})
    again = PlanService(tmp_path / "planner")
    assert again.snapshot("health")["profile"]["goals"] == LONG["goals"]
