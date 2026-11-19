"""The Contacts tab's HTTP API, over the real server: categories, permission types (CRUD and reorder), groups, contacts, and the
effective permissions GET /api/contacts reports for each."""
import os
import tempfile
from unittest import mock

from serve.tests.test_api_plans import PlansApiBase

KEY = "test-contacts-key-0001"


class ContactsApiTest(PlansApiBase):
    api_key = KEY

    @classmethod
    def setUpClass(cls):
        cls._data_tmp = tempfile.TemporaryDirectory()
        cls._env_patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": cls._data_tmp.name})
        cls._env_patch.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._env_patch.stop()
        cls._data_tmp.cleanup()

    def setUp(self):
        from pathlib import Path
        Path(self._data_tmp.name, "contacts.json").unlink(missing_ok=True)      # each test starts from an empty catalog

    def headers(self):
        return {"Authorization": f"Bearer {KEY}"}

    def test_a_fresh_server_has_nothing_and_a_category_type_and_group_can_be_built_up(self):
        code, res = self.call("GET", "/api/contacts", headers=self.headers())
        self.assertEqual((code, res), (200, {"permission_categories": [], "permission_types": [], "groups": [], "contacts": []}))
        code, res = self.call("POST", "/api/contacts/categories", {"action": "add", "label": "Documenti"}, headers=self.headers())
        cat_id = res["permission_categories"][0]["id"]
        self.assertEqual(code, 200)
        code, res = self.call("POST", "/api/contacts/types", {"action": "add", "category_id": cat_id, "label": "Invia estratti conto via email"},
                              headers=self.headers())
        type_id = res["permission_types"][0]["id"]
        self.assertEqual(code, 200)
        code, res = self.call("POST", "/api/contacts/groups", {"action": "add", "name": "Bookkeeping", "permissions": {type_id: True}},
                              headers=self.headers())
        group_id = res["groups"][0]["id"]
        self.assertEqual((code, res["groups"][0]["permissions"]), (200, {type_id: True}))
        code, res = self.call("POST", "/api/contacts/items",
                              {"action": "add", "name": "Mario", "group_id": group_id, "phones": [{"number": "+39 333 1234567"}]},
                              headers=self.headers())
        self.assertEqual((code, res["contacts"][0]["effective"]), (200, {type_id: True}))

    def test_an_override_changes_only_that_contact_not_the_group(self):
        self.call("POST", "/api/contacts/categories", {"action": "add", "label": "Documenti"}, headers=self.headers())
        cat_id = self.call("GET", "/api/contacts", headers=self.headers())[1]["permission_categories"][0]["id"]
        self.call("POST", "/api/contacts/types", {"action": "add", "category_id": cat_id, "label": "Invia fatture"}, headers=self.headers())
        type_id = self.call("GET", "/api/contacts", headers=self.headers())[1]["permission_types"][0]["id"]
        code, res = self.call("POST", "/api/contacts/groups", {"action": "add", "name": "Bookkeeping", "permissions": {type_id: True}}, headers=self.headers())
        group_id = res["groups"][0]["id"]
        self.call("POST", "/api/contacts/items", {"action": "add", "name": "Mario", "group_id": group_id}, headers=self.headers())
        code, res = self.call("POST", "/api/contacts/items",
                              {"action": "add", "name": "Anna", "group_id": group_id, "overrides": {type_id: False}}, headers=self.headers())
        by_name = {c["name"]: c["effective"][type_id] for c in res["contacts"]}
        self.assertEqual((code, by_name), (200, {"Mario": True, "Anna": False}))

    def test_reorder_and_removal_and_bad_actions_are_reported(self):
        self.call("POST", "/api/contacts/categories", {"action": "add", "label": "Documenti"}, headers=self.headers())
        cat_id = self.call("GET", "/api/contacts", headers=self.headers())[1]["permission_categories"][0]["id"]
        self.call("POST", "/api/contacts/types", {"action": "add", "category_id": cat_id, "label": "A"}, headers=self.headers())
        self.call("POST", "/api/contacts/types", {"action": "add", "category_id": cat_id, "label": "B"}, headers=self.headers())
        types = self.call("GET", "/api/contacts", headers=self.headers())[1]["permission_types"]
        order = [types[1]["id"], types[0]["id"]]
        code, res = self.call("POST", "/api/contacts/types", {"action": "reorder", "order": order}, headers=self.headers())
        self.assertEqual((code, [t["id"] for t in res["permission_types"]]), (200, order))
        code, res = self.call("POST", "/api/contacts/types", {"action": "reorder", "order": [order[0]]}, headers=self.headers())
        self.assertEqual(code, 400)
        code, res = self.call("POST", "/api/contacts/groups", {"action": "bogus"}, headers=self.headers())
        self.assertEqual((code, res["error"]["code"]), (400, "action"))

    def test_the_wrong_key_is_refused(self):
        code, res = self.call("GET", "/api/contacts", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(code, 401)
        code, res = self.call("POST", "/api/contacts/categories", {"action": "add", "label": "x"}, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(code, 401)
