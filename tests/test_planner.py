import pytest
from pathlib import Path
from unittest.mock import MagicMock

from harness.planner import (
    PlanDAG,
    PlanNode,
    NodeType,
    Assignee,
    EventBuffer,
    JevTriageGatekeeper,
)
from harness.integrations.call_api_client import CallAPICallClient


def test_plandag_structure_and_topological_sort():
    dag = PlanDAG(plan_id="plan_cervinia", title="Weekend a Cervinia", target_type="occasional")

    n1 = PlanNode(id="n1", type=NodeType.SENSOR, title="Meteo", description="Check weather", assignee=Assignee.AGENT)
    n2 = PlanNode(id="n2", type=NodeType.ACTION, title="Hotel", description="Book room", assignee=Assignee.AGENT)
    n3 = PlanNode(id="n3", type=NodeType.DECISION, title="Cena", description="Ristorante o baita", assignee=Assignee.AGENT)
    n4 = PlanNode(id="n4", type=NodeType.MERGE, title="Sync", description="Partenza", assignee=Assignee.AGENT)

    dag.add_node(n1)
    dag.add_node(n2)
    dag.add_node(n3)
    dag.add_node(n4)

    dag.add_edge("n1", "n2")
    dag.add_edge("n2", "n3")
    dag.add_edge("n3", "n4")

    order = dag.topological_order()
    assert order == ["n1", "n2", "n3", "n4"]


def test_plandag_cycle_detection():
    dag = PlanDAG(plan_id="cycle_test", title="Cycle", target_type="occasional")
    dag.add_node(PlanNode(id="a", type=NodeType.ACTION, title="A", description="", assignee=Assignee.AGENT))
    dag.add_node(PlanNode(id="b", type=NodeType.ACTION, title="B", description="", assignee=Assignee.AGENT))

    dag.add_edge("a", "b")
    with pytest.raises(ValueError, match="Cycle detected"):
        dag.add_edge("b", "a")


def test_plandag_serialization_and_human_fallback():
    dag = PlanDAG(plan_id="p1", title="Trip", target_type="occasional")
    dag.add_node(PlanNode(id="wash", type=NodeType.ACTION, title="Lava piatti", description="Manuale", assignee=Assignee.AGENT))

    # AI cannot do dishes -> human fallback
    dag.reassign_to_user("wash", reason="Physical action outside software capability")
    assert dag.nodes["wash"].assignee == Assignee.USER
    assert dag.nodes["wash"].state_variables["fallback_reason"] == "Physical action outside software capability"

    data = dag.to_dict()
    reconstructed = PlanDAG.from_dict(data)
    assert reconstructed.plan_id == "p1"
    assert reconstructed.nodes["wash"].assignee == Assignee.USER


def test_event_buffer_batching():
    buf = EventBuffer(max_batch_size=3)
    assert not buf.add_event("event 1")
    assert not buf.add_event("event 2")
    # 3rd event triggers batch threshold
    assert buf.add_event("event 3")

    flushed = buf.flush()
    assert len(flushed) == 3
    assert len(buf) == 0


def test_jev_gatekeeper_triage():
    gate = JevTriageGatekeeper()
    plan_card = {"title": "Cervinia ski trip", "dates": "2026-10-24"}

    # 1. Critical event (road closure / strike) wakes System 2
    critical_events = [
        "Sciopero improvviso del personale funivie Cervinia",
        "Chiusura strada regionale per valanga",
    ]
    res_critical = gate.triage_offline(plan_card, critical_events)
    assert res_critical.wake_system_2 is True
    assert res_critical.has_blocker_prob >= 0.85
    assert len(res_critical.reasons) > 0

    # 2. Neutral events do NOT wake System 2 (0% GPU cost)
    neutral_events = [
        "Nuovo museo aperto a Parigi",
        "Ricetta torta alle mele pubblicata online",
    ]
    res_neutral = gate.triage_offline(plan_card, neutral_events)
    assert res_neutral.wake_system_2 is False
    assert res_neutral.impact_scope == "ZERO_EVENTS_IMPACTFUL"


def _jev_answer(noul, choice, input_tokens=1144, output_tokens=120):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "has_blocker": {"type": "noul", "noul": noul},
            "batch_impact_scope": {"type": "choice", "choice": choice, "confidence": 0.96},
        },
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def test_jev_parse_real_response_shape():
    gate = JevTriageGatekeeper(api_key="k")

    # One critical event among harmless ones: Noul alone must wake System 2
    res = gate.parse_response(_jev_answer(0.94, "MIXED_SOME_IMPACTFUL"), latency_ms=254.0)
    assert res.wake_system_2 is True
    assert res.has_blocker_prob == 0.94
    assert res.token_usage == 1264
    assert res.source == "cloud"

    # Background noise stays silent
    quiet = gate.parse_response(_jev_answer(0.06, "ZERO_EVENTS_IMPACTFUL"))
    assert quiet.wake_system_2 is False


def test_jev_malformed_response_is_rejected_and_flagged(monkeypatch):
    gate = JevTriageGatekeeper(api_key="k")
    for bad in ({}, {"answers": {}}, {"answers": {"has_blocker": {"type": "noul"}}},
                _jev_answer(1.7, "ZERO_EVENTS_IMPACTFUL")):
        with pytest.raises((ValueError, KeyError)):
            gate.parse_response(bad)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"answers": {}}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    res = gate.triage_event_batch({"title": "t"}, ["Nuovo museo aperto a Parigi"])
    assert res.source == "error"
    assert "jev unavailable" in res.reasons[0]


def test_call_api_client_graceful_offline():
    client = CallAPICallClient(endpoint="http://127.0.0.1:59998", timeout=0.1)
    assert not client.is_available()

    res = client.make_phone_call("+390166123456", "Verifica apertura hotel")
    assert res["status"] == "failed"
    assert "unavailable" in res["error"]
    assert not client.hangup("call_123")
    assert client.get_call_transcript("call_123") is None
