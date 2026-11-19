"""The contacts store: categories, permission types (CRUD and reorder), groups with default permissions, contacts with overrides, and the
effective-permission and phone-match lookups the telephony tools use."""
import pytest

from tadamoo.contacts import ContactError, ContactStore


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
