"""The contacts store: categories, permission types (CRUD and reorder), groups with default permissions, contacts with overrides, and the
effective-permission, phone-match and voice-match lookups the telephony tools use."""
import pytest

from tadamoo.contacts import ContactError, ContactStore
from tadamoo.speech import voiceid


@pytest.fixture
def store(tmp_path):
    return ContactStore(tmp_path / "contacts.json")


def test_a_fresh_store_is_empty_and_survives_a_reload(store):
    data = store.load()
    assert data == {"schema": "1.0", "permission_categories": [], "permission_types": [], "groups": [], "contacts": []}
    cat = store.add_category("Documenti")
    assert store.load()["permission_categories"] == [cat]


def test_a_category_in_use_cannot_be_removed_but_an_empty_one_can(store):
    cat = store.add_category("Documenti")
    t = store.add_type(cat["id"], "Invia fatture")
    with pytest.raises(ContactError, match="1 permission type"):
        store.remove_category(cat["id"])
    store.remove_type(t["id"])
    store.remove_category(cat["id"])
    assert store.load()["permission_categories"] == []
    with pytest.raises(ContactError):
        store.remove_category(cat["id"])


def test_a_type_needs_a_real_category_and_can_be_renamed(store):
    cat = store.add_category("Documenti")
    with pytest.raises(ContactError):
        store.add_type("bogus", "Invia fatture")
    t = store.add_type(cat["id"], "Invia fatture")
    store.update_type(t["id"], label="Invia tutte le fatture")
    assert store.load()["permission_types"][0]["label"] == "Invia tutte le fatture"
    other = store.add_category("Informazioni")
    store.update_type(t["id"], category_id=other["id"])
    assert store.load()["permission_types"][0]["category"] == other["id"]


def test_removing_a_type_clears_it_from_every_group_and_contact(store):
    cat = store.add_category("Documenti")
    t = store.add_type(cat["id"], "Invia fatture")
    g = store.add_group("Bookkeeping", {t["id"]: True})
    c = store.add_contact("Mario", group_id=g["id"], overrides={t["id"]: False})
    store.remove_type(t["id"])
    data = store.load()
    assert data["groups"][0]["permissions"] == {} and data["contacts"][0]["overrides"] == {}


def test_reorder_needs_every_existing_type_exactly_once(store):
    cat = store.add_category("Documenti")
    a = store.add_type(cat["id"], "A")
    b = store.add_type(cat["id"], "B")
    store.reorder_types([b["id"], a["id"]])
    assert [t["id"] for t in store.load()["permission_types"]] == [b["id"], a["id"]]
    with pytest.raises(ContactError):
        store.reorder_types([a["id"]])
    with pytest.raises(ContactError):
        store.reorder_types([a["id"], a["id"]])
    with pytest.raises(ContactError):
        store.reorder_types([a["id"], "bogus"])


def test_a_group_permission_needs_a_real_type_and_a_boolean(store):
    cat = store.add_category("Documenti")
    t = store.add_type(cat["id"], "Invia fatture")
    with pytest.raises(ContactError):
        store.add_group("Bookkeeping", {"bogus": True})
    with pytest.raises(ContactError):
        store.add_group("Bookkeeping", {t["id"]: "yes"})
    g = store.add_group("Bookkeeping", {t["id"]: True})
    assert g["permissions"] == {t["id"]: True}


def test_removing_a_group_loosens_its_contacts_instead_of_deleting_them(store):
    g = store.add_group("Bookkeeping")
    c = store.add_contact("Mario", group_id=g["id"])
    store.remove_group(g["id"])
    data = store.load()
    assert data["contacts"][0]["id"] == c["id"] and data["contacts"][0]["group_id"] == ""


def test_a_contact_needs_a_name_valid_phones_and_a_real_group(store):
    with pytest.raises(ContactError):
        store.add_contact("")
    with pytest.raises(ContactError):
        store.add_contact("Mario", group_id="bogus")
    with pytest.raises(ContactError):
        store.add_contact("Mario", phones=["not a number"])
    with pytest.raises(ContactError):
        store.add_contact("Mario", emails=["not an address"])
    c = store.add_contact("Mario", organization="Bookkeeping SRL", role="Contabile",
                           phones=[{"number": "+39 333 123 4567", "label": "mobile"}], emails=[{"address": "mario@example.com"}])
    assert c["phones"] == [{"number": "+393331234567", "label": "mobile"}]
    assert c["emails"] == [{"address": "mario@example.com", "label": ""}]


def test_update_contact_replaces_only_the_fields_given(store):
    c = store.add_contact("Mario", organization="Bookkeeping SRL")
    store.update_contact(c["id"], role="Contabile")
    row = store.load()["contacts"][0]
    assert row["name"] == "Mario" and row["organization"] == "Bookkeeping SRL" and row["role"] == "Contabile"
    with pytest.raises(ContactError):
        store.update_contact(c["id"], name="")
    with pytest.raises(ContactError):
        store.update_contact("bogus", name="x")


