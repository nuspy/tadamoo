"""Saved load profiles and inference presets (serve/profiles.py, /api/profiles)."""
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
from serve import loader, profiles  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GiB = 1 << 30
ROOMY = {"ram_total": 128 * GiB, "ram_available": 120 * GiB, "commit_limit": 144 * GiB, "commit_used": 10 * GiB,
         "vram_total": 32 * GiB, "vram_free": 31 * GiB}
FAST = {"engine.max-context": 65536, "engine.kv": "int8", "engine.spec": 7, "server.port": 7300}


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": self.dir.name, "BASALT_DATA": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_builtin_safe_profile_is_the_first_default_and_protected(self):
        lst = profiles.listing(ROOMY)
        self.assertEqual(lst["load"][0]["name"], profiles.SAFE_NAME)
        self.assertTrue(lst["load"][0]["builtin"] and lst["load"][0]["default"])
        self.assertEqual(lst["load"][0]["summary"]["text"], "256K · q4_0 · spec 7")
        self.assertIn("vram_gb", lst["load"][0]["summary"])
        for req in ({"action": "delete", "name": profiles.SAFE_NAME},
                    {"action": "rename", "name": profiles.SAFE_NAME, "new_name": "x"},
                    {"action": "save", "name": profiles.SAFE_NAME, "values": {}}):
            with self.assertRaises(ValueError):
                profiles.change(req)

    def test_save_rename_default_delete(self):
        profiles.change({"action": "save", "kind": "load", "name": "Veloce", "values": FAST})
        self.assertEqual(profiles.get_load("Veloce"), FAST)
        profiles.change({"action": "default", "name": "Veloce"})
        self.assertEqual(profiles.default_name(), "Veloce")
        profiles.change({"action": "rename", "name": "Veloce", "new_name": "Rapido"})
        self.assertEqual(profiles.default_name(), "Rapido")               # the default follows the rename
        self.assertIsNone(profiles.get_load("Veloce"))
        profiles.change({"action": "delete", "name": "Rapido"})
        self.assertEqual(profiles.default_name(), profiles.SAFE_NAME)     # back to the built-in
        self.assertTrue(profiles.path().is_file())

    def test_bad_values_are_refused(self):
        for values in ({"engine.kv": "fp8"}, {"server.model": "x"}, {"server.port": 0}, "nope"):
            with self.assertRaises(ValueError, msg=values):
                profiles.change({"action": "save", "name": "x", "values": values})
        with self.assertRaises(ValueError):
            profiles.change({"action": "save", "name": " ", "values": {}})
        with self.assertRaises(ValueError):
            profiles.change({"action": "explode"})

    def test_inference_presets_and_summary(self):
        s = {"temperature": "0.7", "effort": "medium", "max_tokens": "4096", "policy": "compact", "format": "json_schema",
             "schema": '{"type":"object"}', "unknown": 1}
        profiles.change({"action": "save", "kind": "inference", "name": "Preciso", "settings": s})
        p = profiles.listing()["inference"][0]
        self.assertNotIn("unknown", p["settings"])
        self.assertEqual(p["summary"], "temp 0.7 · reasoning medium · max 4K · compact · JSON schema")

    def test_export_import(self):
        profiles.change({"action": "save", "name": "Veloce", "values": FAST})
        profiles.change({"action": "save", "kind": "inference", "name": "P", "settings": {"temperature": "1"}})
        data = profiles.export()
        profiles.change({"action": "delete", "name": "Veloce"})
        profiles.change({"action": "delete", "kind": "inference", "name": "P"})
        profiles.change({"action": "import", "data": json.loads(json.dumps(data))})
        self.assertEqual(profiles.get_load("Veloce"), FAST)
        self.assertEqual([p["name"] for p in profiles.listing()["inference"]], ["P"])

    def test_apply_writes_the_config_and_keeps_a_backup(self):
        cfg = os.path.join(self.dir.name, "basalt_config.json")
        original = {"server": {"model": "m.basalt", "port": 7232}, "engine": {"kv": "fp16", "resident-experts": True}}
        Path(cfg).write_text(json.dumps(original), encoding="utf-8")
        profiles.change({"action": "save", "name": "Veloce", "values": FAST})
        profiles.apply("Veloce", cfg)
        new = json.loads(Path(cfg).read_text(encoding="utf-8"))
        self.assertEqual(new["server"]["port"], 7300)
        self.assertEqual(new["server"]["model"], "m.basalt")
        self.assertEqual((new["engine"]["kv"], new["engine"]["max-context"]), ("int8", 65536))
        self.assertNotIn("resident-experts", new["engine"])              # the profile's own value: the default
        self.assertEqual(json.loads(Path(cfg + ".bak").read_text(encoding="utf-8")), original)
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(loader, "system_now", return_value=dict(ROOMY)):   # Valori sicuri follow the PC
            self.assertEqual(profiles.main(["apply", "Nessuno", "--config", cfg]), 1)
            self.assertIn("Valori sicuri", err.getvalue())
            self.assertEqual(profiles.main(["apply", profiles.SAFE_NAME, "--config", cfg]), 0)
        self.assertEqual(json.loads(out.getvalue())["values"]["engine.kv"], "q4_0")
        self.assertEqual(json.loads(Path(cfg).read_text(encoding="utf-8"))["engine"]["max-context"], 262144)


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": self.dir.name, "BASALT_DATA": self.dir.name})
        self.env.start()
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.system = mock.patch.object(loader, "system_now", return_value=dict(ROOMY))
        self.system.start()
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.system.stop()
        self.env.stop()
        self.dir.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers or {})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def test_list_change_export(self):
        code, body = self.req("GET", "/api/profiles")
        self.assertEqual((code, body["default"]), (200, profiles.SAFE_NAME))
        json_ = {"Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/api/profiles", {"action": "save", "name": "A", "values": FAST}, json_)[0], 200)
        self.assertEqual(self.req("POST", "/api/profiles", {"action": "delete", "name": profiles.SAFE_NAME}, json_)[0], 400)
        self.assertEqual(self.req("POST", "/api/profiles", {"action": "save", "name": "B", "values": {}},
                                  {**json_, "Origin": "http://evil.example.com"})[0], 403)
        with mock.patch("serve.settings.is_loopback", return_value=False):
            self.assertEqual(self.req("POST", "/api/profiles", {"action": "save", "name": "B", "values": {}}, json_)[0], 403)
        code, data = self.req("GET", "/api/profiles/export?kind=load")
        self.assertEqual(code, 200)
        self.assertIn("A", data["load"])
        self.assertNotIn("inference", data)


if __name__ == "__main__":
    unittest.main()
