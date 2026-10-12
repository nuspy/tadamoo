"""The language of the messages (serve/lang.py, the page, the scripts) and `Avvia-Basalt.ps1 -server` with a server
already running: the same loading parameters attach, different ones ask whether to reuse or restart it (-riusa,
-riavvia, no answer = reuse)."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from serve import lang, loader, profiles
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.tests.test_models import tiny_basalt
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
PORT = 7283                     # a free port of this repository's test range


class Isolated(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_DATA": self.dir.name, "BASALT_CACHE": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()


class Language(Isolated):
    def test_the_order(self):
        with mock.patch.dict(os.environ, {"BASALT_LANG": ""}), mock.patch.object(lang, "system_lang", return_value="it"):
            self.assertEqual(lang.lang(), "en")                      # English, whatever the system's
            lang.set_lang("it")
            self.assertEqual(lang.lang(), "it")                      # the user's choice wins over the system
            self.assertEqual(lang.t("sì", "yes"), "sì")
            with mock.patch.dict(os.environ, {"BASALT_LANG": "en"}):
                self.assertEqual(lang.lang(), "en")                  # BASALT_LANG (or --lang) wins over the choice
            lang.set_lang("auto")
            self.assertEqual(lang.lang(), "en")
            self.assertIsNone(lang.info()["chosen"])
        with self.assertRaises(ValueError):
            lang.set_lang("fr")

    def test_the_page_and_the_prefs(self):
        tok = ByteTokenizer()
        svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        httpd = serve(svc, port=0)
        port = httpd.server_address[1]

        def req(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body=None if body is None else json.dumps(body),
                      headers={"Content-Type": "application/json"} if body is not None else {})
            r = c.getresponse()
            data = r.read()
            c.close()
            return r.status, data
        try:
            with mock.patch.dict(os.environ, {"BASALT_LANG": ""}), mock.patch.object(lang, "system_lang", return_value="en"):
                self.assertIn(b'data-server-lang="en"', req("GET", "/")[1])
                code, body = req("POST", "/api/prefs", {"lang": "it"})
                self.assertEqual(code, 200, body)
                self.assertIn(b'<html lang="it" data-server-lang="it">', req("GET", "/")[1])
                self.assertEqual(json.loads(req("GET", "/api/prefs")[1])["chosen"], "it")
                self.assertEqual(req("POST", "/api/prefs", {"lang": "de"})[0], 400)
                with mock.patch("serve.settings.is_loopback", return_value=False):
                    self.assertEqual(req("POST", "/api/prefs", {"lang": "en"})[0], 403)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_the_command_line_follows_it(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"BASALT_LANG": "en", "BASALT_FAKE_SYSTEM_OK": "1",
                                          "BASALT_FAKE_DISPLAYS": "[]", "BASALT_FAKE_PAGEFILE": '{"files": []}'}), \
                contextlib.redirect_stdout(out):
            loader.main(["wizard", "--monitor", "none"])
        self.assertIn("prediction:", out.getvalue())
        self.assertNotIn("previsione", out.getvalue())


class Compare(Isolated):
    RUNNING = {"server.port": 7232, "server.parallel": 1, "server.vision": "dynamic", "server.slot-full-context": False,
               "engine.max-context": 65536, "engine.kv": "q4_0", "engine.spec": 7, "engine.spec-min-p": 0.5,
               "engine.expert-cache": "auto", "engine.prefill": "auto", "engine.resident-experts": True,
               "engine.vram-reserve-mib": 1536, "engine.ple-io": "direct", "engine.pool-workers": 15}

    def requested(self, **changes):
        v = {k: v for k, v in self.RUNNING.items() if k != "server.port"}
        v.update(changes)
        return v

    def test_the_same(self):
        r = profiles.compare(self.RUNNING, "D:/m/a.basalt", self.requested(), "D:/m/a.basalt", 7232, None)
        self.assertTrue(r["same"], r)
        self.assertTrue(profiles.compare(self.RUNNING, "D:/m/a.basalt", self.requested(), None)["same"])   # none chosen

    def test_a_different_model(self):
        r = profiles.compare(self.RUNNING, "D:/m/a.basalt", self.requested(), "D:/m/b.basalt")
        self.assertEqual([d["field"] for d in r["differences"]], ["server.model"])

    def test_a_different_field_and_port(self):
        r = profiles.compare(self.RUNNING, None, self.requested(**{"engine.max-context": 131072, "engine.spec-min-p": 0.3}),
                             None, 7232, 7240)
        self.assertEqual(sorted(d["field"] for d in r["differences"]),
                         ["engine.max-context", "engine.spec-min-p", "server.port"])
        self.assertTrue(profiles.compare(self.RUNNING, None, self.requested(**{"engine.kv": "Q4_0"}), None)["same"])
        absent = profiles.compare({}, None, {"engine.kv": "fp16"}, None)                 # the engine's defaults
        self.assertTrue(absent["same"], absent)

    def test_over_http_with_the_default_profile(self):
        tok = ByteTokenizer()
        svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        cfg = Path(self.dir.name) / "basalt_config.json"
        cfg.write_text(json.dumps({"server": {"port": 7232}}), encoding="utf-8")
        svc.settings_info = {"config": str(cfg), "model": None, "cli": [], "running": dict(self.RUNNING)}
        httpd = serve(svc, port=0)
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        try:
            profiles.change({"action": "save", "kind": "load", "name": "Uguale", "values": self.requested()})
            profiles.change({"action": "save", "kind": "load", "name": "Lungo",
                             "values": self.requested(**{"engine.max-context": 262144})})
            self.assertTrue(profiles.compare_with_server(url, "Uguale", None, str(cfg))["same"])
            profiles.change({"action": "default", "kind": "load", "name": "Lungo"})
            r = profiles.compare_with_server(url, None, None, str(cfg))                    # no --config: the default
            self.assertEqual((r["profile"], [d["field"] for d in r["differences"]]), ("Lungo", ["engine.max-context"]))
        finally:
            httpd.shutdown()
            httpd.server_close()


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell scripts")
class RunningServer(Isolated):
    """Avvia-Basalt.ps1 -server against a mock server of this repository on a test port."""

    def setUp(self):
        super().setUp()
        root = Path(self.dir.name)
        self.cfg = root / "basalt_config.json"
        self.cfg.write_text(json.dumps({"server": {"port": PORT}}), encoding="utf-8")
        self.model = tiny_basalt(root / "a.basalt", "A")
        profiles.change({"action": "save", "kind": "load", "name": "Uguale", "values": {}})
        profiles.change({"action": "save", "kind": "load", "name": "Corto", "values": {"engine.max-context": 8192}})
        self.envs = {**os.environ, "BASALT_DATA": self.dir.name, "BASALT_CACHE": self.dir.name, "PYTHONUTF8": "1",
                     "BASALT_NO_DIALOG": "1", "BASALT_LANG": "it"}
        self.server = subprocess.Popen([sys.executable, "-m", "serve.server", "--mock-engine", "--config", str(self.cfg),
                                        "--port", str(PORT)], cwd=ROOT, env=self.envs, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        for _ in range(150):
            try:
                c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
                c.request("GET", "/health")
                c.getresponse().read()
                c.close()
                break
            except OSError:
                time.sleep(0.1)

    def tearDown(self):
        subprocess.run(["taskkill", "/PID", str(self.server.pid), "/T", "/F"], capture_output=True)
        self.server.wait(timeout=20)
        super().tearDown()

    def avvia(self, *args, lang_code="it"):
        p = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(ROOT / "Avvia-Basalt.ps1"), "-server", "--file", str(self.cfg), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                           stdin=subprocess.DEVNULL, env={**self.envs, "BASALT_LANG": lang_code})
        return p.returncode, p.stdout + p.stderr

    def alive(self) -> bool:
        return self.server.poll() is None

    def test_the_same_parameters_attach(self):
        code, out = self.avvia("--config", "Uguale")
        self.assertEqual(code, 0, out)
        self.assertIn("ha già i parametri di caricamento chiesti", out)
        self.assertIn("mi collego a quello", out)

    def test_different_ones_and_the_answers(self):
        code, out = self.avvia("--config", "Corto", "-riusa")
        self.assertEqual(code, 0, out)
        self.assertIn("engine.max-context", out)
        self.assertIn("riuso il server avviato (-riusa)", out)
        code, out = self.avvia("--config", "Corto", "--model", str(self.model), "-riavvia", "-prova")
        self.assertEqual(code, 0, out)
        self.assertIn("server.model", out)
        self.assertIn(f"PROVA: fermerei solo il server sulla porta {PORT}", out)
        code, out = self.avvia("--config", "Corto")                         # nobody to ask (no desktop, no console)
        self.assertEqual(code, 0, out)
        self.assertIn("nessuna risposta", out)
        self.assertIn("riuso il server avviato", out)
        self.assertTrue(self.alive())                                        # never stopped without an answer

    def test_the_default_profile_and_english(self):
        profiles.change({"action": "default", "kind": "load", "name": "Corto"})
        code, out = self.avvia("-riusa", lang_code="en")
        self.assertEqual(code, 0, out)
        self.assertIn("profile asked for: Corto", out)
        self.assertIn("reuse the running server (-riusa)", out)

    def test_the_timeout(self):
        with mock.patch.dict(self.envs, {"BASALT_ASK_TIMEOUT_S": "1"}):
            t0 = time.time()
            code, out = self.avvia("--config", "Corto")
        self.assertEqual(code, 0, out)
        self.assertIn("nessuna risposta (entro 1 s", out)
        self.assertLess(time.time() - t0, 120)
        self.assertTrue(self.alive())


if __name__ == "__main__":
    unittest.main()
