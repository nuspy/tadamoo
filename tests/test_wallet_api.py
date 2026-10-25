"""The page that keeps the vault and the payment limits: values go in and never come out, and the declaration is checked by the server."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.browser import BrowserPrefs
from harness.payments_broker import PaymentsBroker
from harness.vault import Vault
from harness.waiver import required_phrase
from serve import handler
from serve.wallet_api import WalletApi
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer
from test_e2e_mock_server import _NoTelemetry, answer, start_server  # noqa: F401  (fixture)

NAME = "Mario Rossi"
NUMBER = "4242 4242 4242 4242"


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def setup(tmp_path, monkeypatch, start_server):  # noqa: F811
    api = WalletApi(lambda: Vault(tmp_path / "vault"), lambda: PaymentsBroker(tmp_path / "payments"), BrowserPrefs(tmp_path / "browser.json"), tmp_path / "profile")
    monkeypatch.setattr(handler, "_WALLET", api)
    return start_server([answer("x")]), tmp_path


def card(base, **kw):
    return call(base, "POST", "/api/wallet/entry", {"id": "trip", "kind": "card", "label": "Trip card", "card_kind": "virtual", "currency": "eur",
                                                    "fields": {"holder": NAME, "number": NUMBER, "expiry": "12/30", "cvc": "123"}, "hosts": "shop.example, pay.example", **kw})


def system(base, **kw):
    return call(base, "POST", "/api/wallet/system", {"id": "vcard", "name": "Trip card", "kind": "virtual card", "credential_kind": "vault_card",
                                                      "vault_entry": "trip", "currency": "EUR", "terms_url": "https://provider.example/terms",
                                                      "terms_checked_on": "2026-10-24", "how_to_revoke": "Block the card in the provider app", **kw})


def test_a_card_goes_in_and_only_its_last_four_digits_come_out(setup):
    base, tmp = setup
    code, body = card(base)
    assert code == 200 and body["entry"]["fields"]["number"] == "•••• 4242" and body["entry"]["hosts"] == ["shop.example", "pay.example"]
    code, body = call(base, "GET", "/api/wallet")
    assert code == 200 and [e["id"] for e in body["entries"]] == ["trip"]
    shown = json.dumps(body)
    for needle in ("4242 4242", "Rossi", "123"):
        assert needle not in shown
    assert not any(NUMBER.encode() in f.read_bytes() for f in (tmp / "vault").rglob("*") if f.is_file())     # nor on disk


def test_an_edit_that_leaves_a_field_out_keeps_it(setup):
    base, tmp = setup
    card(base)
    call(base, "POST", "/api/wallet/entry", {"id": "trip", "kind": "card", "fields": {"cvc": "999"}, "guard": False})
    stored = Vault(tmp / "vault").resolve("{{vault:trip.number}}{{vault:trip.cvc}}")
    assert stored == NUMBER + "999"


def test_bad_input_is_a_clear_400(setup):
    base, _ = setup
    code, body = call(base, "POST", "/api/wallet/entry", {"id": "bad id", "kind": "card"})
    assert code == 400 and "name of an entry" in body["error"]["message"]
    assert call(base, "POST", "/api/wallet/entry", {"id": "x", "kind": "login", "second_factor": "automatic"})[0] == 400
    assert call(base, "POST", "/api/wallet/nothing", {})[0] == 404


def test_a_system_needs_its_vault_entry_and_an_entry_in_use_stays(setup):
    base, _ = setup
    assert call(base, "POST", "/api/wallet/system", {})[0] == 400
    code, body = system(base)
    assert code == 400 and "no vault entry" in body["error"]["message"]
    card(base)
    assert system(base)[0] == 200
    code, body = call(base, "POST", "/api/wallet/entry/delete", {"id": "trip"})
    assert code == 400 and "remove the system first" in body["error"]["message"]
    assert call(base, "POST", "/api/wallet/system/remove", {"id": "vcard"}) == (200, {"removed": True})
    assert call(base, "POST", "/api/wallet/entry/delete", {"id": "trip"}) == (200, {"deleted": True})


def test_a_login_system_cannot_use_a_card_entry(setup):
    base, _ = setup
    card(base)
    code, body = system(base, credential_kind="vault_login")
    assert code == 400 and "needs a login entry" in body["error"]["message"]


def grant(base, **kw):
    per = kw.pop("per_payment", "50")
    body = {"system_id": "vcard", "user_name": NAME, "typed_text": required_phrase(NAME, per, "it"), "per_payment": per, "cumulative": "120",
            "read_to_end": True, "release_ack": True, "retyped_names": {"release": NAME, "acceptance": NAME}, "lang": "it", **kw}
    return call(base, "POST", "/api/wallet/grant", body)


def test_the_declaration_is_given_checked_shown_and_revoked(setup):
    base, tmp = setup
    card(base)
    system(base)
    code, body = call(base, "POST", "/api/wallet/phrase", {"name": NAME, "amount": "50", "lang": "it"})
    assert code == 200 and body["phrase"] == required_phrase(NAME, "50", "it")
    code, body = grant(base, typed_text="I agree")
    assert code == 400 and "declaration" in body["error"]["message"]
    assert grant(base, read_to_end=False)[0] == 400
    code, body = grant(base)
    assert code == 200 and body["waiver"]["system_id"] == "vcard"
    code, body = call(base, "GET", "/api/wallet")
    system_row = body["systems"][0]
    assert system_row["waiver"]["per_payment"] == "50.00 EUR" and system_row["waiver"]["cumulative"] == "120.00 EUR"
    assert [r["action"] for r in body["ledger"]][:2] == ["waiver_granted", "system_registered"]
    assert call(base, "POST", "/api/wallet/revoke", {})[1] == {"ended": 1}                 # the kill switch, with no system named
    assert call(base, "GET", "/api/wallet")[1]["systems"][0]["waiver"] is None
    assert PaymentsBroker(tmp / "payments").ledger.verify_integrity()[0]


def test_a_pasted_declaration_is_accepted_and_recorded_as_pasted(setup):
    base, tmp = setup
    card(base)
    system(base)
    assert grant(base, pasted=True)[0] == 200
    ledger = json.loads((tmp / "payments" / "payments.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert ledger["details"]["pasted"] is True


def test_the_browser_choice_is_kept_and_read_by_the_conversations(setup):
    base, tmp = setup
    code, body = call(base, "GET", "/api/wallet")
    assert body["browser"]["enabled"] is True and body["browser"]["headless"] is False        # on, with a window, until the person says otherwise
    assert body["browser"]["private"] is False                                                 # pages of this PC and its network: off until they say so
    assert call(base, "POST", "/api/wallet/browser", {"enabled": True})[0] == 400
    code, body = call(base, "POST", "/api/wallet/browser", {"enabled": False, "headless": True})
    assert code == 200 and body["browser"]["enabled"] is False and body["browser"]["headless"] is True and body["browser"]["private"] is False
    assert call(base, "POST", "/api/wallet/browser", {"enabled": True, "headless": False, "private": True})[1]["browser"]["private"] is True
    assert BrowserPrefs(tmp / "browser.json").read() == {"enabled": True, "headless": False, "private": True, "persistent": False}


def test_the_owner_can_read_a_value_back_and_an_unknown_one_is_a_clear_error(setup):
    base, _ = setup
    card(base)
    assert call(base, "POST", "/api/wallet/reveal", {"id": "trip", "field": "number"}) == (200, {"value": NUMBER})
    code, body = call(base, "POST", "/api/wallet/reveal", {"id": "trip", "field": "passport"})
    assert code == 400 and "no field" in body["error"]["message"]
    assert call(base, "POST", "/api/wallet/reveal", {"id": "ghost", "field": "number"})[0] == 400


def test_logins_from_an_exported_file_are_added_once_and_for_their_own_sites(setup):
    base, tmp = setup
    csv_text = "name,url,username,password\nExample,https://www.example.com,mario,pw-one-1234\nBank,https://bank.example.org,mrossi,pw-two-5678\n"
    code, body = call(base, "POST", "/api/wallet/import", {"csv": csv_text})
    assert code == 200 and body["added"] == 2 and body["skipped"]["duplicate"] == 0 and body["refused"] == []
    again = call(base, "POST", "/api/wallet/import", {"csv": csv_text})[1]
    assert again["added"] == 0 and again["skipped"]["duplicate"] == 2
    listing = call(base, "GET", "/api/wallet")[1]
    assert sorted(e["id"] for e in listing["entries"]) == ["bank-example-org", "example-com"]
    assert "pw-one" not in json.dumps(listing)
    assert Vault(tmp / "vault").resolve("{{vault:bank-example-org.password}}", hosts=["bank.example.org"]) == "pw-two-5678"
    assert call(base, "POST", "/api/wallet/import", {"csv": "name,note\nx,y\n"})[0] == 400
    assert call(base, "POST", "/api/wallet/import", {})[0] == 400


def test_the_kept_browser_choice_and_the_window_to_sign_in_and_forgetting(setup, monkeypatch):
    base, tmp = setup
    opened = []
    monkeypatch.setattr("serve.wallet_api.open_signin_window", lambda profile, url: opened.append((str(profile), url)))
    assert call(base, "GET", "/api/wallet")[1]["browser"]["persistent"] is False                    # sign-ins are not kept until the person says so
    code, body = call(base, "POST", "/api/wallet/browser", {"enabled": True, "headless": False, "persistent": True})
    assert code == 200 and body["browser"]["persistent"] is True
    assert BrowserPrefs(tmp / "browser.json").read()["persistent"] is True
    assert call(base, "POST", "/api/wallet/browser/signin", {}) == (200, {"opened": True})
    assert call(base, "POST", "/api/wallet/browser/signin", {"url": "https://bank.example/login"})[0] == 200
    assert opened == [(str(tmp / "profile"), "https://accounts.google.com"), (str(tmp / "profile"), "https://bank.example/login")]
    (tmp / "profile").mkdir()
    (tmp / "profile" / "Cookies").write_bytes(b"x")
    assert call(base, "POST", "/api/wallet/browser/forget", {}) == (200, {"forgotten": True})
    assert not (tmp / "profile").exists()


def test_the_window_to_sign_in_says_when_the_profile_is_in_use(setup):
    from harness.browser import ProfileLock
    base, tmp = setup
    held = ProfileLock(tmp / "profile")
    assert held.acquire()
    try:
        code, body = call(base, "POST", "/api/wallet/browser/signin", {})
        assert code == 400 and "in use" in body["error"]["message"]
        assert call(base, "POST", "/api/wallet/browser/forget", {})[0] == 400
    finally:
        held.release()


# ---- the gate in front of every POST route: the server's secret, the page's own origin, this PC ----
POST_ROUTES = ("entry", "reveal", "import", "entry/delete", "system", "system/remove", "phrase", "grant", "revoke", "browser", "browser/signin", "browser/forget")
SECRET = "control-secret"


class Spy:
    """Stands in for the wallet: it records what the handler lets through and changes nothing."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return {}
        return record


