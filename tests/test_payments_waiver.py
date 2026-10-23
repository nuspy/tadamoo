import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.audit import AuditLedger
from harness.payments_broker import PaymentsBroker, register_payment_tool, to_cents
from harness.permissions import PermissionLevel, PermissionManager
from harness.tools.registry import ToolRegistry
from harness.waiver import PaymentSystem, parse_amount, required_phrase, verify_phrase
from harness.waiver.dossier import DossierStore, DossierUnavailable
from harness.waiver.keystore import load_or_create_key

NAME = "Mario Rossi"


def system(**kw):
    base = dict(system_id="vcard", name="Carta virtuale Test", kind="virtual card", credential_kind="delegated_card",
                currency="EUR", provider_terms_url="https://provider.example/terms", provider_terms_checked_on="2026-10-24",
                revoke_instructions="Block the card in the provider app")
    return PaymentSystem(**{**base, **kw})


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def broker(tmp_path, clock):
    b = PaymentsBroker(tmp_path / "data", clock=clock)
    b.register_system(system())
    return b


def grant(broker, per="50", cum="120", lang="it", **kw):
    args = dict(user_name=NAME, typed_text=required_phrase(NAME, per, lang), per_payment=per, cumulative=cum,
                read_to_end=True, release_ack=True, retyped_names={"release": NAME, "acceptance": NAME}, lang=lang)
    args.update(kw)
    return broker.grant_waiver("vcard", **args)


# -- the declaration ---------------------------------------------------------------------------------

@pytest.mark.parametrize("lang", ["it", "en"])
def test_the_dictated_declaration_is_accepted_in_both_languages(lang):
    text = required_phrase(NAME, "50", lang)
    assert verify_phrase(text, NAME, Decimal("50"), "EUR") == (lang, Decimal("50"))
    assert verify_phrase("  " + text.upper().replace(" ", "   ") + "\n", NAME, Decimal("50"), "EUR")[0] == lang


def test_italian_text_is_the_one_dictated():
    assert required_phrase(NAME, "50 EUR") == ("Io Mario Rossi hackero la applicazione sotto la mia responsabilità di quanto "
                                               "accade, autorizzando il pagamento automatico senza mia conferma fino a 50 EUR massimo.")


@pytest.mark.parametrize("typed,problem", [
    ("Io Mario Rossi hackero la applicazione.", "does not match"),
    (required_phrase("Luca Bianchi", "50"), "does not match"),
    (required_phrase(NAME, "500"), "not the amount you chose"),
    (required_phrase(NAME, "50 USD"), "currency"),
    (required_phrase(NAME, ""), "missing"),
    (required_phrase(NAME, "molto"), "plain number"),
    (required_phrase(NAME, "0"), "greater than zero"),
])
def test_a_wrong_declaration_is_refused(typed, problem):
    with pytest.raises(ValueError, match=problem):
        verify_phrase(typed, NAME, Decimal("50"), "EUR")


def test_the_name_needs_first_and_last_name():
    with pytest.raises(ValueError, match="first and last"):
        verify_phrase(required_phrase("Mario", "50"), "Mario", Decimal("50"), "EUR")


def test_amount_parsing():
    assert parse_amount("50") == (Decimal("50"), None)
    assert parse_amount("12,5 EUR") == (Decimal("12.5"), "EUR")
    assert parse_amount("€ 30") == (Decimal("30"), "EUR")
    assert parse_amount("30$") == (Decimal("30"), "USD")
    for bad in ("", "1e5", "-5", "5.123", "1,000", "abc", "5 zorkmids"):
        with pytest.raises(ValueError):
            parse_amount(bad)


# -- payment-system cards ----------------------------------------------------------------------------

def test_a_system_that_needs_a_password_or_code_is_not_eligible():
    for kind in ("password", "password_totp", "login", "anything_else"):
        assert any("delegated" in p for p in system(credential_kind=kind).problems())
    assert system().problems() == []


def test_an_incomplete_card_lists_what_is_missing():
    problems = system(provider_terms_url="", revoke_instructions="", currency="E", system_id="Bad Id").problems()
    assert len(problems) == 4


