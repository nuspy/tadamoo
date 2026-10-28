"""The Cervinia weekend of the specification, whole and offline: the real server with a scripted model, the Chat's tools, the events pump.

What is deterministic is run here: the Chat builds the plan and is asked each time, the interview fills the card, the invitations write the plan's
values, the decision routes (and, when no exit holds, goes to the person), a strike arrives with nine harmless events, the pump judges the batch with the
local filter (the cloud is off: nothing leaves), the scripted model re-plans, the Chat reads and approves a proposal, a task the software cannot do is
offered and declined. What needs the real model, the real service and a real window stays in tests/QUEUE_daytime.md.
"""
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.integrations.planner_client import PlannerClient
from harness.planner_tools import Confirmer, register_planner_tools
from harness.tools.registry import ToolRegistry
from test_e2e_mock_server import start_server  # noqa: F401  (fixture)
from test_planner_tools import AgentState


def N(i, title, **kw):
    return {"id": i, "type": "action", "title": title, "description": "", "assignee": "agent", "dependencies": [], "outputs": [], **kw}


# the structure the Chat adds to the two-step plan it made: calls, a merge that needs the head count, a decision, a weather sensor, a task for the person
STRUCTURE = {
    "add_nodes": [
        N("meteo", "Neve giovedì notte", type="sensor", dependencies=[], outputs=["s2"], conditions={"when": "'nevicata' in text", "set": {"meteo_ok": True}}),
        N("invita_gianna", "Telefona a Gianna", dependencies=["s1"], outputs=["esiti"]),
        N("invita_lucia", "Telefona a Lucia", dependencies=["s1"], outputs=["esiti"]),
        N("esiti", "Raccogli gli esiti", type="merge", dependencies=["invita_gianna", "invita_lucia"], outputs=["scelta"], conditions={"requires": ["invitati_confermati"]}),
        N("scelta", "Dove si cena", type="decision", dependencies=["esiti"], outputs=["terrazza", "tavolo", "rimanda"],
          conditions={"terrazza": "len(invitati_confermati) == 2", "tavolo": "len(invitati_confermati) == 1", "rimanda": "len(invitati_confermati) == 0"}),
        N("terrazza", "Prenota la terrazza", dependencies=["scelta"], outputs=["s2"]),
        N("tavolo", "Prenota il tavolo interno", dependencies=["scelta"], outputs=["s2"]),
        N("rimanda", "Rimanda di una settimana", dependencies=["scelta"], outputs=["s2"]),
        N("lava", "Lava i piatti dopo cena", dependencies=["s1"]),
    ],
    "remove_edges": [["s1", "s2"]],
}
REPLAN = {"diagnosis": "Lo sciopero ferma il treno di Gianna: serve un piano per arrivare in auto.",
          "affected_nodes": ["s2"],
          "alternatives": [{"title": "Parti in auto", "description": "Gianna sale in macchina con gli altri",
                            "diff": {"update_nodes": {"s2": {"title": "Parti in auto"}}}},
                           {"title": "Rimanda la partenza", "description": "Partenza sabato mattina", "diff": {"update_nodes": {"s2": {"title": "Parti sabato"}}}}],
          "recommended": 0,
          "human_fallbacks": [{"node_id": "lava", "reason": "Lavare i piatti è un'azione fisica che il software non può fare",
                               "offer": "Cerco e prenoto un servizio di pulizie a Cervinia"}]}
SCRIPT = "</think>\n\n" + json.dumps(REPLAN)


class Yes:
    """The person's answer to every question the harness puts."""

    def __init__(self):
        self.asked = []

    def __call__(self, payload):
        self.asked.append(payload)
        return "confirm"