def test_effective_permissions_are_the_override_else_the_group_else_denied(store):
    cat = store.add_category("Documenti")
    send_invoices = store.add_type(cat["id"], "Invia fatture")
    send_statements = store.add_type(cat["id"], "Invia estratti conto")
    say_value = store.add_type(cat["id"], "Dire il valore per telefono")
    g = store.add_group("Bookkeeping", {send_statements["id"]: True})
    # the group's own default: only send_statements granted, the other two denied by omission
    group_only = store.add_contact("Luigi", group_id=g["id"])
    data = store.load()
    eff = store.effective_permissions(group_only, data)
    assert eff == {send_invoices["id"]: False, send_statements["id"]: True, say_value["id"]: False}
    # one member of the same group handles tenders, not accounts: excluded from what the group grants, and separately given something the group does not
    excluded = store.add_contact("Anna", group_id=g["id"], overrides={send_statements["id"]: False, send_invoices["id"]: True})
    eff2 = store.effective_permissions(excluded, store.load())
    assert eff2 == {send_invoices["id"]: True, send_statements["id"]: False, say_value["id"]: False}
    # no group at all: only its own overrides count, nothing is inherited from anywhere
    loner = store.add_contact("Carla", overrides={send_invoices["id"]: True})
    eff3 = store.effective_permissions(loner, store.load())
    assert eff3 == {send_invoices["id"]: True, send_statements["id"]: False, say_value["id"]: False}


def test_match_phone_finds_a_contact_regardless_of_spaces_and_dashes_and_an_unknown_number_matches_nothing(store):
    store.add_contact("Mario", phones=[{"number": "+39 333 123 4567"}])
    found = store.match_phone("+39-333-123-4567")
    assert found is not None and found["name"] == "Mario"
    assert store.match_phone("0039 333 123 4567") is None          # no international-prefix normalization in this phase: +39 and 0039 differ
    assert store.match_phone("+39 333 000 0000") is None
    assert store.match_phone("") is None


def _fake_read(table):
    """A stand-in for voiceid.read(wav) that looks `wav` up in `table` (wav -> (embedding, f0_hz)) instead of running the real
    WavLM model - keeps these tests fast and independent of the model being installed."""
    return lambda wav: {"embedding": table[wav][0], "f0_hz": table[wav][1]}


def test_is_self_allows_only_one_contact_at_a_time(store):
    a = store.add_contact("Io", is_self=True)
    with pytest.raises(ContactError, match="yourself"):
        store.add_contact("Anche io", is_self=True)
    b = store.add_contact("Altro")
    with pytest.raises(ContactError, match="yourself"):
        store.update_contact(b["id"], is_self=True)
    store.update_contact(a["id"], is_self=False)
    store.update_contact(b["id"], is_self=True)
    assert store.self_contact()["id"] == b["id"]


def test_enroll_voice_accumulates_readings_and_seeds_a_phone_reading_from_a_mic_one(store, monkeypatch):
    mario = store.add_contact("Mario")
    monkeypatch.setattr(voiceid, "read", _fake_read({b"m1": ([1.0, 0.0], 110.0), b"m2": ([0.9, 0.1], 112.0)}))
    monkeypatch.setattr(voiceid, "phone_proxy", lambda wav: wav)
    store.enroll_voice(mario["id"], [b"m1", b"m2"], "mic")
    voice = store.load()["contacts"][0]["voice"]
    assert len(voice["embeddings"]["mic"]) == 2
    assert len(voice["embeddings"]["phone"]) == 2                  # one phone-proxy reading seeded per mic reading
    assert voice["f0_hz"] == pytest.approx(111.0, abs=0.1)
    with pytest.raises(ContactError):
        store.enroll_voice(mario["id"], [], "mic")
    with pytest.raises(ContactError):
        store.enroll_voice(mario["id"], [b"m1"], "bogus")


def test_match_voice_finds_the_closest_enrolled_contact_and_nothing_for_an_unenrolled_voice(store, monkeypatch):
    mario = store.add_contact("Mario")
    anna = store.add_contact("Anna")
    monkeypatch.setattr(voiceid, "read", _fake_read({b"mario-1": ([1.0, 0.0], 110.0), b"anna-1": ([0.0, 1.0], 220.0)}))
    monkeypatch.setattr(voiceid, "phone_proxy", lambda wav: wav)
    store.enroll_voice(mario["id"], [b"mario-1"], "mic")
    store.enroll_voice(anna["id"], [b"anna-1"], "mic")
    found = store.match_voice([1.0, 0.0], 110.0)
    assert found is not None and found["contact"]["name"] == "Mario" and found["confident"]
    assert store.match_voice([-1.0, 0.0], 300.0) is None            # nobody close enough, on either branch
