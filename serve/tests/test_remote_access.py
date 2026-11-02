"""Phones: several API keys, what a client from outside this PC may reach, and the setting that turns them on."""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import handler, remote
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.remote import ApiKeys, RemoteAccess, listen_of
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class KeysTest(unittest.TestCase):
    def test_a_key_is_shown_once_kept_only_as_a_hash_checked_and_revoked_on_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = ApiKeys(Path(tmp) / "k.json")
            self.assertFalse(keys.any())
            row, key = keys.create("  Galaxy   S25  ")
            self.assertTrue(key.startswith("bsk_") and len(key) > 40)
            self.assertEqual(row["label"], "Galaxy S25")
            self.assertNotIn(key, (Path(tmp) / "k.json").read_text(encoding="utf-8"))
            self.assertNotIn("hash", keys.list()[0])
            self.assertEqual(keys.verify(key)["id"], row["id"])
            other_row, other = keys.create("Tablet")
            self.assertEqual({r["label"] for r in keys.list()}, {"Galaxy S25", "Tablet"})
            for wrong in ("", "bsk_nope", key[:-1], key + "x", "Bearer " + key):
                self.assertIsNone(keys.verify(wrong), wrong)
            self.assertTrue(keys.revoke(row["id"]))
            self.assertIsNone(keys.verify(key))
            self.assertEqual(keys.verify(other)["label"], "Tablet")
            self.assertFalse(keys.revoke("zzzz"))
            self.assertEqual([r["label"] for r in ApiKeys(Path(tmp) / "k.json").list()], ["Tablet"])          # kept between runs
            with self.assertRaises(ValueError):
                keys.create("   ")

    def test_a_key_can_be_shown_again_for_a_reinstalled_phone_but_is_never_in_the_file_in_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, sealing = Path(tmp) / "k.json", bytes(range(32))
            keys = ApiKeys(path, key=sealing)
            row, key = keys.create("Galaxy")
            self.assertTrue(row["recoverable"] and keys.list()[0]["recoverable"])
            self.assertNotIn(key, path.read_text(encoding="utf-8"))
            self.assertEqual(ApiKeys(path, key=sealing).reveal(row["id"]), ("Galaxy", key))                      # after a restart
            self.assertIsNone(ApiKeys(path, key=bytes(32)).reveal(row["id"]))                                    # another device key opens nothing
            self.assertIsNone(keys.reveal("zzzz"))
            self.assertTrue(keys.revoke(row["id"]))
            self.assertIsNone(keys.reveal(row["id"]))

    def test_a_key_made_without_a_device_key_is_shown_once_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = ApiKeys(Path(tmp) / "k.json")
            row, key = keys.create("Old")
            self.assertFalse(row["recoverable"])
            self.assertIsNone(keys.reveal(row["id"]))
            self.assertEqual(keys.verify(key)["label"], "Old")

    def test_the_address_to_listen_on_is_tailscale_every_interface_or_one_ipv4_address(self):
        self.assertEqual([listen_of(v) for v in (None, "", " Tailscale ", "0.0.0.0", "192.168.1.20", " 10.0.0.7 ")],
                         ["tailscale", "tailscale", "tailscale", "0.0.0.0", "192.168.1.20", "10.0.0.7"])
        for bad in ("localhost", "192.168.1", "300.1.1.1", "::", "fd7a::1", "1.2.3.4:80"):
            with self.assertRaises(ValueError, msg=bad):
                listen_of(bad)


