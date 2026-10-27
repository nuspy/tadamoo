"""The cloud filter against the recorded answers of the real service, offline.

Nothing here reaches TypeSafe: a local server plays back the answers recorded in tools/jev_test_results.json and
tools/jev_batch_extremes_results.json, matched to the event it is asked about. What this checks is everything on this side of the wire with the
settings a user gets: what is sent (the card of the specification, names replaced, nothing of a participant's name left), and what the gate does
with each recorded verdict. It cannot say how well the service judges once the names are replaced; that needs the service.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.planner import JevTriageGatekeeper, PlanInsightCard, Pseudonymizer
from harness.planner.card import CARD_FIELDS, scrub_contacts
from test_planner import FakeJev, jev  # noqa: F401  (the local server and its fixture)


def _module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EXTENDED = _module("run_jev_extended_tests")
EXTREMES = _module("run_jev_batch_extremes")
SINGLES = json.loads((ROOT / "tools" / "jev_test_results.json").read_text(encoding="utf-8"))["individual_tests"]
BATCHES = json.loads((ROOT / "tools" / "jev_batch_extremes_results.json").read_text(encoding="utf-8"))
CARD = PlanInsightCard.from_dict(EXTENDED.PLAN_CARD)
NAMES = ("Gianni", "Erica", "Diana")
HIGH = ("MOLTO", "CRITICAL")


def recorded_single(rec):
    """The service's answer to one event, in the shape of the three questions the planner asks today. The recordings hold the relevance and the
    impact; a one-event batch is `all impactful` when its event is serious and `none` when it is not."""
    answers = {"has_blocker_or_critical": {"type": "noul", "noul": rec["relevance"]},
               "batch_impact_scope": {"type": "choice", "choice": "ALL_EVENTS_IMPACTFUL" if rec["impact"] in HIGH else "ZERO_EVENTS_IMPACTFUL"},
               "max_event_impact": {"type": "choice", "choice": rec["impact"], "probabilities": rec["probabilities"]}}
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": rec["tokens"]["input_tokens"], "output_tokens": rec["tokens"]["output_tokens"]}}


def labelled(text):
    """An event as the filter receives it: contacts taken out, the card's people replaced."""
    return Pseudonymizer(CARD.people()).apply(scrub_contacts(text))


class Replay:
    """A local stand-in for the service: it answers with what the real one recorded for the event in the request."""

    def __init__(self, jev):
        self.by_digest = {labelled(r["event"]): recorded_single(r) for r in SINGLES}
        for key in ("batch_c_all_impactful", "batch_d_none_impactful"):
            events = EXTREMES.BATCH_C_ALL_IMPACTFUL if "batch_c" in key else EXTREMES.BATCH_D_NONE_IMPACTFUL
            self.by_digest["\n".join(labelled(e.split(". ", 1)[1]) for e in events)] = {**BATCHES[key]["data"], "answers": self._three(BATCHES[key]["data"]["answers"], key)}
        self.fake = jev(answer=self.answer)
        self.asked = []

    @staticmethod
    def _three(answers, key):
        """The extremes were recorded with two questions of today's set; the third (the strongest event) follows from the verdict."""
        high = "batch_c" in key
        return {**answers, "max_event_impact": {"type": "choice", "choice": "CRITICAL" if high else "NO"}}

    def answer(self, body):
        self.asked.append(body)
        digest = body["state"]["events_digest"]
        lines = [line.split(". ", 1)[1] for line in digest.split("\n")]
        return self.by_digest.get("\n".join(lines)) or self.by_digest[lines[0]]

    def gate(self, **kw):
        return JevTriageGatekeeper(api_key="sk-replay", endpoint=self.fake.url, cloud_consent=True, **kw)


@pytest.fixture
def replay(jev):
    return Replay(jev)


def ids(gate_mode, replay):
    gate = replay.gate(gate_mode=gate_mode)
    return {r["id"] for r in SINGLES if gate.triage_event_batch(CARD, [r["event"]]).wake_system_2}


# -- what is sent with the default settings ----------------------------------------------------------------------

def test_the_default_sends_the_whole_card_of_the_specification_and_no_participants_name(replay):
    gate = replay.gate()
    assert gate.card_fields is None
    for rec in SINGLES:
        gate.triage_event_batch(CARD, [rec["event"]])
    assert len(replay.asked) == len(SINGLES)
    for body in replay.asked:
        sent = json.dumps(body, ensure_ascii=False)
        assert set(body["state"]["active_plan"]) == set(CARD_FIELDS)                       # nothing taken out of the card that the experiment sent
        assert not any(name in sent for name in NAMES)
        assert "Persona 1" in sent and "Persona 2" in sent and "Persona 3" in sent