@pytest.fixture
def guarded(monkeypatch):
    """A server that has a control secret, like the one the app starts, with the wallet replaced by a spy."""
    spy = Spy()
    monkeypatch.setattr(handler, "_WALLET", spy)
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, answer("x"), max_context=4096), tok, ChatTemplate(Path(__file__).resolve().parents[1] / "serve" / "chat_template.jinja"))
    svc.telemetry = _NoTelemetry()
    svc.control_token = SECRET
    httpd = serve(svc, port=0)
    yield f"http://127.0.0.1:{httpd.server_address[1]}", spy
    httpd.shutdown()
    httpd.server_close()


def post(base, route, headers=None, body=b"{}"):
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + "/api/wallet/" + route, data=body, method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("route", POST_ROUTES)
def test_a_wallet_route_needs_the_control_secret(guarded, route):
    base, spy = guarded
    code, body = post(base, route)
    assert code == 403 and body["error"]["code"] == "control_secret"
    assert post(base, route, {"X-Basalt-Control": "wrong"})[0] == 403
    assert spy.calls == []


@pytest.mark.parametrize("route", POST_ROUTES)
def test_a_wallet_route_refuses_the_page_of_another_site(guarded, route):
    base, spy = guarded
    code, body = post(base, route, {"X-Basalt-Control": SECRET, "Origin": "http://evil.example"})
    assert code == 403 and "own page" in body["error"]["message"]
    assert spy.calls == []


@pytest.mark.parametrize("route", POST_ROUTES)
def test_a_wallet_route_refuses_what_is_not_json(guarded, route):
    base, spy = guarded
    assert post(base, route, {"X-Basalt-Control": SECRET, "Content-Type": "text/plain"})[0] == 415
    assert spy.calls == []


@pytest.mark.parametrize("route", POST_ROUTES)
def test_a_wallet_route_refuses_a_client_that_is_not_on_this_pc(guarded, monkeypatch, route):
    base, spy = guarded
    monkeypatch.setattr(handler.settings, "is_loopback", lambda address: False)
    code, body = post(base, route, {"X-Basalt-Control": SECRET})
    assert code == 403 and "only from the PC" in body["error"]["message"]
    assert spy.calls == []


@pytest.mark.parametrize("route", POST_ROUTES)
def test_the_same_request_with_the_secret_from_the_page_goes_through(guarded, route):
    base, spy = guarded
    assert post(base, route, {"X-Basalt-Control": SECRET})[0] == 200
    assert post(base, route, {"X-Basalt-Control": SECRET, "Origin": base})[0] == 200      # the page's own origin
    assert len(spy.calls) == 2