# -- granting ----------------------------------------------------------------------------------------

def test_the_waiver_needs_every_step(broker):
    cases = [dict(read_to_end=False), dict(release_ack=False),
             dict(retyped_names={"release": NAME, "acceptance": "Luigi Verdi"}), dict(retyped_names={"release": NAME}),
             dict(cum="10"), dict(ttl_days=0), dict(ttl_days=400), dict(typed_text="I agree")]
    for kw in cases:
        with pytest.raises(ValueError):
            grant(broker, **kw)
    assert broker.status() == []


def test_granting_seals_an_encrypted_dossier_and_a_ledger_entry(broker, tmp_path):
    w = grant(broker, pasted=True, typing_intervals_ms=[120, 95, 130])
    blob = (tmp_path / "data" / "dossiers" / f"{w['dossier_id']}.bin").read_bytes()
    assert NAME.encode() not in blob and b"hackero" not in blob              # encrypted at rest
    rec = broker.dossiers.open(w["dossier_id"])
    assert rec["declaration"]["name"] == NAME and rec["declaration"]["pasted"] is True
    assert rec["declaration"]["typing_intervals_ms"] == [120, 95, 130]
    assert rec["limits"] == {"per_payment": "50.00 EUR", "cumulative": "120.00 EUR", "currency": "EUR"}
    assert rec["payment_system"]["how_to_revoke"] and rec["dialogs_shown"]["waiver"]
    assert rec["identity"]["user"] and rec["identity"]["machine_hash"] and rec["time"]["utc"].endswith("+00:00")
    granted = [r for r in broker.ledger.records if r["action"] == "waiver_granted"][0]
    assert granted["details"]["dossier_id"] == w["dossier_id"] and len(granted["details"]["dossier_sha256"]) == 64
    assert broker.ledger.verify_integrity() == (True, None)


def test_a_tampered_dossier_or_another_device_key_cannot_be_opened(broker, tmp_path):
    w = grant(broker)
    path = tmp_path / "data" / "dossiers" / f"{w['dossier_id']}.bin"
    data = bytearray(path.read_bytes())
    data[30] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="altered"):
        broker.dossiers.open(w["dossier_id"])
    other = DossierStore(tmp_path / "data" / "dossiers", b"\x01" * 32)
    path.write_bytes(bytes(bytearray(data)))
    with pytest.raises(ValueError):
        other.open(w["dossier_id"])
    with pytest.raises(ValueError):
        broker.dossiers.open("../../etc/passwd")


def test_without_the_crypto_package_no_waiver_is_given(broker, monkeypatch):
    import harness.waiver.dossier as d
    monkeypatch.setattr(d, "_aead", lambda: (_ for _ in ()).throw(DossierUnavailable("needs cryptography")))
    with pytest.raises(DossierUnavailable):
        grant(broker)
    assert broker.status() == []


def test_the_device_key_is_created_once_and_reused(tmp_path):
    k1 = load_or_create_key(tmp_path / "k" / "device.key")
    assert len(k1) == 32 and load_or_create_key(tmp_path / "k" / "device.key") == k1
    assert k1 not in (tmp_path / "k" / "device.key").read_bytes() or sys.platform != "win32"


# -- authorizing -------------------------------------------------------------------------------------