def test_an_event_about_a_participant_and_the_participants_profile_use_the_same_label(replay):
    gate = replay.gate()
    for rec in (r for r in SINGLES if "Diana" in r["event"] or "Gianni" in r["event"] or "Erica" in r["event"]):
        gate.triage_event_batch(CARD, [rec["event"]])
        body = replay.asked[-1]
        digest, profile = body["state"]["events_digest"], body["state"]["active_plan"]["profilo_e_vincoli_partecipanti"]
        named = [label for label in profile if label in digest]
        assert named, rec["id"]                                                             # the clue in the event reaches the line that explains it
        assert all(len(profile[label]) > 20 for label in named)


def test_the_profile_lines_that_the_indirect_detections_relied_on_are_in_the_payload(replay):
    gate = replay.gate()
    gate.triage_event_batch(CARD, [next(r["event"] for r in SINGLES if r["id"] == "CAT3-IND-3-BREVE")])
    sent = json.dumps(replay.asked[-1], ensure_ascii=False)
    assert "allergia a pecore, lana grezza e ovini" in sent
    gate.triage_event_batch(CARD, [next(r["event"] for r in SINGLES if r["id"] == "CAT3-IND-4-BREVE")])
    sent = json.dumps(replay.asked[-1], ensure_ascii=False)
    assert "precettazioni straordinarie" in sent and "nazionalità russa" in sent            # both clues of the war event: the rail job and the Russian partner


def test_taking_the_personal_field_out_is_a_choice_that_keeps_it_home(replay):
    gate = replay.gate(card_fields=[f for f in CARD_FIELDS if f != "profilo_e_vincoli_partecipanti"])
    gate.triage_event_batch(CARD, [SINGLES[0]["event"]])
    body = replay.asked[-1]
    assert "profilo_e_vincoli_partecipanti" not in body["state"]["active_plan"] and "polmonite" in json.dumps(body, ensure_ascii=False)
    assert "ovini" not in json.dumps(body, ensure_ascii=False)


# -- what the gate does with the recorded verdicts -------------------------------------------------------------------

SERIOUS = {r["id"] for r in SINGLES if r["id"].startswith(("CAT1", "CAT3")) and r["id"] != "CAT3-IND-1-BREVE"}     # the recordings' own verdicts: serious impact
FALSE_ALARMS = {"CAT4-FALSE-2", "CAT4-FALSE-4"}                                                                    # strongly relevant, little impact


def test_the_or_gate_wakes_every_serious_recorded_event_and_the_two_relevant_but_minor_ones(replay):
    woken = ids("or", replay)
    assert SERIOUS <= woken                                                                 # no serious recorded event is lost
    assert woken - SERIOUS == FALSE_ALARMS                                                  # the cost: skis already bought, bovines for a guest allergic to ovines
    assert not woken & {r["id"] for r in SINGLES if r["id"].startswith("CAT2")}             # nothing unrelated wakes the model


def test_the_and_gate_silences_the_minor_ones_and_loses_the_serious_event_rated_below_the_threshold(replay):
    woken = ids("and", replay)
    assert not woken & FALSE_ALARMS
    assert SERIOUS - woken == {"CAT3-IND-2-LUNGA"}                                          # 0.75: serious, but under 0.80
    assert woken <= SERIOUS


def test_the_one_event_the_short_text_hides_is_not_woken_by_either_gate(replay):
    for mode in ("or", "and"):
        assert "CAT3-IND-1-BREVE" not in ids(mode, replay)


def test_the_extreme_batches_wake_and_stay_quiet_as_recorded(replay):
    gate = replay.gate()
    loud = gate.triage_event_batch(CARD, [e.split(". ", 1)[1] for e in EXTREMES.BATCH_C_ALL_IMPACTFUL])
    assert (loud.wake_system_2, loud.impact_scope, loud.has_blocker_prob, loud.source, loud.sent_to_cloud) == (True, "ALL_EVENTS_IMPACTFUL", 0.99, "cloud", True)
    quiet = gate.triage_event_batch(CARD, [e.split(". ", 1)[1] for e in EXTREMES.BATCH_D_NONE_IMPACTFUL])
    assert (quiet.wake_system_2, quiet.impact_scope, quiet.has_blocker_prob) == (False, "ZERO_EVENTS_IMPACTFUL", 0.06)
    assert loud.token_usage == 1557 + 158 and quiet.token_usage == 1421 + 152               # the tokens of the recordings, counted


def test_without_consent_the_same_events_never_reach_the_server_and_are_judged_offline(replay):
    gate = JevTriageGatekeeper(api_key="sk-replay", endpoint=replay.fake.url, cloud_consent=False)
    res = gate.triage_event_batch(CARD, [SINGLES[0]["event"]])
    assert replay.asked == [] and res.source == "offline" and res.sent_to_cloud is False