class Remote(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.svc.keys = ApiKeys(base / "keys.json", key=bytes(32))
        cls.svc.remote = RemoteAccess(base / "remote.json", 0, lambda ip, port: None)
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner"))]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.phone = handler._listen(cls.svc, "127.0.0.1", 0, remote=True)                 # stands for the listener on the Tailscale address
        cls.phone_base = f"http://127.0.0.1:{cls.phone.server_address[1]}"
        cls.row, cls.key = cls.svc.keys.create("Phone")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.phone.shutdown()
        cls.phone.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.svc.remote._httpd = None
        self.svc.api_key = ""

    def call(self, method, path, payload=None, key=None, outside=False, away=False):
        """`away`: the request comes from a phone's address, not from this PC (the test's own connection is on this PC)."""
        base = self.phone_base if outside else self.base
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if key:
            headers["Authorization"] = "Bearer " + key
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        try:
            with mock.patch.object(handler.settings, "is_loopback", (lambda address: False) if away else handler.settings.is_loopback):
                with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=10) as r:
                    body = r.read()
                    return r.status, (json.loads(body) if body[:1] in (b"{", b"[") else body)
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (json.loads(body) if body[:1] in (b"{", b"[") else body)

    def listen(self):
        self.svc.remote._httpd = object()                                   # as if the listener on the Tailscale address were up

    def test_this_pc_works_as_before_with_no_key_and_a_set_key_still_works_with_the_other_keys(self):
        self.assertEqual(self.call("GET", "/api/agent/sessions")[0], 200)
        self.svc.api_key = "master"
        self.assertEqual(self.call("GET", "/api/agent/sessions")[0], 401)
        self.assertEqual(self.call("GET", "/api/agent/sessions", key="master")[0], 200)
        self.assertEqual(self.call("GET", "/api/agent/sessions", key=self.key)[0], 401)                  # a phone's key counts only on the listener for phones
        self.assertEqual(self.call("GET", "/api/agent/sessions", key=self.key, outside=True)[0], 200)
        self.assertEqual(self.call("GET", "/api/agent/sessions", key="master", outside=True)[0], 200)

    def test_every_request_to_the_listener_for_phones_needs_one_of_the_keys(self):
        for path in ("/api/agent/sessions", "/", "/web/i18n.js", "/metrics", "/api/models", "/api/settings"):
            self.assertEqual(self.call("GET", path, outside=True)[0], 401, path)
            self.assertEqual(self.call("GET", path, key="bsk_wrong", outside=True)[0], 401, path)
        code, hello = self.call("GET", "/api/remote/hello", key=self.key, outside=True)
        self.assertEqual((code, hello["device"]), (200, "Phone"))

    def test_a_phone_with_a_key_reaches_every_route_a_client_on_this_pc_reaches_with_the_same_credentials(self):
        for path in ("/api/agent/sessions", "/", "/web/i18n.js", "/metrics", "/api/models", "/api/settings", "/api/plans", "/api/knowledge"):
            self.assertEqual(self.call("GET", path, key=self.key, outside=True)[0], 200, path)
        self.assertEqual(self.call("GET", "/api/remote", key=self.key, outside=True)[0], 200)             # the test's own connection is on this PC

    def test_what_answers_only_to_this_pc_stays_closed_to_a_phone_from_away(self):
        for path in ("/api/remote", "/api/memories", "/api/wallet"):
            self.assertEqual(self.call("GET", path, key=self.key, outside=True, away=True)[0], 403, path)

    def test_a_phone_may_write_only_while_the_listener_is_on(self):
        work = Path(self.tmp.name) / "work"
        work.mkdir(exist_ok=True)
        body = {"workspace": str(work), "mode": "read_only", "ask_only": True}
        self.assertEqual(self.call("POST", "/api/agent/sessions", body, key=self.key, outside=True)[0], 403)          # phones are not turned on
        self.listen()
        code, made = self.call("POST", "/api/agent/sessions", body, key=self.key, outside=True)
        self.assertEqual(code, 200)
        self.assertEqual(self.call("POST", "/api/agent/sessions", body, outside=True)[0], 401)
        for path in ("/api/control/load", "/api/remote/keys/create"):
            self.assertEqual(self.call("POST", path, {}, key=self.key, outside=True, away=True)[0], 403, path)
        self.call("POST", f"/api/agent/sessions/{made['session']['id']}/delete", {}, key=self.key, outside=True)

    def test_a_phone_adds_a_folder_and_works_in_any_folder(self):
        self.listen()
        folder = Path(self.tmp.name) / "added"
        folder.mkdir()
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.call("POST", "/api/agent/projects", {"path": str(folder)}, key=self.key, outside=True)[0], 200)
        body = {"mode": "read_only", "ask_only": True}
        self.assertEqual(self.call("POST", "/api/agent/sessions", {**body, "workspace": str(elsewhere)}, key=self.key, outside=True)[0], 200)
        self.assertEqual(self.call("POST", "/api/agent/sessions", {**body, "workspace": str(folder / ".." / "elsewhere")}, key=self.key, outside=True)[0], 200)
        routine = {"name": "n", "prompt": "p", "schedule": "daily 08:30"}
        self.assertEqual(self.call("POST", "/api/routines", {**routine, "workspace": str(elsewhere)}, key=self.key, outside=True)[0], 200)
        self.call("POST", "/api/agent/projects/remove", {"path": str(folder)})

    def test_a_key_that_was_taken_away_stops_working_at_once(self):
        row, key = self.svc.keys.create("Lost phone")
        self.assertEqual(self.call("GET", "/api/remote/hello", key=key, outside=True)[0], 200)
        self.svc.keys.revoke(row["id"])
        self.assertEqual(self.call("GET", "/api/remote/hello", key=key, outside=True)[0], 401)

    def test_the_setup_routes_work_from_this_pc_only_and_a_key_can_be_shown_again_there(self):
        with mock.patch.object(handler, "tailscale_status", lambda: {"installed": True, "state": "Running", "ip": "100.64.1.2", "dns_name": "pc.tail.ts.net", "version": "1"}):
            code, status = self.call("GET", "/api/remote")
            self.assertEqual((code, status["tailscale"]["ip"], status["enabled"], status["listening"]), (200, "100.64.1.2", False, False))
            code, made = self.call("POST", "/api/remote/keys/create", {"label": "Tablet"})
            self.assertEqual(code, 200)
            self.assertTrue(made["key"].startswith("bsk_") and "Tablet" in [k["label"] for k in made["keys"]])
            self.assertNotIn(made["key"], json.dumps(self.call("GET", "/api/remote")[1]))                           # the status never carries it
            code, again = self.call("POST", "/api/remote/keys/show", {"id": made["row"]["id"]})                      # but its code can be asked for again
            self.assertEqual((code, again["key"], again["row"]["label"]), (200, made["key"], "Tablet"))
            self.assertEqual(self.call("POST", "/api/remote/keys/show", {"id": made["row"]["id"]}, key=self.key, outside=True, away=True)[0], 403)
            self.assertEqual(self.call("POST", "/api/remote/keys/show", {"id": "nope"})[0], 404)
            self.assertEqual(self.call("POST", "/api/remote/keys/create", {"label": ""})[0], 400)
            self.assertEqual(self.call("POST", "/api/remote/keys/revoke", {"id": made["row"]["id"]})[0], 200)
            self.assertEqual(self.call("POST", "/api/remote/keys/revoke", {"id": "nope"})[0], 404)
            self.assertEqual(self.call("POST", "/api/remote/keys/create", {"label": "x"}, key=self.key, outside=True, away=True)[0], 403)
            self.assertEqual(self.call("GET", "/api/remote")[1]["listen"], "tailscale")
            self.assertEqual(self.call("POST", "/api/remote/listen", {"listen": "0.0.0.0"})[1]["listen"], "0.0.0.0")
            self.assertEqual(self.call("POST", "/api/remote/listen", {"listen": "nowhere"})[0], 400)
            self.assertEqual(self.call("POST", "/api/remote/listen", {"listen": "0.0.0.0"}, key=self.key, outside=True, away=True)[0], 403)
            self.assertEqual(self.call("POST", "/api/remote/listen", {"listen": "tailscale"})[1]["listen"], "tailscale")
        with mock.patch.object(remote, "tailscale_status", lambda: {"ip": ""}):
            code, status = self.call("POST", "/api/remote/enable", {})
            self.assertEqual((code, status["enabled"], status["listening"]), (200, True, False))
            self.assertIn("Tailscale is not connected", status["error"])
            self.assertEqual(self.call("POST", "/api/remote/disable", {})[1]["enabled"], False)


