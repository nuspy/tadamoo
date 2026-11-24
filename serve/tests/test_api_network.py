"""Settings > Network over the real server (scripted mock engine): the values the server runs with and the ones in the config file, writing them back checked, what waits for a restart,
and the test that says whether an address answers and whether the Host rule lets its name through, also behind a reverse proxy (a small proxy in the test)."""
import http.server
import json
import threading
import unittest
import urllib.request
from pathlib import Path

from serve import handler
from serve.tests.test_api_plans import PlansApiBase
from serve.tests import cfgv2  # noqa: E402


class Proxy(http.server.ThreadingHTTPServer):
    """A reverse proxy beside the server: it forwards to `target` with the public name as Host and the scheme it was reached with."""

    def __init__(self, target, public):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.target, self.public = target, public

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            req = urllib.request.Request(self.server.target + self.path, headers={"Host": self.server.public, "X-Forwarded-Proto": "https", "X-Forwarded-Host": self.server.public})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    status, body = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class NetworkApiTest(PlansApiBase):
    def setUp(self):
        self.config = Path(self.tmp.name) / "tadamoo_config.json"
        self.config.write_text(cfgv2.dumps({"server": {"port": self.port, "parallel": 2}, "engine": {"max-context": 4096}}), encoding="utf-8")
        self.old = getattr(self.svc, "settings_info", None)
        self.svc.settings_info = {"config": str(self.config), "model": None, "running": {}, "cli": []}
        self.svc.allowed_hosts, self.svc.public_name = (), ""

    def tearDown(self):
        self.svc.settings_info = self.old
        self.svc.allowed_hosts, self.svc.public_name = (), ""

    def test_the_view_says_what_runs_what_the_file_says_and_what_waits_for_a_restart(self):
        code, v = self.call("GET", "/api/network")
        self.assertEqual(code, 200)
        self.assertEqual((v["running"]["host"], v["running"]["port"], v["running"]["public_name"], v["editable"], v["pending"]), ("127.0.0.1", self.port, "", True, []))
        code, res = self.call("POST", "/api/network", {"host": "0.0.0.0", "port": self.port + 1, "allowed_hosts": ["Mypc.example.ts.net"], "public_name": "tadamoo.example.org"})
        self.assertEqual(code, 200)
        self.assertEqual(sorted(res["changed"]), ["server.allowed-hosts", "server.host", "server.port", "server.public-name"])
        self.assertEqual(sorted(res["pending"]), ["allowed_hosts", "host", "port", "public_name"])
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["server"]["host"], "0.0.0.0")
        self.assertEqual(saved["server"]["allowed-hosts"], ["mypc.example.ts.net"])
        self.assertEqual((saved["engine"]["parallel"], saved["engine"]["max-context"]), (2, 4096))           # every other key is kept
        self.assertTrue(Path(str(self.config) + ".bak").exists())
        code, res = self.call("POST", "/api/network", {"allowed_hosts": [], "public_name": ""})              # taken out of the file
        self.assertEqual(sorted(res["changed"]), ["server.allowed-hosts", "server.public-name"])
        self.assertNotIn("public-name", json.loads(self.config.read_text(encoding="utf-8"))["server"])

    def test_a_wrong_value_is_refused_and_the_file_is_left_as_it_was(self):
        before = self.config.read_text(encoding="utf-8")
        for bad in ({"host": "not a host"}, {"host": "*"}, {"port": 70000}, {"port": "x"}, {"allowed_hosts": ["http://x.example.org"]}, {"public_name": "10.0.0.1"}, {"public_name": "*"}, {}):
            code, res = self.call("POST", "/api/network", bad)
            self.assertEqual(code, 400, bad)
            self.assertTrue(res["error"]["message"], bad)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_without_a_config_file_it_says_so_and_changes_nothing(self):
        self.svc.settings_info = {"config": None}
        code, v = self.call("GET", "/api/network")
        self.assertEqual((code, v["editable"], v["reason"]), (200, False, "no_config"))
        self.assertEqual(self.call("POST", "/api/network", {"port": 7000})[0], 400)

    def test_the_test_says_whether_the_listen_address_answers(self):
        code, res = self.call("POST", "/api/network/test", {"host": "0.0.0.0", "port": self.port})
        self.assertEqual(code, 200)
        listen = res["checks"][0]
        self.assertEqual((listen["id"], listen["answers"], listen["running"]), ("listen", True, True))
        _, res = self.call("POST", "/api/network/test", {"host": "127.0.0.1", "port": self.port + 40})
        listen = res["checks"][0]
        self.assertEqual((listen["answers"], listen["running"]), (False, False))                     # nothing listens there: it will after the restart
        self.assertTrue(listen["reason"])

    def test_the_test_reaches_an_address_and_reads_the_host_rule_also_behind_a_proxy(self):
        _, res = self.call("POST", "/api/network/test", {"name": "127.0.0.1", "scheme": "http", "port": self.port})
        rule, reach = res["checks"]
        self.assertTrue(rule["ok"] and reach["ok"])                                                  # an IP address is always allowed
        self.assertEqual(reach["seen"]["host"], f"127.0.0.1:{self.port}")
        proxy = Proxy(f"http://127.0.0.1:{self.port}", "tadamoo.example.org")
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.shutdown)
        # the proxy forwards the public name as the Host: the rule refuses it
        _, res = self.call("POST", "/api/network/test", {"name": "127.0.0.1", "scheme": "http", "port": proxy.server_address[1]})
        reach = res["checks"][0] if res["checks"][0]["id"] == "reach" else res["checks"][1]
        self.assertEqual((reach["ok"], reach["status"], reach["error"]), (False, 403, "host_not_allowed"))
        _, res = self.call("POST", "/api/network/test", {"name": "tadamoo.example.org"})
        self.assertEqual((res["checks"][0]["ok"], res["checks"][0]["after_restart"]), (False, False))           # not allowed now, nor after a restart
        self.call("POST", "/api/network", {"public_name": "tadamoo.example.org"})
        _, res = self.call("POST", "/api/network/test", {"name": "tadamoo.example.org"})
        self.assertEqual((res["checks"][0]["ok"], res["checks"][0]["after_restart"]), (False, True))            # saved: it passes after the restart
        self.svc.allowed_hosts = ("tadamoo.example.org",)                                                        # the server is started again with it
        _, res = self.call("POST", "/api/network/test", {"name": "127.0.0.1", "scheme": "http", "port": proxy.server_address[1]})
        reach = res["checks"][-1]
        self.assertEqual((reach["ok"], reach["seen"]["host"], reach["seen"]["proto"], reach["seen"]["forwarded_host"]), (True, "tadamoo.example.org", "https", "tadamoo.example.org"))

    def test_the_test_names_an_address_that_does_not_answer_and_refuses_a_bad_one(self):
        _, res = self.call("POST", "/api/network/test", {"name": "127.0.0.1", "scheme": "http", "port": 1})
        reach = res["checks"][-1]
        self.assertFalse(reach["ok"])
        self.assertTrue(reach["error"])
        self.assertEqual(self.call("POST", "/api/network/test", {"name": "http://x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/network/test", {"name": "x.example.org", "scheme": "ftp"})[0], 400)

    def test_the_ping_needs_no_key_and_says_what_arrived(self):
        code, body = self.call("GET", "/api/network/ping", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "tadamoo.example.org"})
        self.assertEqual(code, 200)
        self.assertEqual((body["tadamoo"], body["proto"], body["forwarded_host"]), (True, "https", "tadamoo.example.org"))


if __name__ == "__main__":
    unittest.main()
