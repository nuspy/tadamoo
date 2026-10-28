"""What reaches beyond the person's own work (other people, a call, effects that cannot be undone) is decided step by step: a change that
touches such a step waits for the person, the rest of it goes through and the plan carries on."""
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.integrations.planner_client import PlannerClient
from harness.planner import PlanCanvasBridge, PlanDAG
from harness.planner.card import PlanInsightCard
from harness.planner.involvement import CALL, EFFECTS, PEOPLE, split_diff, step_facts
from harness.planner.replan import Replanner
from harness.planner_tools import Confirmer, involved, register_planner_tools
from harness.tools.registry import ToolRegistry
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_planner import CARD, THREAT, cervinia
from test_planner_tools import AgentState, Asker, title


def N(i, **kw):
    return {"id": i, "type": "action", "title": kw.pop("title", i.title()), "description": "", "assignee": "agent", "dependencies": [], "outputs": [],
            "status": "pending", **kw}


BOOKED = {"effects": [{"effect": "irreversible", "what": "hotel booked"}]}
NODES = {"a": N("a"), "ospite": N("ospite", assignee="third_party"), "hotel": N("hotel", state_variables=BOOKED), "tel": N("tel", call_request={"number": "+39 1"}),
         "me": N("me", assignee="user")}


# -- the facts of one step -------------------------------------------------------------------------------

def test_a_step_is_an_exception_by_its_data_not_by_its_words():
    assert step_facts(NODES["a"]) == [] and step_facts(NODES["me"]) == []                  # a step for the user is a to-do, not an exception
    assert step_facts(NODES["ospite"]) == [PEOPLE] and step_facts(NODES["tel"]) == [CALL] and step_facts(NODES["hotel"]) == [EFFECTS]
    assert step_facts(N("x", state_variables={"effects": [{"effect": "reversible"}]})) == []
    assert step_facts(N("x", title="Telefona a Gianna e prenota")) == []


def test_involved_looks_at_the_steps_asked_for_and_names_them():
    snap = {"plan": {"nodes": NODES}, "card": {"participants": ["Anna"]}}
    assert involved(snap, []) == [] and involved(snap, ["a", "me"]) == []
    assert involved(snap, ["ospite", "hotel"]) == [f'"Ospite": {PEOPLE}', f'"Hotel": {EFFECTS}']
    whole = involved(snap)
    assert whole[0] == "people: Anna" and PEOPLE in whole and CALL in whole and EFFECTS in whole


# -- a diff divided ---------------------------------------------------------------------------------------

def test_a_diff_that_touches_no_exception_goes_whole():
    diff = {"add_nodes": [N("b", dependencies=["a"])], "update_nodes": {"a": {"title": "Uno"}}, "add_edges": [["a", "me"]]}
    s = split_diff(NODES, diff)
    assert s.parked == {} and s.reasons == [] and s.safe["update_nodes"] == {"a": {"title": "Uno"}} and [n["id"] for n in s.safe["add_nodes"]] == ["b"]


def test_a_new_step_for_a_third_person_is_parked_with_what_waits_for_it():
    diff = {"add_nodes": [N("x", title="Invita Gianna", assignee="third_party", dependencies=["a"]), N("y", dependencies=["x"]), N("z", dependencies=["a"], outputs=["x"])],
            "update_nodes": {"a": {"title": "Uno"}}}
    s = split_diff(NODES, diff)
    assert [n["id"] for n in s.parked["add_nodes"]] == ["x", "y"] and [n["id"] for n in s.safe["add_nodes"]] == ["z"]
    assert s.safe["update_nodes"] == {"a": {"title": "Uno"}} and "outputs" not in s.safe["add_nodes"][0] or s.safe["add_nodes"][0]["outputs"] == []
    assert ["z", "x"] in s.parked["add_edges"]                                            # the link to a parked step is parked with it
    assert s.reasons[0] == f'"Invita Gianna": {PEOPLE}' and "waits for a step of that kind" in s.reasons[1]


def test_changing_removing_or_linking_an_existing_exception_is_parked():
    s = split_diff(NODES, {"update_nodes": {"hotel": {"title": "Altro"}, "a": {"title": "Uno"}}, "remove_nodes": ["tel"],
                           "add_edges": [["a", "hotel"], ["a", "me"]], "remove_edges": [["ospite", "a"]]})
    assert set(s.parked["update_nodes"]) == {"hotel"} and set(s.safe["update_nodes"]) == {"a"}
    assert s.parked["remove_nodes"] == ["tel"] and "remove_nodes" not in s.safe
    assert s.parked["add_edges"] == [["a", "hotel"]] and s.safe["add_edges"] == [["a", "me"]] and s.parked["remove_edges"] == [["ospite", "a"]]


