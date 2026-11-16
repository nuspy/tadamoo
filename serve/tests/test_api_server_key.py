"""Changing the server's key from a client (serve/server_key.py) over the real server: the key in force changes at once, the old one stops working from the network and is still believed from
this machine until a restart, a wrong current key or a bad new one is refused, and the new key is kept in the data folder. All keys here are made up for the test."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import handler, server_key
from serve.tests.test_api_plans import PlansApiBase

OLD, NEW, THIRD = "test-old-key-0001", "test-new-key-0002", "test-third-key-0003"


class ServerKeyApiTest(PlansApiBase):
    api_key = OLD

    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(handler.userdata, "path", lambda name: Path(self.data.name) / name)
        self.patch.start()
        self.svc.api_key, self.svc.retired_keys = OLD, set()
        self.keyfile = Path(self.data.name) / "server_key.json"

    def tearDown(self):
        self.patch.stop()
        self.data.cleanup()
        self.svc.api_key, self.svc.retired_keys = OLD, set()

    def with_key(self, key):
        return {"Authorization": f"Bearer {key}"}

    def change(self, key, current, new):
        return self.call("POST", "/api/server-key", {"current": current, "new": new}, headers=self.with_key(key))

    def test_the_key_changes_at_once_and_the_old_one_stops_working_from_the_network(self):
        code, res = self.call("GET", "/api/server-key", headers=self.with_key(OLD))
        self.assertEqual((code, res["set"], res["kept"]), (200, True, False))
        self.assertNotIn(OLD, json.dumps(res))
        code, res = self.change(OLD, OLD, NEW)
        self.assertEqual((code, res["changed"], res["applied"]), (200, True, True))
        self.assertEqual(self.svc.api_key, NEW)
        self.assertEqual(json.loads(self.keyfile.read_text(encoding="utf-8"))["key"], NEW)           # kept in the data folder
        self.assertEqual(self.call("GET", "/api/server-key", headers=self.with_key(NEW))[1]["kept"], True)
        self.assertEqual(self.call("GET", "/api/server-key", headers=self.with_key(NEW))[0], 200)
        self.assertEqual(self.call("GET", "/api/plans", headers=self.with_key(NEW))[0], 200)

    def test_the_previous_key_is_believed_only_from_this_machine_and_only_until_a_restart(self):
        self.assertEqual(self.change(OLD, OLD, NEW)[0], 200)
        self.assertIn(OLD, self.svc.retired_keys)
        self.assertEqual(self.call("GET", "/api/plans", headers=self.with_key(OLD))[0], 200)           # the test client is on this machine: a conversation already running here
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):                    # the same request from the network
            self.assertEqual(self.call("GET", "/api/plans", headers=self.with_key(OLD))[0], 401)
            self.assertEqual(self.call("GET", "/api/plans", headers=self.with_key(NEW))[0], 200)
        self.svc.retired_keys = set()                                                                       # a restart
        self.assertEqual(self.call("GET", "/api/plans", headers=self.with_key(OLD))[0], 401)

    def test_a_wrong_current_key_a_bad_new_one_and_the_same_key_are_refused(self):
        code, res = self.change(OLD, "not-the-key", NEW)
        self.assertEqual((code, res["error"]["code"]), (403, "wrong_key"))
        for bad in ("", "   ", "two\nlines", "x" * 201, None, 7):
            code, res = self.change(OLD, OLD, bad)
            self.assertEqual((code, res["error"]["code"]), (400, "bad_key"), bad)
        code, res = self.change(OLD, OLD, OLD)
        self.assertEqual((code, res["error"]["code"]), (400, "same_key"))
        self.assertEqual(self.svc.api_key, OLD)
        self.assertFalse(self.keyfile.exists())

    def test_without_the_current_key_in_the_header_nothing_changes(self):
        code, _ = self.call("POST", "/api/server-key", {"current": OLD, "new": NEW})
        self.assertEqual(code, 401)
        self.assertEqual(self.svc.api_key, OLD)

    def test_the_key_can_be_changed_again_and_the_holders_of_the_key_follow(self):
        class Holder:
            api_key = OLD
        h = Holder()
        store = server_key.KeyStore(self.keyfile)
        server_key.change(self.svc, store, OLD, NEW, False, [h, None])
        self.assertEqual(h.api_key, NEW)
        server_key.change(self.svc, store, NEW, THIRD, False, [h])
        self.assertEqual((self.svc.api_key, h.api_key, store.stored()), (THIRD, THIRD, THIRD))
        self.assertEqual(self.svc.retired_keys, {OLD, NEW})

    def test_a_server_with_no_key_gets_one_only_from_this_machine(self):
        store = server_key.KeyStore(self.keyfile)
        self.svc.api_key = ""
        with self.assertRaises(server_key.KeyChangeRefused) as caught:
            server_key.change(self.svc, store, "", NEW, False)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(server_key.change(self.svc, store, "", NEW, True)["applied"], True)
        self.assertEqual(self.svc.api_key, NEW)


if __name__ == "__main__":
    unittest.main()
