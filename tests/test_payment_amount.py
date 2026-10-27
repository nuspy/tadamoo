"""A card approved for an amount goes only into a page that asks for no more, once, and never through a channel where no page is read."""
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import page_money
from harness.payments_broker import PaymentsBroker
from harness.session import Session, SessionStore
from harness.stack import AgentConfig, build_agent
from harness.vault import Vault, VaultError

from test_payments_wiring import NAME, NUMBER, PAY, set_up
from test_session import Events, Scripted, call, say


# -- reading the amount a page asks for --------------------------------------------------------------------------------------

@pytest.mark.parametrize("number,cents", [("30", 3000), ("30,00", 3000), ("30.00", 3000), ("1.234,50", 123450), ("1,234.50", 123450), ("12,5", 1250),
                                          ("1 234,50", 123450), ("1.234", 123400), ("0,99", 99)])
def test_numbers_are_read_in_both_european_and_english_notation(number, cents):
    assert page_money.cents_of(number) == cents


def test_the_total_is_the_amount_marked_as_such_and_not_a_part_of_it():
    page = "Room 2 nights  € 100,00\nSubtotal 100,00 EUR\nShipping 5,00 EUR\nTotal: 105,00 EUR\nYou saved 20,00 EUR"
    assert page_money.payable(page, "EUR") == (10500, "")
    assert page_money.payable("Pay now $30.00\nShipping $2.00", "USD") == (3000, "")


def test_without_a_marked_total_the_largest_amount_counts_and_a_page_without_one_says_why():
    assert page_money.payable("Deposit 30 EUR\nBalance 470,00 €", "EUR")[0] == 47000
    cents, why = page_money.payable("The room is nice", "EUR")
    assert cents is None and "no amount in EUR" in why
    cents, why = page_money.payable("Total $30.00", "EUR")
    assert cents is None and "USD" in why


# -- the vault releases a card only against the approved amount ------------------------------------------------------------

def armed(tmp_path, amount="30", **kw):
    vault, broker = set_up(tmp_path, waiver=("50", "120"))
    r = broker.authorize("vcard", amount, "EUR", "Hotel", "deposit", site="shop.example")
    vault.arm("trip", payment_id=r["payment_id"], site=r["site"], amount_cents=r["amount_cents"], currency="EUR", **kw)
    return vault, r


def test_the_card_is_released_into_a_page_that_asks_for_the_approved_amount_or_less(tmp_path):
    vault, r = armed(tmp_path)
    assert r["amount_cents"] == 3000
    assert vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=lambda c: (3000, c)) == NUMBER
    assert vault.resolve("{{vault:trip.cvc}}", hosts=["shop.example"], payable=lambda c: (2500, c)) == "123"


def test_a_page_that_asks_for_more_than_was_approved_gets_nothing_and_the_use_is_kept(tmp_path):
    vault, _ = armed(tmp_path)
    with pytest.raises(VaultError, match="asks for 300.00 EUR, not the 30.00 EUR that was approved"):
        vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=lambda c: (30000, c))
    with pytest.raises(VaultError, match="USD"):
        vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=lambda c: (3000, "USD"))
    assert vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=lambda c: (3000, c)) == NUMBER        # the refusals did not spend the use


def test_a_page_with_no_amount_it_can_read_gets_nothing(tmp_path):
    vault, _ = armed(tmp_path)

    def unreadable(currency):
        raise VaultError("the page shows no amount in EUR; the card is typed only into a page that shows the amount to pay")

    with pytest.raises(VaultError, match="shows no amount"):
        vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=unreadable)


def test_a_command_cannot_get_the_card_because_no_page_is_read_for_it(tmp_path):
    vault, _ = armed(tmp_path)
    with pytest.raises(VaultError, match="payment page"):
        vault.resolve("{{vault:trip.number}}", hosts=["shop.example"])
    with pytest.raises(VaultError, match="payment page"):
        vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=None)