def test_a_step_that_becomes_an_exception_by_the_change_is_parked():
    s = split_diff(NODES, {"update_nodes": {"a": {"assignee": "third_party"}, "me": {"call_request": {"number": "+39 2"}}}})
    assert set(s.parked["update_nodes"]) == {"a", "me"} and s.safe == {}


# -- the bridge applies the safe part ----------------------------------------------------------------------

@pytest.fixture
def bridge():
    return PlanCanvasBridge(cervinia(), PlanInsightCard.from_dict(CARD))


MIXED = {"update_nodes": {"parti": {"title": "Partenza sabato"}},
         "add_nodes": [N("gianna", title="Chiedi a Gianna", assignee="third_party", dependencies=["meteo"], outputs=["parti"])]}


def test_a_proposal_says_which_steps_hold_it_back(bridge):
    pid = bridge.handle({"type": "plan:propose_diff", "title": "Misto", "diff": MIXED, "actor": "planner"})["proposal_id"]
    prop = next(p for p in bridge.snapshot()["proposals"] if p["proposal_id"] == pid)
    assert prop["involves"] == [f'"Chiedi a Gianna": {PEOPLE}'] and prop["auto"] is False
    plain = bridge.handle({"type": "plan:propose_diff", "title": "Semplice", "diff": {"update_nodes": {"parti": {"title": "x"}}}})["proposal_id"]
    assert next(p for p in bridge.snapshot()["proposals"] if p["proposal_id"] == plain)["involves"] == []


def test_approving_the_safe_part_applies_it_and_leaves_the_rest_pending(bridge):
    pid = bridge.handle({"type": "plan:propose_diff", "title": "Misto", "diff": MIXED, "actor": "planner"})["proposal_id"]
    rev = bridge.rev
    res = bridge.handle({"type": "plan:approve", "proposal_id": pid, "safe_only": True, "actor": "planner"})
    assert res["ok"] and res["applied"] and res["remaining"] == pid and res["changed"] and bridge.rev == rev + 1
    assert bridge.dag.nodes["parti"].title == "Partenza sabato" and "gianna" not in bridge.dag.nodes
    snap = bridge.snapshot()
    waiting = [p for p in snap["proposals"]]
    assert [p["proposal_id"] for p in waiting] == [pid] and [n["id"] for n in waiting[0]["diff"]["add_nodes"]] == ["gianna"]
    assert waiting[0]["involves"] == [f'"Chiedi a Gianna": {PEOPLE}']
    done = [p for p in bridge.proposals.values() if p.status == "applied"]
    assert len(done) == 1 and done[0].auto is True and done[0].diff == {"update_nodes": {"parti": {"title": "Partenza sabato"}}}
    assert bridge.history[-1]["actor"] == "planner" and "applied without waiting" in bridge.history[-1]["summary"]
    # the person then approves the rest, as before
    assert bridge.handle({"type": "plan:approve", "proposal_id": pid, "actor": "user"})["ok"] and "gianna" in bridge.dag.nodes


def test_a_proposal_that_is_all_exception_applies_nothing(bridge):
    diff = {"add_nodes": [MIXED["add_nodes"][0]]}
    pid = bridge.handle({"type": "plan:propose_diff", "title": "Solo lei", "diff": diff})["proposal_id"]
    rev = bridge.rev
    res = bridge.handle({"type": "plan:approve", "proposal_id": pid, "safe_only": True})
    assert res["ok"] and res["applied"] is False and res["remaining"] == pid and bridge.rev == rev and bridge.proposals[pid].status == "pending"


def test_a_proposal_with_no_exception_is_applied_whole_and_marked_automatic(bridge):
    pid = bridge.handle({"type": "plan:propose_diff", "title": "Semplice", "diff": {"update_nodes": {"parti": {"title": "x"}}}})["proposal_id"]
    res = bridge.handle({"type": "plan:approve", "proposal_id": pid, "safe_only": True})
    assert res["applied"] and res["remaining"] is None and bridge.proposals[pid].status == "applied" and bridge.proposals[pid].auto is True


