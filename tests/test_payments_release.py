"""A card leaves the vault only while the payment that allowed it still stands, to the site named, a few times; the kill switch works at once."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.payments_broker import PaymentsBroker
from harness.vault import Vault, VaultError
from harness.waiver import PaymentSystem, required_phrase

NAME = "Mario Rossi"
FORM = "{{vault:trip.number}}"


def system(**kw):
    return PaymentSystem(system_id="vcard", name="Trip card", kind="virtual card", credential_kind="vault_card", currency=kw.pop("currency", "EUR"),
                         provider_terms_url="https://provider.example/terms", provider_terms_checked_on="2026-10-24",
                         revoke_instructions="Block the card in the provider app", vault_entry=kw.pop("vault_entry", "trip"), **kw)


def set_up(tmp_path, waiver=("50", "120"), sites=None):
    vault = Vault(tmp_path / "vault")
    vault.put("trip", kind="card", fields={"holder": NAME, "number": "4242424242424242", "expiry": "12/30", "cvc": "123"}, hosts=["shop.example"],
              card_kind="virtual", currency="eur")
    broker = PaymentsBroker(tmp_path / "payments")
    broker.register_system(system())
    if waiver:
        per, cum = waiver
        broker.grant_waiver("vcard", user_name=NAME, typed_text=required_phrase(NAME, per, "it"), per_payment=per, cumulative=cum, read_to_end=True,
                            release_ack=True, retyped_names={"release": NAME, "acceptance": NAME}, lang="it", sites=sites)
    vault.attach_gate(broker.still_valid)
    return vault, broker


def pay(broker, vault, site="shop.example", amount="30"):
    r = broker.authorize("vcard", amount, "EUR", "Hotel", "deposit", site=site)
    vault.arm("trip", payment_id=r["payment_id"], site=r["site"])
    return r


def test_the_card_comes_out_to_the_approved_site_and_not_to_another(tmp_path):
    vault, broker = set_up(tmp_path)
    vault.put("trip", kind="card", hosts=["shop.example", "other.example"])         # the card may go to either; the payment was for one
    pay(broker, vault)
    assert vault.resolve(FORM, hosts=["shop.example"]) == "4242424242424242"
    assert vault.resolve(FORM, hosts=["pay.shop.example"]) == "4242424242424242"
    with pytest.raises(VaultError, match="approved for shop.example"):
        vault.resolve(FORM, hosts=["other.example"])


def test_the_kill_switch_closes_a_card_already_released_even_from_another_process(tmp_path):
    vault, broker = set_up(tmp_path)
    pay(broker, vault)
    assert vault.resolve(FORM) == "4242424242424242"
    PaymentsBroker(tmp_path / "payments").revoke()                                  # what the GUI does, in its own process
    with pytest.raises(VaultError, match="stopped or has ended"):
        vault.resolve(FORM)


def test_a_payment_the_person_confirmed_is_stopped_by_the_kill_switch_too(tmp_path):
    vault, broker = set_up(tmp_path, waiver=None)
    r = broker.authorize("vcard", "80", "EUR", "Hotel", "deposit", confirm=lambda req: True, site="shop.example")
    vault.arm("trip", payment_id=r["payment_id"], site=r["site"])
    assert broker.still_valid(r["payment_id"])
    PaymentsBroker(tmp_path / "payments").revoke()
    assert not broker.still_valid(r["payment_id"])
    with pytest.raises(VaultError):
        vault.resolve(FORM)


def test_an_approval_lets_the_card_out_a_few_times_not_for_ever(tmp_path):
    vault, broker = set_up(tmp_path)
    pay(broker, vault)
    for _ in range(9):
        vault.resolve(FORM)
    with pytest.raises(VaultError, match="has been used"):
        vault.resolve(FORM)
    pay(broker, vault)                                                              # a new payment is a new approval
    assert vault.resolve(FORM) == "4242424242424242"


def test_the_approval_ends_with_the_waiver_and_without_one_the_card_stays_in(tmp_path):
    vault, broker = set_up(tmp_path)
    r = pay(broker, vault)
    assert broker.still_valid(r["payment_id"]) and not broker.still_valid("pay_unknown")
    vault.arm("trip", payment_id="pay_unknown")
    with pytest.raises(VaultError):
        vault.resolve(FORM)


def test_the_question_to_the_person_does_not_hold_the_lock_so_the_kill_switch_works_while_it_is_open(tmp_path):
    vault, broker = set_up(tmp_path, waiver=("20", "120"))
    asked, released, done = threading.Event(), threading.Event(), threading.Event()

    def confirm(request):
        asked.set()
        released.wait(10)
        return False

    def run():
        try:
            broker.authorize("vcard", "80", "EUR", "Hotel", "deposit", confirm=confirm)
        except PermissionError:
            pass
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert asked.wait(5)
    finished = threading.Event()
    threading.Thread(target=lambda: (PaymentsBroker(tmp_path / "payments").revoke(), finished.set()), daemon=True).start()
    assert finished.wait(5), "the revoke waited for the question to be answered"
    released.set()
    assert done.wait(5)


def test_a_waiver_can_name_the_only_sites_it_covers(tmp_path):
    vault, broker = set_up(tmp_path, sites=["https://www.shop.example/checkout"])
    assert broker.authorize("vcard", "10", "EUR", "Hotel", "deposit", site="pay.shop.example")["status"] == "authorized"
    with pytest.raises(PermissionError, match="not one of the sites"):
        broker.authorize("vcard", "10", "EUR", "Hotel", "deposit", site="evil.example")
    with pytest.raises(PermissionError, match="say which site"):
        broker.authorize("vcard", "10", "EUR", "Hotel", "deposit")
    assert broker.status()[0]["sites"] == ["shop.example"]


def test_changing_the_currency_or_the_card_of_a_system_ends_its_waiver(tmp_path):
    vault, broker = set_up(tmp_path)
    r = pay(broker, vault)
    broker.register_system(system(currency="USD"))
    assert not broker.status() and not broker.still_valid(r["payment_id"])
    vault2, broker2 = set_up(tmp_path / "again")
    broker2.register_system(system())                                               # the same system again keeps its waiver
    assert broker2.status()
    broker2.register_system(system(vault_entry="other"))
    assert not broker2.status()


def test_reading_a_value_in_the_wallet_is_recorded_without_the_value(tmp_path):
    from serve.wallet_api import WalletApi
    from harness.browser import BrowserPrefs
    vault, broker = set_up(tmp_path)
    api = WalletApi(lambda: vault, lambda: broker, BrowserPrefs(tmp_path / "b.json"), tmp_path / "profile")
    assert api.reveal({"id": "trip", "field": "number"})["value"] == "4242424242424242"
    rows = [r for r in broker.ledger.__class__(broker.ledger.path).records if r.get("action") == "vault_revealed"]
    assert rows and rows[0]["details"] == {"entry": "trip", "field": "number"}
