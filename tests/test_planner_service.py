"""The server's side of the planner (PlanService) with local fakes: one filter for the whole process, one re-planning at a time,
the settings that steer them. No network beyond loopback, no model, no cloud."""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import PlanDAG, PlanNode
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
    svc.update_settings({"auto_apply": False})
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


# -- the filter's own settings and the Jev key --------------------------------------------------------------

def test_the_blocker_threshold_the_cloud_timeout_and_the_chunk_size_are_settings(tmp_path, jev, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    svc = make_service(tmp_path)
    s = svc.settings()
    assert (s["blocker_threshold"], s["cloud_timeout"], s["max_events"]) == (0.8, 5.0, 20)
    gate = svc.gatekeeper()
    assert (gate.blocker_threshold, gate.timeout, gate.max_events) == (0.8, 5.0, 20)
    svc.update_settings({"blocker_threshold": 0.95, "cloud_timeout": 30, "max_events": 2})
    assert svc.gatekeeper() is gate and (gate.blocker_threshold, gate.timeout, gate.max_events) == (0.95, 30.0, 2)
    assert svc.settings()["cloud_timeout"] == 30.0                                         # kept across a restart
    assert make_service(tmp_path).settings()["max_events"] == 2
    for bad in ({"blocker_threshold": 1.5}, {"blocker_threshold": "high"}, {"cloud_timeout": 0}, {"cloud_timeout": True}, {"max_events": 0}, {"max_events": 2.5}):
        with pytest.raises(ValueError):
            svc.update_settings(bad)
    fake = jev(answer=jev_answer(0.9, "ZERO_EVENTS_IMPACTFUL"))
    svc.create({"plan": PLAN, "card": CARD})
    svc.update_settings({"cloud_triage": True})
    svc._jev_from_registry = lambda: ("sk-test", fake.url, "jev-latest")
    result = svc.triage("cervinia", ["a", "b", "c"])
    assert len(fake.requests) == 2 and not result["wake_system_2"]                         # 3 events in chunks of 2; 0.9 does not pass 0.95


def _registry(tmp_path):
    from tadamoo.llm.credentials import FileCredentials
    from tadamoo.llm.store import SecureRegistry
    return SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))


def test_the_jev_key_is_saved_in_the_providers_registry_never_shown_and_can_be_removed(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    svc = make_service(tmp_path)
    svc.registry = reg = _registry(tmp_path)
    s = svc.update_settings({"jev_key": "sk-secret-123"})
    assert s["key_saved"] is True and s["key_present"] is False                             # saved, but the registry wants the person's yes for this function
    assert "sk-secret-123" not in json.dumps(s) and "sk-secret-123" not in (tmp_path / "planner" / "planner.json").read_text()
    assert "sk-secret-123" not in (tmp_path / "p.json").read_text() and reg.api_key_for("jev") == "sk-secret-123"
    reg.set_consent("triage", True)
    reg.assign_checked("triage", reg.load()["providers"][0]["id"])
    assert svc.settings()["key_present"] is True and svc.gatekeeper().api_key == "sk-secret-123"
    after = svc.update_settings({"clear_jev_key": True})
    assert after["key_saved"] is False and after["key_present"] is False and reg.api_key_for("jev") == ""
    assert "sk-secret-123" not in (tmp_path / "c.json").read_text()
    for bad in ("", "  ", 5):
        with pytest.raises(ValueError, match="jev_key"):
            svc.update_settings({"jev_key": bad})


def test_with_the_yes_already_given_a_saved_key_is_used_at_once(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    svc = make_service(tmp_path)
    svc.registry = reg = _registry(tmp_path)
    reg.set_consent("triage", True)
    s = svc.update_settings({"jev_key": "sk-secret-456", "cloud_triage": True})
    assert s["key_present"] is True and svc.gatekeeper().cloud_allowed


def test_a_replan_is_applied_by_itself_unless_the_person_turned_that_off(tmp_path):
    svc = make_service(tmp_path, FakeModel())
    assert svc.settings()["auto_apply"] is True and svc.update_settings({"auto_apply": False})["auto_apply"] is False
    with pytest.raises(ValueError, match="auto_apply"):
        svc.update_settings({"auto_apply": "yes"})
    svc.update_settings({"auto_apply": True})
    svc.create({"plan": PLAN, "card": CARD})
    svc.triage("cervinia", ["Sciopero dei treni"], replan=True)
    assert wait_for(lambda: not svc.snapshot("cervinia")["replanning"] and svc.snapshot("cervinia")["plan"]["nodes"]["partenza"]["title"] == "Partenza in auto")
    snap = svc.snapshot("cervinia")
    assert snap["proposals"] == [] and snap["history"][-1]["actor"] == "planner"


def test_a_replan_that_adds_a_step_for_a_third_person_leaves_that_step_for_the_person(tmp_path):
    third = {"id": "gianni", "type": "action", "title": "Avvisa Gianni", "description": "", "assignee": "third_party", "dependencies": ["hotel"], "outputs": []}
    model = FakeModel({**REPLAN, "alternatives": [{"title": "Parti in auto e avvisa", "description": "d",
                                                    "diff": {"update_nodes": {"partenza": {"title": "Partenza in auto"}}, "add_nodes": [third]}}]})
    svc = make_service(tmp_path, model)
    svc.create({"plan": PLAN, "card": CARD})
    svc.triage("cervinia", ["Sciopero dei treni"], replan=True)
    assert wait_for(lambda: model.calls == 1 and not svc.snapshot("cervinia")["replanning"] and svc.snapshot("cervinia")["proposals"])
    snap = svc.snapshot("cervinia")
    assert snap["plan"]["nodes"]["partenza"]["title"] == "Partenza in auto" and "gianni" not in snap["plan"]["nodes"]
    assert [n["id"] for n in snap["proposals"][0]["diff"]["add_nodes"]] == ["gianni"] and snap["proposals"][0]["involves"]