def test_involves_and_auto_survive_saving(bridge):
    pid = bridge.handle({"type": "plan:propose_diff", "title": "Misto", "diff": MIXED})["proposal_id"]
    bridge.handle({"type": "plan:approve", "proposal_id": pid, "safe_only": True})
    again = PlanCanvasBridge.from_dict(json.loads(json.dumps(bridge.to_dict())))
    assert again.proposals[pid].involves == bridge.proposals[pid].involves and any(p.auto for p in again.proposals.values())


# -- re-planning that applies by itself --------------------------------------------------------------------

def alternatives(*alts, recommended=0):
    class Client:
        def chat_completion(self, messages, **kw):
            body = {"diagnosis": "x", "affected_nodes": ["parti"], "alternatives": list(alts), "recommended": recommended, "human_fallbacks": []}
            return {"choices": [{"message": {"role": "assistant", "content": json.dumps(body)}}]}
    return Replanner(Client())


SAFE_ALT = {"title": "Rimanda", "description": "d", "diff": {"update_nodes": {"parti": {"title": "Partenza tra una settimana"}}}}
OTHER_ALT = {"title": "Auto", "description": "d", "diff": {"update_nodes": {"parti": {"title": "Parti in auto"}}}}
MIXED_ALT = {"title": "Misto", "description": "d", "diff": MIXED}


def test_replan_without_the_setting_leaves_every_proposal_pending(bridge):
    bridge.replanner = alternatives(SAFE_ALT, OTHER_ALT)
    out = bridge.replan(THREAT, ["sciopero"])
    assert out["applied"] == {} and bridge.dag.nodes["parti"].title == "Partenza" and len(bridge.snapshot()["proposals"]) == 2


def test_replan_with_the_setting_applies_the_recommended_one_and_closes_the_others(bridge):
    bridge.replanner = alternatives(OTHER_ALT, SAFE_ALT, recommended=1)
    heard = []
    bridge.subscribe(heard.append)
    out = bridge.replan(THREAT, ["sciopero"], auto_apply=True)
    assert out["applied"]["applied"] is True and out["applied"]["proposal_id"] == out["proposals"][1]
    assert bridge.dag.nodes["parti"].title == "Partenza tra una settimana" and bridge.snapshot()["proposals"] == []
    assert bridge.proposals[out["proposals"][0]].status == "rejected"
    assert heard[-1]["type"] == "plan:replan_ready" and heard[-1]["applied"]["applied"] is True


def test_replan_parks_the_step_with_a_person_and_goes_on_with_the_rest(bridge):
    bridge.replanner = alternatives(MIXED_ALT, SAFE_ALT)
    out = bridge.replan(THREAT, ["sciopero"], auto_apply=True)
    assert out["applied"]["applied"] is True and out["applied"]["remaining"] == out["proposals"][0]
    assert bridge.dag.nodes["parti"].title == "Partenza sabato" and "gianna" not in bridge.dag.nodes
    pending = bridge.snapshot()["proposals"]
    assert {p["proposal_id"] for p in pending} == set(out["proposals"])                  # the rest and the other alternative stay for the person


def test_replan_with_only_exceptions_applies_nothing(bridge):
    bridge.replanner = alternatives({**MIXED_ALT, "diff": {"add_nodes": MIXED["add_nodes"]}})
    out = bridge.replan(THREAT, ["sciopero"], auto_apply=True)
    assert out["applied"]["applied"] is False and "gianna" not in bridge.dag.nodes and len(bridge.snapshot()["proposals"]) == 1


# -- the Chat's tools at the Auto level ---------------------------------------------------------------------

@pytest.fixture
def auto_world(start_server):  # noqa: F811
    client = PlannerClient(start_server([answer("x")]))
    agent = AgentState()

    def build(auto=True, *answers):
        ask = Asker(*answers)
        reg = ToolRegistry()
        register_planner_tools(reg, client, Confirmer(agent, ask, auto=lambda: auto))
        return reg, ask

    return client, agent, build


def made(client, reg, steps):
    t = title()
    out = reg.execute("plan_create", {"title": t, "kind": "occasional", "steps": steps})
    pid = next(p["plan_id"] for p in client.list() if p["title"] == t)
    reg.execute("plan_get", {"plan_id": pid})
    return pid, out


