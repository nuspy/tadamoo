"""The whole planner path with fakes: an event in the mailbox -> the buffer -> the cloud filter (a fake answering in the real format) ->
the local model (a fake) -> a proposal the person approves -> a step carrying a prepared call -> the Chat's phone_call, which asks.
No cloud, no model, no telephone."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_services import FakeCallAPICall, FakeWikibridge  # noqa: E402
from test_agent_loop import ScriptedClient, call, say  # noqa: E402
from test_planner import CARD, FakeJev, jev_answer  # noqa: E402
from test_planner_service import PLAN, FakeModel, wait_for  # noqa: E402

from harness.agent_loop import AgentLoop  # noqa: E402
from harness.integrations import CallAPICallClient, WikiBridgeClient  # noqa: E402
from harness.integrations._http import ServiceError  # noqa: E402
from harness.integrations.telephony_tools import register_phone_tools  # noqa: E402
from harness.integrations.wiki_tools import register_wiki_tools  # noqa: E402
from harness.permissions import PermissionLevel, PermissionManager  # noqa: E402
from harness.planner_tools import Confirmer, register_planner_tools  # noqa: E402
from harness.tools.registry import ToolRegistry  # noqa: E402
from serve.plans import PlanService  # noqa: E402

CALL = {"number": "+39 0166 949136", "reason": "Verificare che l'hotel abbia camere per il 24", "text": "Chiedere una doppia e una singola"}
REPLAN = {"diagnosis": "Lo sciopero ferma il treno di Gianni: serve sapere se l'hotel regge un arrivo in ritardo.",
          "affected_nodes": ["partenza"],
          "alternatives": [{"title": "Telefona all'hotel", "description": "Chiedere se si può arrivare tardi",
                            "diff": {"add_nodes": [{"id": "chiama_hotel", "type": "action", "title": "Telefona all'hotel", "description": "Arrivo tardivo",
                                                    "assignee": "agent", "dependencies": ["hotel"], "outputs": ["partenza"],
                                                    "call_request": CALL, "wiki_query": "numero e orari della reception dell'hotel"}]}}],
          "recommended": 0, "human_fallbacks": []}


class LocalPlanner:
    """The Chat's planner client, talking to the service in this process instead of over HTTP."""

    def __init__(self, svc):
        self.svc = svc

    def list(self, archived=False):
        return self.svc.list(archived)

    def snapshot(self, plan_id):
        return self.svc.snapshot(plan_id)

    def alignment(self, plan_id, model=False):
        return self.svc.alignment(plan_id, model)

    def event(self, plan_id, event):
        res = self.svc.handle_event(plan_id, {**event, "actor": "chat"})
        if not res.get("ok"):
            raise ServiceError("http_error", "refused", 400, res.get("error", ""))
        return res

    def task(self, *a, **k):
        return {}


class Person:
    last_user_text, untrusted_since_user = "Prepara pure la telefonata all'hotel", False


@pytest.fixture
def world(tmp_path):
    jev, phone, wiki = FakeJev(answer=jev_answer(0.94, "MIXED_SOME_IMPACTFUL", "CRITICAL")), FakeCallAPICall(), FakeWikibridge()
    model = FakeModel(REPLAN)
    svc = PlanService(tmp_path / "planner", client_factory=lambda: model, pump_batch=2, pump_window=300, pump_poll=0.02)
    svc.registry = False
    svc._jev_from_registry = lambda: ("sk-test", jev.url, "jev-latest")
    yield svc, jev, phone, wiki, model
    svc.close()
    for f in (jev, phone, wiki):
        f.close()


def agent_run(svc, phone, wiki, steps, approve):
    approvals = []
    perms = PermissionManager(level=PermissionLevel.DEFAULT, confirm_callback=lambda t, a: approvals.append((t, a)) or (approve and t == "phone_call"))
    registry = ToolRegistry()
    register_planner_tools(registry, LocalPlanner(svc), Confirmer(Person(), lambda q: "confirm"))
    register_phone_tools(registry, CallAPICallClient(phone.url, token="t0ken"), perms, on_behalf_of="Mario Rossi")
    register_wiki_tools(registry, WikiBridgeClient(wiki.url))
    agent = AgentLoop(client=ScriptedClient(*steps), tool_registry=registry, permissions=perms, max_turns=10)
    agent.run("organizza")
    return agent, approvals


def tool_texts(agent):
    return [m["content"] for m in agent.messages if m["role"] == "tool"]