def wait_for(pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def chat(start_server):
    url = start_server([SCRIPT])
    client = PlannerClient(url)
    client._call("POST", "/api/planner/settings", {"cloud_triage": False, "event_pump": True, "auto_apply": False})
    agent, yes = AgentState(), Yes()
    reg = ToolRegistry()
    register_planner_tools(reg, client, Confirmer(agent, yes))
    return client, reg, agent, yes


def pid_of(client, title):
    return next(p["plan_id"] for p in client.list() if p["title"] == title)


def test_the_cervinia_weekend_from_the_chat_to_the_approved_replan(chat):
    client, reg, agent, yes = chat
    title = f"Weekend a Cervinia {uuid.uuid4().hex[:5]}"
    run = lambda name, **args: reg.execute(name, args)

    # 1. the Chat makes the plan and is told what the card still lacks
    made = run("plan_create", title=title, kind="occasional", steps=[{"title": "Prenota l'hotel"}, {"title": "Parti"}])
    pid = pid_of(client, title)
    assert "still lacks: the dates, the place, who takes part" in made
    snap = client.snapshot(pid)
    assert snap["card"]["titolo"] == title and snap["card_missing"] == ["date", "destinazione", "partecipanti"]

    # 2. the interview: the person answers, the Chat writes it with their yes
    out = run("plan_set_card", plan_id=pid, date="Venerdì 24 - Domenica 26 ottobre", destination="Cervinia", participants=["Io", "Gianna", "Lucia"],
              constraints={"Gianna": "ferroviera, può essere precettata", "Lucia": "allergica agli ovini"})
    assert "Saved (asked)" in out and client.snapshot(pid)["card_missing"] == []

    # 3. the Chat gives the plan its structure; a decision with its exits and a merge that needs the head count are accepted
    out = run("plan_propose_change", plan_id=pid, title="Inviti, scelta della cena e meteo", rationale="il weekend dipende da chi accetta", diff=STRUCTURE)
    assert "Change applied (asked)" in out
    snap = client.snapshot(pid)
    assert {"meteo", "esiti", "scelta", "lava"} <= set(snap["plan"]["nodes"]) and snap["plan"]["nodes"]["scelta"]["type"] == "decision"
    assert set(snap["plan"]["nodes"]["s2"]["dependencies"]) == {"meteo", "terrazza", "tavolo", "rimanda"}
    assert "s1" in snap["layers"][0] and "meteo" in snap["layers"][0]                                   # the canvas has its layers
    mermaid = client._call("GET", f"/api/plans/{pid}/mermaid")["mermaid"]
    assert "scelta" in mermaid and "-->" in mermaid

    # 4. a call is prepared, never placed
    run("plan_get", plan_id=pid)
    out = run("plan_prepare_call", plan_id=pid, node_id="invita_gianna", number="+39 333 1112233", reason="Invitare Gianna al weekend", text="Ciao, vieni?")
    assert "Nothing was called" in out and "NOT been placed" in run("plan_step_requests", plan_id=pid, node_id="invita_gianna")

    # 5. the invitations are done: each writes the plan's values; the merge waits for them, then the decision routes
    run("plan_get", plan_id=pid)
    assert "marked completed" in run("plan_mark", plan_id=pid, node_id="s1", status="completed")
    assert "esiti" not in client.snapshot(pid)["ready"]
    run("plan_mark", plan_id=pid, node_id="invita_gianna", status="completed", outputs={"invitati_confermati": ["Gianna"]})
    assert "esiti" not in client.snapshot(pid)["ready"]                                              # one of two is not the whole head count of the merge's inputs
    run("plan_mark", plan_id=pid, node_id="invita_lucia", status="completed", outputs={"invitati_confermati": ["Gianna", "Lucia"]})
    assert "esiti" in client.snapshot(pid)["ready"]
    run("plan_mark", plan_id=pid, node_id="esiti", status="completed")
    run("plan_mark", plan_id=pid, node_id="scelta", status="completed")
    snap = client.snapshot(pid)
    assert snap["plan"]["shared_state"] == {"invitati_confermati": ["Gianna", "Lucia"]}
    assert snap["plan"]["nodes"]["scelta"]["state_variables"]["selected"] == ["terrazza"]
    assert [snap["plan"]["nodes"][n]["status"] for n in ("tavolo", "rimanda")] == ["skipped", "skipped"]

    # 6. a strike arrives among nine harmless events: the pump judges the batch with the local filter, nothing leaves, the model re-plans
    calls_before = client._call("GET", "/api/plans/telemetry")["triage"]
    for text in ["Sciopero dei treni venerdì: Gianna è stata precettata"] + [f"Notizia {i} senza rapporto" for i in range(9)]:
        client.ingest(text, "rss", pid)
    assert wait_for(lambda: client.snapshot(pid)["proposals"] and not client.snapshot(pid).get("replanning"))
    triage = [e for e in client._call("GET", f"/api/plans/{pid}/events?after=0")["events"] if e["type"] == "plan:triage"][0]
    assert triage["via"] == "pump" and triage["events"] == 10 and triage["result"]["source"] == "offline" and triage["result"]["sent_to_cloud"] is False
    tele = client._call("GET", "/api/plans/telemetry")["triage"]
    assert tele["cloud"] == calls_before["cloud"] and tele["tokens"] == calls_before["tokens"]        # the cloud was not asked

    # 7. nothing changed by itself; the Chat reads what the proposals would do and the person approves one
    snap = client.snapshot(pid)
    assert snap["plan"]["nodes"]["s2"]["title"] == "Parti"
    seen = run("plan_proposals", plan_id=pid)
    assert 'changes s2 "Parti": title -> "Parti in auto"' in seen and 'title -> "Parti sabato"' in seen
    first = next(p for p in snap["proposals"] if p["title"] == "Parti in auto")
    out = run("plan_decide_proposal", plan_id=pid, proposal_id=first["proposal_id"], decision="approve")
    assert "applied (asked)" in out and client.snapshot(pid)["plan"]["nodes"]["s2"]["title"] == "Parti in auto"
    assert client.snapshot(pid)["history"][-1]["actor"] == "chat"

    # 8. the task the software cannot do is offered; declining hands it to the person and blocks what depends on it
    offers = client.snapshot(pid)["offers"]
    assert [o["node_id"] for o in offers] == ["lava"] and "azione fisica" in offers[0]["reason"]
    assert "Waiting for the user: lava" in run("plan_get", plan_id=pid)
    client.event(pid, {"type": "plan:fallback_response", "offer_id": offers[0]["offer_id"], "accept_alternative": False, "actor": "user"})
    snap = client.snapshot(pid)
    assert snap["plan"]["nodes"]["lava"]["assignee"] == "user" and snap["offers"] == []

    # 9. the person was asked for every change, and no call was placed
    kinds = [q["facts"]["kind"] for q in yes.asked if q.get("mode") == "confirm"]
    assert "plan_change" in kinds and "plan_mark" in kinds


def test_when_no_exit_of_the_decision_holds_it_goes_to_the_person_with_the_facts(chat):
    client, reg, agent, yes = chat
    title = f"Cena {uuid.uuid4().hex[:5]}"
    reg.execute("plan_create", {"title": title, "kind": "occasional", "steps": [{"title": "Prenota l'hotel"}, {"title": "Parti"}]})
    pid = pid_of(client, title)
    reg.execute("plan_propose_change", {"plan_id": pid, "title": "Struttura", "rationale": "x", "diff": STRUCTURE})
    reg.execute("plan_get", {"plan_id": pid})
    for node in ("s1", "invita_gianna", "invita_lucia"):
        reg.execute("plan_mark", {"plan_id": pid, "node_id": node, "status": "completed", **({"outputs": {"invitati_confermati": ["A", "B", "C"]}} if node == "invita_lucia" else {})})
    reg.execute("plan_mark", {"plan_id": pid, "node_id": "esiti", "status": "completed"})
    out = reg.execute("plan_mark", {"plan_id": pid, "node_id": "scelta", "status": "completed"})
    assert "NOT completed" in out and "len(invitati_confermati) == 2' does not hold" in out and "'A', 'B', 'C'" not in out and '"A", "B", "C"' in out
    snap = client.snapshot(pid)
    scelta = snap["plan"]["nodes"]["scelta"]
    assert scelta["assignee"] == "user" and scelta["status"] == "pending" and scelta["state_variables"]["no_route"]["values"] == {"invitati_confermati": ["A", "B", "C"]}
    assert all(snap["plan"]["nodes"][n]["status"] in ("blocked", "pending") for n in ("terrazza", "tavolo", "rimanda"))      # nothing was skipped in silence
    # the person decides: they add a condition for three guests and complete the decision again
    client.event(pid, {"type": "plan:update_node", "node_id": "scelta", "actor": "user",
                       "changes": {"conditions": {"terrazza": "len(invitati_confermati) >= 2", "tavolo": "len(invitati_confermati) == 1", "rimanda": "len(invitati_confermati) == 0"}}})
    res = client.event(pid, {"type": "plan:mark", "node_id": "scelta", "status": "completed", "actor": "user"})
    assert res["status"] == "completed" and client.snapshot(pid)["plan"]["nodes"]["scelta"]["state_variables"]["selected"] == ["terrazza"]