def test_each_field_comes_out_once_per_approval_and_a_larger_number_is_an_explicit_choice(tmp_path):
    vault, _ = armed(tmp_path)
    pay = lambda c: (3000, c)  # noqa: E731
    assert vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=pay) == NUMBER
    with pytest.raises(VaultError, match="has been used"):
        vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=pay)
    vault2, _ = armed(tmp_path / "again", uses=2)
    for _ in range(2):
        vault2.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=pay)
    with pytest.raises(VaultError, match="has been used"):
        vault2.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=pay)


def test_the_approval_is_short(tmp_path):
    now = [1000.0]
    vault = Vault(tmp_path / "v", clock=lambda: now[0])
    vault.put("trip", kind="card", fields={"holder": NAME, "number": NUMBER}, hosts=["shop.example"])
    vault.arm("trip", amount_cents=3000, currency="EUR")
    now[0] += 301
    with pytest.raises(VaultError, match="only by an approved payment"):
        vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=lambda c: (3000, c))


def test_request_payment_arms_the_card_with_the_amount_it_approved(tmp_path):
    set_up(tmp_path, waiver=("50", "120"))
    ws = tmp_path / "ws"
    ws.mkdir()
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="auto",
                                    vault_dir=str(tmp_path / "vault"), payments_dir=str(tmp_path / "payments")), client=Scripted())
    out = built.agent.tools.execute("request_payment", {**PAY, "site": "shop.example"})
    assert out["amount_cents"] == 3000
    vault = built.agent.secrets.vault
    with pytest.raises(VaultError, match="asks for 31.00 EUR"):
        vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=lambda c: (3100, c))
    assert vault.resolve("{{vault:trip.number}}", hosts=["shop.example"], payable=lambda c: (3000, c)) == NUMBER


# -- the whole way in a real browser, when there is one ------------------------------------------------------------------

def shop_server(total, got):
    import urllib.parse

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
            self._send(f'<title>Checkout</title><p>Total: {total}</p><form method="post" action="/pay"><label for="n">Card number</label>'
                       '<input id="n" name="number"><button type="submit">Pay</button></form>')

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            got.append({k: v[0] for k, v in urllib.parse.parse_qs(body).items()})
            self._send("<title>Paid</title><h1>Order received</h1>")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("total,typed", [("30,00 EUR", True), ("300,00 EUR", False), ("30,00 USD", False)])
def test_a_merchant_page_that_asks_for_more_than_the_approved_amount_never_sees_the_card(tmp_path, total, typed):
    from harness.browser import find_browser
    if find_browser() is None:
        pytest.skip("needs Edge or Chrome on this PC")
    got = []
    server = shop_server(total, got)
    try:
        shop = f"http://127.0.0.1:{server.server_address[1]}/"
        vault, broker = set_up(tmp_path, waiver=("50", "120"))
        vault.put("trip", kind="card", fields={"holder": NAME, "number": NUMBER, "expiry": "12/30", "cvc": "737"}, hosts=["127.0.0.1"], guard=True)
        client = Scripted(call("request_payment", **PAY), call("browser_open", url=shop), call("browser_type", ref=1, text="{{vault:trip.number}}"),
                          call("browser_click", ref=2), say("fine"))
        ws = tmp_path / "ws"
        ws.mkdir()
        built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="auto",
                                        max_turns=10, vault_dir=str(tmp_path / "vault"), payments_dir=str(tmp_path / "payments"), browser=True,
                                        browser_headless=True, browser_private=True), client=client)
        events = Events()
        session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None)
        built.payment_confirm.ask = session.ask_payment
        built.agent.permissions.confirm_callback = lambda tool, args: True
        session.send("m1", "buy")
        events.wait("done", timeout=90)
        assert (got == [{"number": NUMBER}]) if typed else (got == [] or got[0].get("number", "") == "")
        assert NUMBER not in json.dumps(client.sent)
        built.close()
    finally:
        server.shutdown()