def test_amounts_are_exact_cents():
    assert to_cents("12.50") == 1250 and to_cents(3) == 300 and to_cents(Decimal("0.01")) == 1
    assert to_cents("5") == 500
    for bad in (float("nan"), float("inf"), -1, 0, True, "abc", "1.005", None, [1], "1e400", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            to_cents(bad)


def test_payment_within_the_caps_is_authorized_and_counted(broker):
    grant(broker)
    seen = []
    broker.notify = seen.append
    r1 = broker.authorize("vcard", "30", "EUR", "Hotel Cervinia", "deposit")
    r2 = broker.authorize("vcard", "50.00", "eur", "Skipass", "tickets")
    assert (r1["mode"], r1["remaining"]) == ("waiver", "90.00 EUR") and r2["remaining"] == "40.00 EUR"
    assert [s["recipient"] for s in seen] == ["Hotel Cervinia", "Skipass"]
    assert broker.status()[0]["spent"] == "80.00 EUR"


def test_over_the_cap_per_payment_never_goes_through(broker):
    grant(broker)
    with pytest.raises(PermissionError, match="per payment"):
        broker.authorize("vcard", "50.01", "EUR", "x", "y")
    assert not [r for r in broker.ledger.records if r["action"] == "payment_authorized"]


def test_the_cumulative_cap_holds_across_a_restart(broker, tmp_path, clock):
    grant(broker)
    broker.authorize("vcard", "50", "EUR", "a", "r")
    broker.authorize("vcard", "50", "EUR", "b", "r")
    again = PaymentsBroker(tmp_path / "data", clock=clock)                   # a new process
    with pytest.raises(PermissionError, match="cumulative cap, 20.00 EUR left"):
        again.authorize("vcard", "30", "EUR", "c", "r")
    assert again.authorize("vcard", "20", "EUR", "d", "r")["remaining"] == "0.00 EUR"
    with pytest.raises(PermissionError, match="cumulative"):
        again.authorize("vcard", "0.01", "EUR", "e", "r")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5, 0, True, "x", "1.001", None])
def test_malformed_amounts_are_rejected_before_any_decision(broker, bad):
    grant(broker)
    with pytest.raises(ValueError):
        broker.authorize("vcard", bad, "EUR", "x", "y")
    assert broker.status()[0]["spent"] == "0.00 EUR"


def test_wrong_currency_unknown_system_and_missing_recipient(broker):
    grant(broker)
    with pytest.raises(PermissionError, match="pays in EUR"):
        broker.authorize("vcard", "5", "USD", "x", "y")
    with pytest.raises(PermissionError, match="Unknown"):
        broker.authorize("other", "5", "EUR", "x", "y")
    with pytest.raises(ValueError, match="recipient"):
        broker.authorize("vcard", "5", "EUR", "  ", "y")


def test_without_a_waiver_nothing_is_paid_unless_the_user_confirms(broker):
    with pytest.raises(PermissionError, match="no active waiver"):
        broker.authorize("vcard", "5", "EUR", "x", "y")
    asked = []
    ok = broker.authorize("vcard", "5", "EUR", "x", "y", confirm=lambda req: asked.append(req) or True)
    assert ok["mode"] == "confirmed" and asked[0]["amount"] == "5.00 EUR"
    with pytest.raises(PermissionError):
        broker.authorize("vcard", "5", "EUR", "x", "y", confirm=lambda req: False)
    assert [r["action"] for r in broker.ledger.records if r["action"].startswith("payment")] == [
        "payment_refused", "payment_confirmed", "payment_refused"]


def test_over_the_cap_falls_back_to_a_confirmation_at_the_levels_that_have_one(broker):
    grant(broker)
    r = broker.authorize("vcard", "80", "EUR", "x", "y", confirm=lambda req: True)
    assert r["mode"] == "confirmed"
    assert broker.status()[0]["spent"] == "0.00 EUR"                         # confirmed payments do not eat the waiver


def test_the_waiver_expires(broker, clock):
    grant(broker, ttl_days=2)
    assert broker.authorize("vcard", "5", "EUR", "x", "y")
    clock.t += 3 * 86400
    with pytest.raises(PermissionError, match="no active waiver"):
        broker.authorize("vcard", "5", "EUR", "x", "y")
    assert broker.status() == []


def test_revoking_stops_payments_at_once_everywhere_and_keeps_the_record(broker, tmp_path, clock):
    grant(broker)
    broker.authorize("vcard", "10", "EUR", "x", "y")
    gui = PaymentsBroker(tmp_path / "data", clock=clock)                     # another process revokes
    assert gui.revoke("vcard", reason="user clicked revoke") == 1
    with pytest.raises(PermissionError):
        broker.authorize("vcard", "1", "EUR", "x", "y")
    assert gui.revoke() == 0
    actions = [r["action"] for r in broker.ledger.records]
    assert "waiver_revoked" in actions and AuditLedger(tmp_path / "data" / "payments.jsonl").verify_integrity()[0]