def test_an_event_in_the_mailbox_ends_in_a_prepared_call_that_only_the_chat_can_place_after_a_yes(world):
    svc, jev, phone, wiki, model = world
    svc.create({"plan": PLAN, "card": CARD})
    svc.update_settings({"cloud_triage": True, "event_pump": True})

    # 1. events arrive: they are buffered, and nothing is judged until a batch is due
    first = svc.ingest({"text": "Sciopero dei treni venerdì: Gianni è stato precettato, scrivete a gianni@example.com o al 333 1234567", "source": "rss"})
    assert first["buffered"] == {"cervinia": 1} and jev.requests == []
    svc.ingest({"text": "Nuovo museo a Parigi", "source": "rss"})

    # 2. the cloud filter judged the batch (the fake answers in the real format) and woke the model
    assert wait_for(lambda: svc.snapshot("cervinia")["proposals"] and not svc.snapshot("cervinia")["replanning"])
    sent = json.dumps(jev.requests[0]["body"], ensure_ascii=False)
    assert "Persona 1" in sent and not any(w in sent for w in ("Gianni", "gianni@example.com", "333 1234567"))      # names and contacts did not leave
    assert "Allergica" not in sent and "Macchinista" not in sent                                                        # nor the personal constraints
    assert jev.requests[0]["headers"]["Authorization"] == "Bearer sk-test"
    triage = [e for e in svc.events_since("cervinia", 0) if e["type"] == "plan:triage"][0]
    assert triage["via"] == "pump" and triage["result"]["source"] == "cloud" and triage["result"]["replan_state"] == "started"
    assert model.calls == 1 and svc.telemetry()["triage"]["cloud"] == 1

    # 3. the model's proposal waits: nothing changed, nobody was called
    snap = svc.snapshot("cervinia")
    assert "chiama_hotel" not in snap["plan"]["nodes"] and snap["proposals"][0]["title"] == "Telefona all'hotel" and phone.placed == []

    # 4. the person approves it
    res = svc.handle_event("cervinia", {"type": "plan:approve", "proposal_id": snap["proposals"][0]["proposal_id"], "actor": "user"})
    assert res["ok"]
    node = svc.snapshot("cervinia")["plan"]["nodes"]["chiama_hotel"]
    assert node["call_request"] == CALL and node["wiki_query"].startswith("numero e orari") and phone.placed == []       # prepared, not placed

    # 5. the Chat reads the request and places the call: phone_call asks, and only the yes lets it out
    steps = [call("plan_get", {"plan_id": "cervinia"}, "a"), call("plan_step_requests", {"plan_id": "cervinia", "node_id": "chiama_hotel"}, "b"),
             call("wiki_context", {"query": node["wiki_query"]}, "c"),
             call("phone_call", {"number": CALL["number"], "purpose": CALL["reason"]}, "d"), say("fatto")]
    agent, approvals = agent_run(svc, phone, wiki, steps, approve=True)
    texts = tool_texts(agent)
    assert "call prepared (not placed)" in texts[0] and "NOT been placed" in texts[1] and "wiki_context" in texts[1] and "Allergica agli ovini" in texts[2]
    assert [t for t, _ in approvals].count("phone_call") == 1
    assert len(phone.placed) == 1 and phone.placed[0]["number"] == CALL["number"] and "intelligenza artificiale" in phone.placed[0]["greeting"]
    assert CALL["reason"] in phone.placed[0]["greeting"]


def test_without_the_yes_the_prepared_call_is_never_placed(world):
    svc, jev, phone, wiki, model = world
    svc.create({"plan": PLAN, "card": CARD})
    svc.handle_event("cervinia", {"type": "plan:update_node", "node_id": "hotel", "changes": {"call_request": CALL}, "actor": "user"})
    steps = [call("plan_step_requests", {"plan_id": "cervinia", "node_id": "hotel"}, "a"),
             call("phone_call", {"number": CALL["number"], "purpose": CALL["reason"]}, "b"), say("non chiamo")]
    agent, approvals = agent_run(svc, phone, wiki, steps, approve=False)
    assert [t for t, _ in approvals] == ["phone_call"] and phone.placed == []
    assert "Permission denied" in tool_texts(agent)[1]


def test_the_chat_prepares_a_call_with_the_persons_yes_and_the_plan_never_dials_by_itself(world):
    svc, jev, phone, wiki, model = world
    svc.create({"plan": PLAN, "card": CARD})
    steps = [call("plan_get", {"plan_id": "cervinia"}, "a"),
             call("plan_prepare_call", {"plan_id": "cervinia", "node_id": "hotel", "number": CALL["number"], "reason": CALL["reason"], "text": CALL["text"],
                                        "wiki_query": "orari della reception"}, "b"),
             call("plan_prepare_call", {"plan_id": "cervinia", "node_id": "hotel", "number": "chiama il 112", "reason": "x"}, "c"),
             call("plan_prepare_call", {"plan_id": "cervinia", "node_id": "partenza", "number": CALL["number"], "reason": "x"}, "d"),
             call("plan_step_requests", {"plan_id": "cervinia", "node_id": "hotel"}, "e"), say("preparata")]
    agent, approvals = agent_run(svc, phone, wiki, steps, approve=False)
    texts = tool_texts(agent)
    assert "Nothing was called" in texts[1] and phone.placed == [] and approvals == []
    assert svc.snapshot("cervinia")["plan"]["nodes"]["hotel"]["call_request"] == CALL
    assert "phone number" in texts[2] or "refused" in texts[2]                       # a bad number is refused in words, nothing is written
    assert "action" in texts[3] or "refused" in texts[3]                              # a merge step cannot carry a call
    assert CALL["number"] in texts[4] and "phone_call(" in texts[4]
    assert svc.snapshot("cervinia")["plan"]["nodes"]["hotel"]["call_request"] == CALL