def test_at_auto_a_plan_with_a_step_for_someone_else_is_created_without_it_and_the_step_waits(auto_world):
    client, agent, build = auto_world
    reg, ask = build()
    pid, out = made(client, reg, [{"title": "Prenota"}, {"title": "Chiedi a Gianna", "assignee": "third_party"}, {"title": "Dopo Gianna", "after": [2]}, {"title": "Parti", "after": [1]}])
    snap = client.snapshot(pid)
    assert ask.asked == [] and set(snap["plan"]["nodes"]) == {"s1", "s4"} and "left as a proposal" in out
    waiting = snap["proposals"]
    assert len(waiting) == 1 and {n["id"] for n in waiting[0]["diff"]["add_nodes"]} == {"s2", "s3"} and waiting[0]["involves"]


def test_at_auto_a_plan_made_only_of_steps_for_others_still_asks(auto_world):
    client, agent, build = auto_world
    reg, ask = build(True, "confirm")
    pid, out = made(client, reg, [{"title": "Chiedi a Gianna", "assignee": "third_party"}])
    assert len(ask.asked) == 1 and "Chiedi a Gianna" in ask.asked[0]["question"] and set(client.snapshot(pid)["plan"]["nodes"]) == {"s1"}


def test_at_auto_a_plan_with_people_in_its_card_no_longer_stops_the_steps_that_touch_none(auto_world):
    client, agent, build = auto_world
    reg, ask = build(True, "confirm")
    pid, _ = made(client, reg, [{"title": "Uno"}, {"title": "Due"}])
    assert "Saved (asked)" in reg.execute("plan_set_card", {"plan_id": pid, "participants": ["Gianna"]})        # adding people is asked
    assert len(ask.asked) == 1 and ask.asked[0]["facts"]["involves"] == ["people: Gianna"]
    reg.execute("plan_get", {"plan_id": pid})
    assert client.snapshot(pid)["card"]["partecipanti"] == ["Gianna"]
    out = reg.execute("plan_set_card", {"plan_id": pid, "date": "sabato"})
    assert "Saved (auto)" in out and len(ask.asked) == 1                                  # the card names people, this change touches none
    assert "marked completed" in reg.execute("plan_mark", {"plan_id": pid, "node_id": "s1", "status": "completed"}) and len(ask.asked) == 1
    assert "archived (auto)" in reg.execute("plan_archive", {"plan_id": pid}) and len(ask.asked) == 1


def test_at_auto_a_step_for_someone_else_is_asked_for_but_the_other_steps_are_not(auto_world):
    client, agent, build = auto_world
    reg, ask = build(True, "confirm")
    pid, _ = made(client, reg, [{"title": "Prenota"}, {"title": "Gianna deve confermare", "assignee": "third_party", "after": []}])
    # s2 was parked at creation: add it by hand with the person's yes, then mark the steps
    client.event(pid, {"type": "plan:approve", "proposal_id": client.snapshot(pid)["proposals"][0]["proposal_id"], "actor": "user"})
    reg.execute("plan_get", {"plan_id": pid})
    assert "marked completed" in reg.execute("plan_mark", {"plan_id": pid, "node_id": "s1", "status": "completed"}) and ask.asked == []
    reg.execute("plan_mark", {"plan_id": pid, "node_id": "s2", "status": "completed"})
    assert len(ask.asked) == 1 and ask.asked[0]["facts"]["involves"] == [f'"Gianna deve confermare": {PEOPLE}'] and "This involves" in ask.asked[0]["question"]


def test_at_auto_a_change_that_mixes_both_is_applied_for_the_safe_part_and_the_rest_waits(auto_world):
    client, agent, build = auto_world
    reg, ask = build()
    pid, _ = made(client, reg, [{"title": "Prenota"}, {"title": "Parti"}])
    diff = {"update_nodes": {"s2": {"title": "Parti sabato"}},
            "add_nodes": [N("g", title="Invita Gianna", assignee="third_party", dependencies=["s1"], outputs=["s2"])]}
    out = reg.execute("plan_propose_change", {"plan_id": pid, "title": "Gianna", "rationale": "x", "diff": diff})
    assert ask.asked == [] and out.startswith("Applied (auto)") and "waits for the user" in out and "do not ask again" in out
    snap = client.snapshot(pid)
    assert snap["plan"]["nodes"]["s2"]["title"] == "Parti sabato" and "g" not in snap["plan"]["nodes"]
    assert [n["id"] for n in snap["proposals"][0]["diff"]["add_nodes"]] == ["g"]
    # the plan goes on: a plain change right after is applied with no question
    reg.execute("plan_get", {"plan_id": pid})
    out = reg.execute("plan_propose_change", {"plan_id": pid, "title": "Rinomina", "rationale": "x", "diff": {"update_nodes": {"s1": {"title": "Prenota subito"}}}})
    assert "Change applied (auto)" in out and ask.asked == []