class PeersTest(unittest.TestCase):
    def test_the_other_devices_are_listed_with_the_ones_that_are_on_first(self):
        data = {"Peer": {"a": {"HostName": "tablet", "DNSName": "tablet.tail.ts.net.", "TailscaleIPs": ["100.64.0.5", "fd7a::5"], "OS": "android", "Online": False},
                         "b": {"HostName": "nas", "TailscaleIPs": ["100.64.0.7"], "OS": "linux", "Online": True},
                         "c": {"HostName": "noaddress", "TailscaleIPs": []}}}
        peers = remote.parse_peers(data)
        self.assertEqual([(p["name"], p["ip"], p["online"]) for p in peers], [("nas", "100.64.0.7", True), ("tablet", "100.64.0.5", False)])
        self.assertEqual(peers[1]["dns_name"], "tablet.tail.ts.net")
        self.assertEqual(remote.parse_peers({}), [])


class ListenerTest(unittest.TestCase):
    def test_the_listener_follows_the_setting_and_the_address_tailscale_has(self):
        started, closed = [], []

        class Fake:
            def __init__(self, host):
                self.server_address = (host, 1)

            def shutdown(self):
                closed.append(self.server_address[0])

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            access = RemoteAccess(Path(tmp) / "r.json", 7232, lambda ip, port: started.append((ip, port)) or Fake(ip))
            ip = {"v": "100.64.0.9"}
            with mock.patch.object(remote, "tailscale_status", lambda: {"ip": ip["v"]}):
                access.apply()
                self.assertFalse(access.listening)                          # off until the person turns it on
                access.set_enabled(True)
                self.assertEqual((started, access.listening, access.address()), ([("100.64.0.9", 7232)], True, "100.64.0.9:7232"))
                access.apply()
                self.assertEqual(len(started), 1)                           # the same address: nothing restarts
                ip["v"] = "100.64.0.10"
                access.apply()
                self.assertEqual((closed, started[-1][0]), (["100.64.0.9"], "100.64.0.10"))
                ip["v"] = ""
                access.apply()
                self.assertFalse(access.listening)
                self.assertIn("not connected", access.error)
                access.set_enabled(False)
            self.assertFalse(RemoteAccess(Path(tmp) / "r.json", 7232, lambda ip, port: None).enabled)         # the setting is kept
            ip["v"] = "100.64.0.9"

    def test_the_listener_binds_where_the_person_chose_and_that_is_kept(self):
        started = []

        class Fake:
            def __init__(self, host):
                self.server_address = (host, 1)

            def shutdown(self):
                pass

            def server_close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            access = RemoteAccess(Path(tmp) / "r.json", 7232, lambda ip, port: started.append(ip) or Fake(ip))
            self.assertEqual(access.listen, "tailscale")
            with mock.patch.object(remote, "tailscale_status", lambda: {"ip": ""}):          # Tailscale is not connected: nothing needs it
                access.set_enabled(True)
                self.assertEqual((access.listening, started), (False, []))
                access.set_listen("0.0.0.0")
                self.assertEqual((access.listening, started, access.error), (True, ["0.0.0.0"], ""))
                with mock.patch.object(remote, "lan_addresses", lambda: ["192.168.1.20"]):
                    self.assertEqual(access.address(), "192.168.1.20:7232")                  # a phone cannot connect to 0.0.0.0
                self.assertEqual(access.address("100.64.0.9"), "100.64.0.9:7232")
                access.set_listen(" 192.168.1.20 ")
                self.assertEqual((started[-1], access.address()), ("192.168.1.20", "192.168.1.20:7232"))
                with self.assertRaises(ValueError):
                    access.set_listen("not an address")
                self.assertEqual(access.listen, "192.168.1.20")
                saved = RemoteAccess(Path(tmp) / "r.json", 7232, lambda ip, port: None)
                self.assertEqual((saved.listen, saved.enabled), ("192.168.1.20", True))
                access.set_listen("tailscale")
                self.assertEqual(access.listening, False)                                    # back to Tailscale, which has no address now
                self.assertIn("Tailscale is not connected", access.error)


if __name__ == "__main__":
    unittest.main()
