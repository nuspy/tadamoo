"""Every plan has a card, however it was made: drawn from the plan, kept in step with it, and completed by asking the person only what is missing."""
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import JevTriageGatekeeper, PlanCanvasBridge, PlanDAG, PlanInsightCard, PlanProfile, PlanStore
from tadamoo.planner.card import card_gaps, derive_card
from tadamoo.planner_tools import interview_text
from test_planner import CARD, chain, node
from test_planner_tools import Asker, AgentState, world  # noqa: F401  (fixture)
from test_session_wake import quiet
from test_session_tasks import Model, last_user, said_in
from test_session import say
from test_e2e_mock_server import start_server  # noqa: F401  (fixture)


def test_a_plan_made_without_a_card_gets_one_drawn_from_it(tmp_path):
    dag = chain("hotel", "partenza")
    dag.title = "Weekend a Cervinia"
    bridge = PlanStore(tmp_path).create(dag, None, None, PlanProfile.from_dict({"starts_on": "2026-10-24", "ends_on": "2026-10-26"}))
    card = bridge.card
    assert card.titolo == "Weekend a Cervinia" and card.date == "2026-10-24 - 2026-10-26"
    assert card.logistica_e_prenotazioni == {"Hotel": "pending", "Partenza": "pending"}
    assert card.partecipanti == [] and card.destinazione == "" and bridge.card_auto == ["titolo", "date", "logistica_e_prenotazioni"]


def test_a_card_that_follows_the_plan_changes_with_it(tmp_path):
    bridge = PlanStore(tmp_path).create(chain("hotel", "partenza"))
    bridge.handle({"type": "plan:mark", "node_id": "hotel", "status": "completed"})
    assert bridge.card.logistica_e_prenotazioni["Hotel"] == "completed"
    bridge.handle({"type": "plan:update_meta", "changes": {"title": "Weekend nuovo"}})
    assert bridge.card.titolo == "Weekend nuovo"
    bridge.handle({"type": "plan:update_node", "node_id": "partenza", "changes": {"assignee": "user"}})
    assert bridge.card.logistica_e_prenotazioni["Partenza"] == "pending, for user"


def test_what_the_person_writes_stops_following_the_plan(tmp_path):
    bridge = PlanStore(tmp_path).create(chain("hotel", "partenza"))
    card = bridge.card.to_dict()
    card.update(destinazione="Cervinia", partecipanti=["Io", "Gianni"], logistica_e_prenotazioni={"alloggio": "Hotel confermato"})
    assert bridge.handle({"type": "plan:update_meta", "changes": {"card": card}})["ok"]
    assert bridge.card_auto == ["titolo", "date"]                       # the logistics the person wrote is theirs now
    bridge.handle({"type": "plan:mark", "node_id": "hotel", "status": "completed"})
    assert bridge.card.logistica_e_prenotazioni == {"alloggio": "Hotel confermato"} and bridge.card.destinazione == "Cervinia"


def test_a_card_given_at_creation_is_the_persons_and_does_not_follow_the_plan(tmp_path):
    bridge = PlanStore(tmp_path).create(chain("a", "b"), PlanInsightCard.from_dict(CARD))
    assert bridge.card_auto == []
    bridge.handle({"type": "plan:mark", "node_id": "a", "status": "completed"})
    assert bridge.card.to_dict() == PlanInsightCard.from_dict(CARD).to_dict()


def test_a_plan_stored_without_a_card_gets_one_when_it_is_loaded(tmp_path):
    store = PlanStore(tmp_path)
    store.create(chain("a", "b"))
    data = store._bridges["p"].to_dict()
    data["card"], data["card_auto"] = None, []
    again = PlanCanvasBridge.from_dict(data)
    assert again.card.titolo == "t" and again.card_auto == ["titolo", "date", "logistica_e_prenotazioni"]


def test_the_card_survives_saving_and_loading_with_what_follows_the_plan(tmp_path):
    store = PlanStore(tmp_path)
    store.create(chain("a", "b"))
    fresh = PlanStore(tmp_path).get("p")
    assert fresh.card_auto == ["titolo", "date", "logistica_e_prenotazioni"] and fresh.card.logistica_e_prenotazioni == {"A": "pending", "B": "pending"}


def test_gaps_are_what_a_trip_needs_to_be_judged_and_a_routine_needs_nothing():
    card = derive_card(chain("a"), None)
    assert card_gaps(card, "occasional") == ["date", "destinazione", "partecipanti"] and card_gaps(card, "constant") == []
    full = PlanInsightCard.from_dict({**CARD, "date": "24-26 ottobre", "destinazione": "Cervinia"})
    assert card_gaps(full, "occasional") == []


def test_steps_with_the_same_title_keep_both_and_a_long_plan_keeps_every_step():
    dag = PlanDAG(plan_id="p", title="t", target_type="occasional")
    for i in range(60):
        dag.add_node(node(f"n{i}", title="Telefona"))
    card = derive_card(dag)
    assert len(card.logistica_e_prenotazioni) == 60 and "Telefona (2)" in card.logistica_e_prenotazioni and "Telefona (60)" in card.logistica_e_prenotazioni


