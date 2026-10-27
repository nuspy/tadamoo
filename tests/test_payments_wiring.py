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
    built.payment_confirm.notice = session.payment_notice
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


def test_a_purchase_from_the_agent_to_the_shop_form(tmp_path):
    """The whole way: the agent asks to pay, the broker approves within the cap, the agent types the card from the vault into the shop's form, and
    the shop receives the real number while the model reads only placeholders."""
    import http.server
    import urllib.parse
    from harness.browser import find_browser
    if find_browser() is None:
        pytest.skip("needs Edge or Chrome on this PC")
    got = []

    class Shop(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send('<title>Checkout</title><form method="post" action="/pay"><label for="n">Card number</label><input id="n" name="number">'
                       '<label for="c">Security code</label><input id="c" name="cvc"><button type="submit">Pay</button></form>')

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            got.append({k: v[0] for k, v in urllib.parse.parse_qs(body).items()})
            self._send("<title>Paid</title><h1>Order received</h1>")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        shop = f"http://127.0.0.1:{server.server_address[1]}/"
        vault, broker = set_up(tmp_path, waiver=("50", "120"))
        vault.put("trip", kind="card", fields={"holder": NAME, "number": NUMBER, "expiry": "12/30", "cvc": "737"}, hosts=["127.0.0.1"], guard=True)
        client = Scripted(call("request_payment", **PAY), call("browser_open", url=shop), call("browser_type", ref=1, text="{{vault:trip.number}}"),
                          call("browser_type", ref=2, text="{{vault:trip.cvc}}"), call("browser_click", ref=3), say("ordinato"))
        ws = tmp_path / "ws"
        ws.mkdir()
        built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="auto", max_turns=10,
                                        vault_dir=str(tmp_path / "vault"), payments_dir=str(tmp_path / "payments"), browser=True, browser_headless=True,
                                        browser_private=True), client=client)
        events = Events()
        session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None)
        built.payment_confirm.ask = session.ask_payment
        built.agent.permissions.confirm_callback = lambda tool, args: True                 # the Rule of Two asks once the page was read: the person says yes
        session.send("m1", "compra la camera")
        assert events.wait("done", timeout=90)["final"] == "ordinato"
        assert got == [{"number": NUMBER, "cvc": "737"}]
        everything = json.dumps(client.sent)
        for needle in (NUMBER, "737"):
            at = everything.find(needle)
            assert at < 0, f"{needle} reached the model: ...{everything[max(0, at - 160):at + 60]}..."       # the model never read the card
        assert PaymentsBroker(tmp_path / "payments").status()[0]["spent"] == "30.00 EUR"
        built.close()
    finally:
        server.shutdown()


def test_every_authorized_payment_is_said_in_the_conversation(tmp_path):
    set_up(tmp_path, waiver=("50", "120"))
    client = Scripted(call("request_payment", **PAY, site="shop.example"), say("pagato"))
    session, ev, built = session_for(tmp_path, client)
    session.send("m1", "paga l'acconto")
    notice = ev.wait("payment")
    assert notice["amount"].startswith("30.00") and notice["recipient"] == "Hotel Cervino" and notice["site"] == "shop.example" and notice["mode"] == "waiver"
    assert ev.wait("done")["final"] == "pagato"