def test_at_auto_a_change_that_is_all_exception_waits_as_a_proposal_without_blocking(auto_world):
    client, agent, build = auto_world
    reg, ask = build()
    pid, _ = made(client, reg, [{"title": "Prenota"}, {"title": "Parti"}])
    diff = {"add_nodes": [N("g", title="Invita Gianna", assignee="third_party", dependencies=["s1"])]}
    out = reg.execute("plan_propose_change", {"plan_id": pid, "title": "Gianna", "rationale": "x", "diff": diff})
    assert ask.asked == [] and out.startswith("Nothing was applied") and "Invita Gianna" in out
    assert "g" not in client.snapshot(pid)["plan"]["nodes"] and len(client.snapshot(pid)["proposals"]) == 1


def test_at_auto_the_persons_own_words_still_cover_a_step_for_someone_else(auto_world):
    client, agent, build = auto_world
    reg, ask = build()
    pid, _ = made(client, reg, [{"title": "Prenota"}, {"title": "Parti"}])
    agent.last_user_text = "aggiungi un passo per Gianna che mi conferma"
    diff = {"add_nodes": [N("g", title="Invita Gianna", assignee="third_party", dependencies=["s1"])]}
    out = reg.execute("plan_propose_change", {"plan_id": pid, "title": "Gianna", "rationale": "x", "diff": diff, "quote": "aggiungi un passo per Gianna"})
    assert "Change applied (quoted)" in out and "g" in client.snapshot(pid)["plan"]["nodes"]


def test_at_auto_deciding_a_pending_proposal_applies_what_touches_no_exception(auto_world):
    client, agent, build = auto_world
    reg, ask = build()
    pid, _ = made(client, reg, [{"title": "Prenota"}, {"title": "Parti"}])
    diff = {"update_nodes": {"s2": {"title": "Parti sabato"}}, "add_nodes": [N("g", title="Invita Gianna", assignee="third_party", dependencies=["s1"])]}
    prid = client.event(pid, {"type": "plan:propose_diff", "title": "Misto", "diff": diff, "source": "planner"})["proposal_id"]
    out = reg.execute("plan_decide_proposal", {"plan_id": pid, "proposal_id": prid, "decision": "approve"})
    assert out.startswith("Applied (auto)") and ask.asked == [] and client.snapshot(pid)["plan"]["nodes"]["s2"]["title"] == "Parti sabato"
    assert "g" not in client.snapshot(pid)["plan"]["nodes"]


def test_below_auto_every_change_is_still_asked_whole(auto_world):
    client, agent, build = auto_world
    reg, ask = build(False, "confirm", "confirm", "confirm")
    pid, _ = made(client, reg, [{"title": "Prenota"}, {"title": "Parti"}])
    diff = {"update_nodes": {"s2": {"title": "Parti sabato"}}}
    out = reg.execute("plan_propose_change", {"plan_id": pid, "title": "Rinomina", "rationale": "x", "diff": diff})
    assert "Change applied (asked)" in out and len(ask.asked) == 2                         # the creation and the change


def test_the_confirmer_decides_on_the_touched_steps_not_on_the_whole_plan():
    people = {"plan": {"nodes": NODES}, "card": {"participants": ["Anna"]}}
    for touches, expect in (([], "auto"), (involved(people, ["ospite"]), "asked")):
        asker = Asker("confirm")
        ok, how = Confirmer(AgentState(), asker, auto=lambda: True).check({"kind": "plan_change"}, "Change it?", "", people, touches)
        assert ok and how == expect and len(asker.asked) == (how == "asked")
    asker = Asker("cancel")
    assert Confirmer(AgentState(), asker, auto=lambda: True).check({"kind": "plan_change"}, "Change it?", "", people)[0] is False      # not told: the whole plan
    below = Asker("confirm")
    assert Confirmer(AgentState(), below, auto=lambda: False).check({"kind": "plan_change"}, "Change it?", "", people, [])[1] == "asked"