def test_a_finished_short_plan_has_nothing_left_to_protect_and_a_routine_is_always_open(tmp_path):
    bridge = PlanStore(tmp_path).create(chain("a", "b"))
    assert bridge.active()
    bridge.handle({"type": "plan:mark", "node_id": "a", "status": "completed"})
    bridge.handle({"type": "plan:mark", "node_id": "b", "status": "skipped"})
    assert not bridge.active()
    bridge.handle({"type": "plan:update_meta", "changes": {"target_type": "constant"}})
    assert bridge.active()


def test_a_plan_with_a_drawn_card_is_judged_offline_and_nothing_leaves_without_consent():
    seen = []
    gate = JevTriageGatekeeper(api_key="sk-x", cloud_consent=False)
    gate._ask_cloud = lambda *a, **k: seen.append(a)                    # would be the network
    card = PlanCanvasBridge(chain("hotel")).card
    res = gate.triage_event_batch(card, ["Sciopero dei treni"])
    assert res.source == "offline" and res.sent_to_cloud is False and seen == []


# -- the interview ---------------------------------------------------------------------------------------

def make(reg, agent, kind="occasional"):
    title = f"Weekend {uuid.uuid4().hex[:6]}"
    agent.last_user_text = f"Crea il piano {title} per favore"
    out = reg.execute("plan_create", {"title": title, "kind": kind, "steps": [{"title": "Hotel"}], "quote": f"Crea il piano {title}"})
    return title, out


def test_the_model_is_told_what_is_missing_when_it_creates_or_reads_a_plan(world):
    client, agent, build = world
    reg, ask = build("default")
    title, out = make(reg, agent)
    assert "still lacks: the dates, the place, who takes part" in out and "plan_set_card" in out and "do not invent" in out
    pid = next(p["plan_id"] for p in client.list() if p["title"] == title)
    assert "still lacks" in reg.execute("plan_get", {"plan_id": pid})
    assert "still lacks" not in make(reg, agent, "constant")[1]


def test_the_answer_goes_into_the_card_with_the_users_yes_and_the_interview_ends(world):
    client, agent, build = world
    reg, ask = build("default", "confirm")
    title, _ = make(reg, agent)
    pid = next(p["plan_id"] for p in client.list() if p["title"] == title)
    other, _ = build("default")                                                       # a conversation that has not read the plan
    assert "Read the plan first" in other.execute("plan_set_card", {"plan_id": pid, "destination": "Cervinia"})
    assert "Give at least one" in reg.execute("plan_set_card", {"plan_id": pid})
    assert "list of names" in reg.execute("plan_set_card", {"plan_id": pid, "participants": [" "]})
    out = reg.execute("plan_set_card", {"plan_id": pid, "date": "24-26 ottobre", "destination": "Cervinia", "participants": ["Io", "Gianni"],
                                        "constraints": {"Gianni": "macchinista FS, può essere precettato"}})
    assert "Saved (asked)" in out and "what events need" in out
    q = ask.asked[0]
    assert "write into its card" in q["question"] and "constraints" in q["question"]
    snap = client.snapshot(pid)
    card = snap["card"]
    assert (card["date"], card["destinazione"], card["partecipanti"]) == ("24-26 ottobre", "Cervinia", ["Io", "Gianni"])
    assert card["profilo_e_vincoli_partecipanti"] == {"Gianni": "macchinista FS, può essere precettato"}
    assert snap["card_missing"] == [] and "date" not in snap["card_auto"]


def test_without_a_yes_nothing_is_written_to_the_card(world):
    client, agent, build = world
    reg, ask = build("default", "cancel")
    agent.last_user_text = "Crea il piano Weekend per favore"
    reg.execute("plan_create", {"title": "Weekend2", "kind": "occasional", "steps": [{"title": "Hotel"}], "quote": "Crea il piano Weekend"})
    pid = next(p["plan_id"] for p in client.list() if p["title"] == "Weekend2")
    reg.execute("plan_get", {"plan_id": pid})
    assert "said no" in reg.execute("plan_set_card", {"plan_id": pid, "destination": "Cervinia"})
    assert client.snapshot(pid)["card"]["destinazione"] == ""


def test_the_planner_asks_in_the_chat_about_a_new_plan_made_elsewhere_once(tmp_path):
    model = Model((said_in("[planner]"), lambda: say("Dove e quando?")))
    session, ev, plans = quiet(tmp_path, model)
    plans.add("P1", "Cena")
    plans.plans[0]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    plans.snaps["P1"].update(card_missing=["date", "partecipanti"], history=[{"actor": "user", "type": "create"}])
    key = session.poll_plans()
    assert key == "c:P1:date,partecipanti"
    ev.wait("done")
    assert "lacks date, partecipanti" in last_user(model.sent[0]) and "plan_set_card" in last_user(model.sent[0])


@pytest.mark.parametrize("history,updated", [([{"actor": "chat", "type": "create"}], "now"), ([{"actor": "user", "type": "create"}], "2020-01-01T00:00:00Z")])
def test_it_does_not_ask_about_a_plan_the_chat_made_or_an_old_one(tmp_path, history, updated):
    session, ev, plans = quiet(tmp_path, Model())
    plans.add("P1", "Cena")
    plans.plans[0]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if updated == "now" else updated
    plans.snaps["P1"].update(card_missing=["date"], history=history)
    assert session.poll_plans() is None


def test_interview_text_is_empty_when_nothing_is_missing():
    assert interview_text({"card_missing": []}) == "" and "the place" in interview_text({"card_missing": ["destinazione"]})
