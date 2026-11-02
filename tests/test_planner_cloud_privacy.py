"""What leaves for the cloud judge: the people of the plan and who wrote to them are replaced; what cannot be found is said, and the cloud is off until the person turns it on."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.planner import JevTriageGatekeeper
from tadamoo.planner import triage as triage_module
from tadamoo.planner.card import sender_names
from test_planner import FULL

EVENTS = ["Email from Mario Bianchi: cena annullata per sciopero", "Telegram from Luca: Mario Bianchi dice che arriva tardi",
          "Email from Trenitalia: sciopero del treno di sabato", "Email from mario.bianchi@example.com: oggetto", "Telegram from someone: ciao"]


def payload(events=EVENTS, **kw):
    return JevTriageGatekeeper(api_key="k", cloud_consent=True, **kw).build_payload(FULL, events)["state"]["events_digest"]


def test_the_senders_of_mail_and_messages_are_replaced_with_the_plans_people():
    digest = payload()
    for name in ("Mario Bianchi", "Luca", "Mario", "Bianchi"):
        assert name not in digest, name
    assert digest.count("Persona 3") == 2 and digest.count("Persona 4") == 1   # after the plan's two people: Mario Bianchi wherever he is named, then Luca
    assert "Trenitalia" in digest and "[email]" in digest                      # a company is not a person; an address is taken out


def test_who_counts_as_a_sender_is_decided_by_the_line_the_sources_write():
    assert sender_names(EVENTS) == ["Mario Bianchi", "Luca"]
    assert sender_names(["Email from Anna: x", "Email from unknown sender: x", "Telegram from someone: x", "Email from A. B. 3rd: x", "Hello from Marco: x"]) == []


def test_a_name_written_inside_a_message_is_not_found_and_the_risk_is_stated(tmp_path):
    digest = payload(["Email from Trenitalia: il biglietto di Carlo Neri è annullato"])
    assert "Carlo Neri" in digest                                              # no rule finds this one: the settings say so
    web = Path(__file__).resolve().parent.parent / "serve" / "web" / "i18n.js"
    text = web.read_text(encoding="utf-8")
    assert text.count("is not recognised and goes as it is") == 1 and text.count("non viene riconosciuto e parte così com'è") == 1
    assert text.count("cannot find a name written inside a message") == 1 and text.count("non può trovare un nome scritto dentro il testo") == 1


def test_with_the_replacement_off_nothing_is_replaced():
    assert "Mario Bianchi" in payload(pseudonymize=False)


def test_the_cloud_stays_off_without_the_persons_consent_even_with_a_key(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the cloud was called")

    monkeypatch.setattr(triage_module._OPENER, "open", refuse)
    for consent in (False,):
        gate = JevTriageGatekeeper(api_key="a-real-key", cloud_consent=consent)
        result = gate.triage_event_batch(FULL, ["sciopero del treno annullato"])
        assert result.source == "offline" and not result.sent_to_cloud
    assert not JevTriageGatekeeper(api_key=None, cloud_consent=True).cloud_allowed
    assert not JevTriageGatekeeper.from_environment().cloud_consent


def test_the_servers_settings_start_with_the_cloud_off(tmp_path):
    from serve import plans
    assert plans.DEFAULTS["cloud_triage"] is False and plans.DEFAULTS["pseudonymize"] is True