def test_a_new_waiver_replaces_the_old_one_and_resets_the_running_total(broker):
    grant(broker, per="50", cum="60")
    broker.authorize("vcard", "50", "EUR", "x", "y")
    grant(broker, per="20", cum="40")
    assert len([w for w in json.loads((broker.state_path).read_text(encoding="utf-8"))["waivers"] if not w.get("revoked_at")]) == 1
    with pytest.raises(PermissionError, match="per payment"):
        broker.authorize("vcard", "30", "EUR", "x", "y")
    assert broker.authorize("vcard", "20", "EUR", "x", "y")["remaining"] == "20.00 EUR"


def test_a_tampered_state_file_stops_all_payments(broker, tmp_path):
    grant(broker)
    state = json.loads(broker.state_path.read_text(encoding="utf-8"))
    state["waivers"][0]["per_payment_cents"] = 10_000_000
    state["waivers"][0]["cumulative_cents"] = 10_000_000
    broker.state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(PermissionError, match="failed verification"):
        broker.authorize("vcard", "5000", "EUR", "x", "y")
    broker.state_path.write_text("not json", encoding="utf-8")
    with pytest.raises(PermissionError, match="failed verification"):
        broker.authorize("vcard", "5", "EUR", "x", "y", confirm=lambda r: True)


def test_a_state_file_signed_with_another_key_is_refused(broker, tmp_path):
    grant(broker)
    forger = PaymentsBroker(tmp_path / "forge", key=b"\x07" * 32)
    forger.register_system(system())
    broker.state_path.write_text((tmp_path / "forge" / "payments_state.json").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(PermissionError, match="failed verification"):
        broker.authorize("vcard", "5", "EUR", "x", "y")


def test_a_tampered_ledger_stops_all_payments(broker, tmp_path):
    grant(broker)
    broker.authorize("vcard", "10", "EUR", "x", "y")
    ledger = tmp_path / "data" / "payments.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["details"]["amount_cents"] = 1                                      # hide most of what was spent
    ledger.write_text("\n".join(lines[:-1] + [json.dumps(last)]) + "\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="ledger failed verification"):
        broker.authorize("vcard", "5", "EUR", "x", "y")


def test_a_failing_notification_does_not_undo_the_payment(broker):
    grant(broker)
    broker.notify = lambda r: 1 / 0
    assert broker.authorize("vcard", "5", "EUR", "x", "y")["status"] == "authorized"
    assert broker.status()[0]["spent"] == "5.00 EUR"


def test_register_rejects_an_ineligible_system(broker):
    with pytest.raises(ValueError, match="delegated"):
        broker.register_system(system(system_id="bank", credential_kind="password_totp"))


def test_the_agent_tool_only_asks_and_the_broker_decides(broker):
    grant(broker)
    registry = ToolRegistry()
    perms = PermissionManager(level=PermissionLevel.DEFAULT)
    register_payment_tool(registry, broker, perms)
    ok = registry.execute("request_payment", {"system_id": "vcard", "amount": "25", "currency": "EUR",
                                              "recipient": "Hotel", "reason": "deposit"})
    assert ok["status"] == "authorized" and ok["executed"] is False
    with pytest.raises(PermissionError):
        registry.execute("request_payment", {"system_id": "vcard", "amount": "500", "currency": "EUR",
                                             "recipient": "Hotel", "reason": "deposit"})
    assert perms.evaluate("request_payment", registry.get("request_payment").effect, {})[0]
    assert not perms.evaluate("request_payment", registry.get("request_payment").effect, {})[0] if perms.plan_mode else True
    plan = PermissionManager(level=PermissionLevel.AUTO, plan_mode=True)
    assert not plan.evaluate("request_payment", registry.get("request_payment").effect, {})[0]
