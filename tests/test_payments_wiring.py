"""The agent can ask to pay: the broker decides, a card leaves the vault only for an approved payment, and two processes keep one ledger."""
import copy
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import agent_loop as al
from harness.audit import AuditLedger
from harness.payments_broker import PaymentsBroker
from harness.session import Session, SessionStore
from harness.stack import AgentConfig, build_agent
from harness.vault import Vault
from harness.waiver import PaymentSystem, required_phrase

from test_session import Events, Scripted, call, say

NAME = "Mario Rossi"
NUMBER = "4242424242424242"


def set_up(tmp_path, *, waiver=None, system_kw=None):
    vault = Vault(tmp_path / "vault")
    vault.put("trip", kind="card", fields={"holder": NAME, "number": NUMBER, "expiry": "12/30", "cvc": "123"}, hosts=["shop.example"],
              card_kind="virtual", currency="eur")
    broker = PaymentsBroker(tmp_path / "payments")
    broker.register_system(PaymentSystem(system_id="vcard", name="Trip card", kind="virtual card", credential_kind="vault_card", currency="EUR",
                                         provider_terms_url="https://provider.example/terms", provider_terms_checked_on="2026-10-24",
                                         revoke_instructions="Block the card in the provider app", vault_entry="trip", **(system_kw or {})))
    if waiver:
        per, cum = waiver
        broker.grant_waiver("vcard", user_name=NAME, typed_text=required_phrase(NAME, per, "it"), per_payment=per, cumulative=cum, read_to_end=True,
                            release_ack=True, retyped_names={"release": NAME, "acceptance": NAME}, lang="it")
    return vault, broker


def session_for(tmp_path, client):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="auto",
                                    max_turns=8, vault_dir=str(tmp_path / "vault"), payments_dir=str(tmp_path / "payments")), client=client)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None)
    built.agent.secrets.ask_code = session.ask_code
    built.payment_confirm.ask = session.ask_payment
    return session, events, built


PAY = dict(system_id="vcard", amount="30", currency="EUR", recipient="Hotel Cervino", reason="deposit")


def test_no_tool_is_offered_when_there_is_nothing_to_pay_with(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False,
                                    payments_dir=str(tmp_path / "payments")), client=Scripted())
    assert "request_payment" not in built.agent.tools.names() and built.payment_confirm is None


def test_within_the_caps_it_pays_by_itself_and_the_card_comes_out_for_the_form(tmp_path):
    set_up(tmp_path, waiver=("50", "120"))
    client = Scripted(call("request_payment", **PAY), call("shell_command", command="echo {{vault:trip.number}}"), say("pagato"))
    session, ev, built = session_for(tmp_path, client)
    session.send("m1", "paga l'acconto")
    assert ev.wait("done")["final"] == "pagato"
    tool_result = json.dumps(client.sent[1])
    assert "{{vault:trip.number}}" in tool_result and NUMBER not in tool_result and "authorized" in tool_result
    assert NUMBER not in json.dumps(client.sent[2])                              # the echo of the number came back masked


def test_over_the_cap_the_person_is_asked_and_a_no_stops_it(tmp_path):
    set_up(tmp_path, waiver=("20", "120"))
    client = Scripted(call("request_payment", **PAY), say("non pagato"))
    session, ev, built = session_for(tmp_path, client)
    session.send("m1", "paga")
    ask = ev.wait("ask")
    assert ask["kind"] == "payment" and ask["facts"]["amount"] == "30.00 EUR" and [o["value"] for o in ask["options"]] == ["confirm", "cancel"]
    assert session.answer(ask["id"], "cancel")
    assert ev.wait("done")["final"] == "non pagato"
    assert "refused" in json.dumps(client.sent[1]).lower()
    assert PaymentsBroker(tmp_path / "payments").status()[0]["spent"] == "0.00 EUR"


def test_over_the_cap_a_yes_pays_and_is_recorded_as_confirmed(tmp_path):
    set_up(tmp_path, waiver=("20", "120"))
    client = Scripted(call("request_payment", **PAY), say("pagato"))
    session, ev, built = session_for(tmp_path, client)
    session.send("m1", "paga")
    ask = ev.wait("ask")
    session.answer(ask["id"], "confirm")
    ev.wait("done")
    assert [r["action"] for r in PaymentsBroker(tmp_path / "payments").recent()][0] == "payment_confirmed"


def test_without_any_waiver_every_payment_is_asked(tmp_path):
    set_up(tmp_path)
    client = Scripted(call("request_payment", **PAY), say("ok"))
    session, ev, built = session_for(tmp_path, client)
    session.send("m1", "paga")
    assert ev.wait("ask")["kind"] == "payment"
    session.cancel()


def test_the_card_stays_in_the_vault_until_a_payment_is_approved(tmp_path):
    set_up(tmp_path, waiver=("50", "120"))
    client = Scripted(call("shell_command", command="echo {{vault:trip.number}}"), say("provato"))
    session, ev, built = session_for(tmp_path, client)
    session.send("m1", "mostrami la carta")
    ev.wait("done")
    out = json.dumps(client.sent[1])
    assert NUMBER not in out and "released only by an approved payment" in out


def test_two_processes_writing_the_same_ledger_keep_its_chain(tmp_path):
    set_up(tmp_path, waiver=("50", "10000"))
    errors = []

    def spend(n):
        try:
            broker = PaymentsBroker(tmp_path / "payments")          # each one is another process's view of the same files
            for i in range(10):
                broker.authorize("vcard", "1", "EUR", f"shop{n}", f"order {i}")
        except Exception as e:                                       # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=spend, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    ledger = AuditLedger(tmp_path / "payments" / "payments.jsonl")
    assert ledger.verify_integrity()[0]
    assert PaymentsBroker(tmp_path / "payments").status()[0]["spent"] == "40.00 EUR"
