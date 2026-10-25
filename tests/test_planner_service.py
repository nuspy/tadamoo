"""The server's side of the planner (PlanService) with local fakes: one filter for the whole process, one re-planning at a time,
the settings that steer them. No network beyond loopback, no model, no cloud."""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.planner import PlanDAG, PlanNode
from serve.plans import PlanService
from test_planner import CARD, FakeJev, jev_answer  # noqa: E402

PLAN = {"plan_id": "cervinia", "title": "Weekend a Cervinia", "target_type": "occasional",
        "nodes": {"hotel": {"id": "hotel", "type": "action", "title": "Hotel", "description": "", "assignee": "agent", "dependencies": [], "outputs": ["partenza"]},
                  "partenza": {"id": "partenza", "type": "merge", "title": "Partenza", "description": "", "assignee": "agent", "dependencies": ["hotel"], "outputs": []}}}
REPLAN = {"diagnosis": "Lo sciopero ferma il treno di Gianni.", "affected_nodes": ["partenza"],
          "alternatives": [{"title": "Parti in auto", "description": "Viaggio in auto", "diff": {"update_nodes": {"partenza": {"title": "Partenza in auto"}}}}],
          "recommended": 0, "human_fallbacks": []}


class FakeModel:
    """The local model: answers a re-planning request when released, and counts how often it was asked."""

    def __init__(self, answer=None, hold=False):
        self.answer = answer or REPLAN
        self.calls = 0
        self.gate = threading.Event()
        if not hold:
            self.gate.set()
        self.asked = threading.Event()

    def chat_completion(self, **kwargs):
        self.calls += 1
        self.asked.set()
        assert self.gate.wait(10)
        return {"choices": [{"message": {"content": json.dumps(self.answer)}}]}


def make_service(tmp_path, model=None, **kw):
    svc = PlanService(tmp_path / "planner", client_factory=(lambda: model) if model else None, **kw)
    svc.registry = False                                   # no provider registry in these tests: the key comes from the environment
    return svc


@pytest.fixture
def jev():
    made = []

    def make(**kw):
        f = FakeJev(**kw)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


def wait_for(cond, timeout=10.0):
    done = threading.Event()
    end = threading.Timer(timeout, done.set)
    end.start()
    try:
        while not cond():
            if done.wait(0.02):
                return cond()
        return True
    finally:
        end.cancel()


# -- one filter for the whole process --------------------------------------------------------------------

def test_the_gatekeeper_is_one_object_whose_telemetry_adds_up_and_can_be_read(tmp_path):
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD})
    assert svc.gatekeeper() is svc.gatekeeper()
    assert svc.telemetry()["triage"]["calls"] == 0
    svc.triage("cervinia", ["Nuovo museo a Parigi"])
    svc.triage("cervinia", ["Sciopero dei treni"])
    t = svc.telemetry()["triage"]
    assert (t["calls"], t["offline"], t["cloud"], t["fell_back"], t["wakes"]) == (2, 2, 0, 0, 1)


def test_telemetry_counts_the_cloud_the_local_filter_and_the_falls_back(tmp_path, jev):
    fake = jev(answer=jev_answer(0.1, "ZERO_EVENTS_IMPACTFUL"))
    broken = jev(answer={"answers": {}})
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD})
    svc.update_settings({"cloud_triage": True})
    where = {"url": fake.url}
    svc._jev_from_registry = lambda: ("sk-test", where["url"], "jev-latest")      # the provider the person chose: a fake on this machine
    svc.triage("cervinia", ["Nuovo museo"])
    where["url"] = broken.url
    svc.triage("cervinia", ["Sciopero"])
    svc.update_settings({"cloud_triage": False})
    svc.triage("cervinia", ["Ricetta"])
    t = svc.telemetry()["triage"]
    assert (t["calls"], t["cloud"], t["fell_back"], t["offline"]) == (3, 1, 1, 1) and t["tokens"] == 1264


def test_the_settings_reach_the_same_gatekeeper_including_the_gate(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    svc = make_service(tmp_path)
    gate = svc.gatekeeper()
    assert svc.settings()["gate_mode"] == "or" and gate.gate_mode == "or"
    svc.update_settings({"gate_mode": "and", "card_fields": ["titolo"]})
    assert svc.gatekeeper() is gate and gate.gate_mode == "and" and gate.card_fields == ["titolo"]
    for bad in ("xor", None, 1):
        with pytest.raises(ValueError, match="gate_mode"):
            svc.update_settings({"gate_mode": bad})
    assert svc.gatekeeper().gate_mode == "and"


# -- one re-planning per plan ----------------------------------------------------------------------------

def test_two_close_triages_start_one_replanning_and_the_second_is_told_it_is_in_progress(tmp_path):
    model = FakeModel(hold=True)
    svc = make_service(tmp_path, model)
    svc.create({"plan": PLAN, "card": CARD})
    first = svc.triage("cervinia", ["Sciopero dei treni"], replan=True)
    assert first["replan_state"] == "started" and first["replanning"] is True
    assert model.asked.wait(5)
    second = svc.triage("cervinia", ["Chiusura strada per frana"], replan=True)
    assert second["replan_state"] == "in_progress" and second["replanning"] is True and svc.snapshot("cervinia")["replanning"] is True
    assert svc.telemetry()["replanning"] == ["cervinia"]
    model.gate.set()
    assert wait_for(lambda: not svc.snapshot("cervinia")["replanning"])
    snap = svc.snapshot("cervinia")
    assert model.calls == 1 and len(snap["proposals"]) == 1                    # one request to the model, one set of proposals
    third = svc.triage("cervinia", ["Sciopero ancora"], replan=True)           # once it is over, a new one may start
    assert third["replan_state"] == "started"
    assert wait_for(lambda: model.calls == 2 and not svc.snapshot("cervinia")["replanning"])


def test_replanning_says_why_it_did_not_start(tmp_path):
    svc = make_service(tmp_path)
    svc.create({"plan": PLAN, "card": CARD})
    assert svc.triage("cervinia", ["Nuovo museo"], replan=True)["replan_state"] == "not_needed"
    assert svc.triage("cervinia", ["Sciopero"], replan=False)["replan_state"] == "not_asked"
    no_model = svc.triage("cervinia", ["Sciopero"], replan=True)
    assert no_model["replan_state"] == "unavailable" and "no model" in no_model["replan_error"] and no_model["replanning"] is False


def test_a_failing_model_frees_the_plan_for_the_next_try(tmp_path):
    class Broken:
        def chat_completion(self, **kw):
            raise RuntimeError("model gone")

    svc = make_service(tmp_path, Broken())
    svc.create({"plan": PLAN, "card": CARD})
    assert svc.triage("cervinia", ["Sciopero"], replan=True)["replan_state"] == "started"
    assert wait_for(lambda: not svc.snapshot("cervinia")["replanning"])
    assert any(e["type"] == "plan:replan_failed" for e in svc.events_since("cervinia", 0))
    assert svc.triage("cervinia", ["Sciopero"], replan=True)["replan_state"] == "started"


def test_the_ids_of_the_servers_own_routes_cannot_be_plan_ids(tmp_path):
    svc = make_service(tmp_path)
    for reserved in ("events", "telemetry"):
        with pytest.raises(ValueError, match="kept for the server"):
            svc.create({"plan": {**PLAN, "plan_id": reserved}})
