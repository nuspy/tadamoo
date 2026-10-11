"""The settings page's endpoint (/api/settings) and the web UI's static files, against the mock engine."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve import settings  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CONFIG = {
    "server": {"model": "model.basalt", "alias": "Qwen 3.8 FN", "gpu": "0,1", "host": "127.0.0.1", "port": 7232},
    "engine": {"expert-cache": "auto", "prefill": "auto", "spec": 7, "spec-min-p": 0.5, "max-context": 262144,
               "kv": "int8", "resident-experts": True, "vram-reserve-mib": 700},
}


class Rules(unittest.TestCase):
    """Each field takes what the server and the engine take, and nothing else."""

    def test_accepted(self):
        ok = [("server", "port", 1, 1), ("server", "port", 65535, 65535), ("server", "parallel", 8, 8),
              ("server", "vision", "Dynamic", "dynamic"), ("engine", "max-context", 4096, 4096),
              ("engine", "kv", "q4", "q4_0"), ("engine", "kv", "k8v4", "k8v4"), ("engine", "spec", 0, 0),
              ("engine", "spec", 8, 8), ("engine", "spec-min-p", 0, 0), ("engine", "spec-min-p", 0.75, 0.75),
              ("engine", "expert-cache", "auto", "auto"), ("engine", "expert-cache", 700, 700),
              ("engine", "expert-cache", "700", 700), ("engine", "prefill", "auto:16384", "auto:16384"),
              ("engine", "prefill", 4096, 4096), ("engine", "max-context", 7.0, 7)]
        for section, key, value, want in ok:
            self.assertEqual(settings.check_value(section, key, value), want, (key, value))

    def test_refused(self):
        bad = [("server", "port", 0), ("server", "port", 70000), ("server", "port", "80a"), ("server", "port", True),
               ("server", "parallel", 0), ("server", "parallel", 9), ("server", "vision", True),
               ("server", "vision", "maybe"), ("engine", "max-context", 0), ("engine", "max-context", 2**31),
               ("engine", "max-context", 1.5), ("engine", "kv", "fp8"), ("engine", "kv", 8), ("engine", "spec", -1),
               ("engine", "spec", 9), ("engine", "spec-min-p", 1.5), ("engine", "spec-min-p", "0.5"),
               ("engine", "expert-cache", -1), ("engine", "expert-cache", "lots"), ("engine", "prefill", "auto:999"),
               ("engine", "prefill", -5), ("engine", "prefill", None), ("server", "alias", "x")]
        for section, key, value in bad:
            with self.assertRaises(ValueError, msg=(key, value)):
                settings.check_value(section, key, value)

    def test_loopback(self):
        for a in ("127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(settings.is_loopback(a), a)
        for a in ("192.168.1.20", "10.0.0.1", "::ffff:192.168.1.2", "fe80::1", "", "localhost"):
            self.assertFalse(settings.is_loopback(a), a)


class FileWrites(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "basalt_config.json")
        Path(self.path).write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def test_unknown_keys_and_order_are_kept(self):
        r = settings.save(self.path, {"engine.spec": 5, "server.port": 9000, "engine.kv": "k8v4"})
        self.assertEqual(sorted(r["changed"]), ["engine.kv", "engine.spec", "server.port"])
        new = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(list(new["engine"]), list(CONFIG["engine"]))          # same keys, same order
        self.assertEqual(new["engine"]["spec"], 5)
        self.assertEqual(new["engine"]["kv"], "k8v4")
        self.assertIs(new["engine"]["resident-experts"], True)
        self.assertEqual(new["engine"]["vram-reserve-mib"], 700)
        self.assertEqual(new["server"]["alias"], "Qwen 3.8 FN")
        self.assertEqual(new["server"]["port"], 9000)
        self.assertEqual(json.loads(Path(self.path + ".bak").read_text(encoding="utf-8")), CONFIG)

    def test_null_removes_and_new_keys_are_added(self):
        settings.save(self.path, {"engine.prefill": None, "server.parallel": 2, "server.vision": "off"})
        new = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertNotIn("prefill", new["engine"])
        self.assertEqual(new["server"]["parallel"], 2)
        self.assertEqual(new["server"]["vision"], "off")

    def test_the_file_s_own_spelling_is_kept(self):
        Path(self.path).write_text(json.dumps({"engine": {"max_context": 8192}}), encoding="utf-8")
        settings.save(self.path, {"engine.max-context": 16384})
        self.assertEqual(json.loads(Path(self.path).read_text(encoding="utf-8")), {"engine": {"max_context": 16384}})

    def test_a_bad_value_leaves_the_file_alone(self):
        before = Path(self.path).read_bytes()
        for changes in ({"engine.spec": 99}, {"server.model": "other.basalt"}, {"engine.resident-experts": False},
                        {}, None, {"server.api-key": "x"}):
            with self.assertRaises(ValueError, msg=changes):
                settings.save(self.path, changes)
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertEqual([p for p in os.listdir(self.dir.name) if p.startswith(".basalt-settings-")], [])

    def test_the_whole_file_is_checked_as_the_server_would(self):
        Path(self.path).write_text(json.dumps({"engine": {"kv-resident": 32768, "batch": 2}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "kv-resident"):
            settings.save(self.path, {"engine.kv": "k8v4"})
        with self.assertRaisesRegex(ValueError, "parallel and engine --batch"):
            settings.save(self.path, {"server.parallel": 4})

    def test_no_change_writes_nothing(self):
        r = settings.save(self.path, {"engine.spec": 7})
        self.assertEqual(r["changed"], [])
        self.assertFalse(os.path.exists(self.path + ".bak"))


class OverHttp(unittest.TestCase):
    def setUp(self):
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=4096), tok,
                           ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "basalt_config.json")
        Path(self.path).write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
        self.svc.settings_info = {"config": self.path, "model": "/models/m.basalt", "cli": ["engine.spec"],
                                  "running": {"server.port": 7232, "engine.spec": 3}}
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dir.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                c.request(method, path, body=data, headers=dict(headers or {}))
                r = c.getresponse()
                raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw, r.getheader("Content-Type")
        finally:
            c.close()

    def post(self, changes, **headers):
        return self.req("POST", "/api/settings", {"changes": changes}, {"Content-Type": "application/json", **headers})

    def test_read(self):
        code, body, _ = self.req("GET", "/api/settings")
        self.assertEqual(code, 200)
        self.assertTrue(body["editable"], body["reason"])
        self.assertEqual(body["values"]["engine.spec"], 7)
        self.assertEqual(body["values"]["server.model"], "model.basalt")
        self.assertEqual(body["running"]["engine.spec"], 3)
        self.assertEqual(body["from_command_line"], ["engine.spec"])
        self.assertIn("engine.kv", body["fields"])
        self.assertEqual(body["limits"]["kv"], ["fp16", "int8", "q4_0", "k8v4"])
        self.assertIn("started again", body["note"])

    def test_save(self):
        code, body, _ = self.post({"engine.spec": 6, "server.port": 7300}, Origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code, 200, body)
        self.assertEqual(sorted(body["changed"]), ["engine.spec", "server.port"])
        new = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual((new["engine"]["spec"], new["server"]["port"]), (6, 7300))
        self.assertEqual(new["engine"]["resident-experts"], True)

    def test_bad_values_are_400_and_change_nothing(self):
        before = Path(self.path).read_bytes()
        for changes in ({"engine.kv": "fp8"}, {"server.parallel": 12}, {"server.model": "x"}, {}):
            code, body, _ = self.post(changes)
            self.assertEqual(code, 400, changes)
            self.assertTrue(body["error"]["message"])
        self.assertEqual(self.req("POST", "/api/settings", b"{nope", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_another_site_s_page_or_a_form_cannot_save(self):
        self.assertEqual(self.post({"engine.spec": 2}, Origin="http://evil.example.com")[0], 403)
        self.assertEqual(self.req("POST", "/api/settings", {"changes": {"engine.spec": 2}},
                                  {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(json.loads(Path(self.path).read_text(encoding="utf-8"))["engine"]["spec"], 7)

    def test_only_from_this_pc(self):
        with mock.patch("serve.settings.is_loopback", return_value=False):
            code, body, _ = self.req("GET", "/api/settings")
            self.assertEqual(code, 200)
            self.assertFalse(body["editable"])
            self.assertIn("only from the PC", body["reason"])
            code, body, _ = self.post({"engine.spec": 2})
            self.assertEqual(code, 403)
        self.assertEqual(json.loads(Path(self.path).read_text(encoding="utf-8"))["engine"]["spec"], 7)

    def test_the_api_key_guards_it(self):
        self.svc.api_key = "s3cret"
        self.assertEqual(self.req("GET", "/api/settings")[0], 401)
        self.assertEqual(self.post({"engine.spec": 2})[0], 401)
        self.assertEqual(self.req("GET", "/api/settings", headers={"Authorization": "Bearer s3cret"})[0], 200)
        self.assertEqual(self.post({"engine.spec": 2}, Authorization="Bearer s3cret")[0], 200)

    def test_without_a_config_file(self):
        self.svc.settings_info = {"config": None, "running": {}}
        code, body, _ = self.req("GET", "/api/settings")
        self.assertEqual(code, 200)
        self.assertFalse(body["editable"])
        self.assertIn("--config", body["reason"])
        self.assertEqual(self.post({"engine.spec": 2})[0], 409)
        self.svc.settings_info = None
        self.assertEqual(self.req("GET", "/api/settings")[0], 200)

    def test_oversized_body(self):
        big = {"changes": {"engine.kv": "int8"}, "pad": "x" * (settings.BODY_MAX + 10)}
        self.assertEqual(self.req("POST", "/api/settings", big, {"Content-Type": "application/json"})[0], 413)

if __name__ == "__main__":
    unittest.main()
