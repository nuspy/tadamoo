"""The server without a loaded model (--standby: the desktop app's start screen is the Loader) and the restart with
another model (--relaunch-on-model-change: exit code 75 for the program that started the server)."""
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

from serve import loader, supervisor
from serve.config import RELAUNCH_EXIT_CODE
from serve.engine.mock import MockEngine, NoEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.tests.test_models import ROOMY, tiny_basalt
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
PORT = 7281                     # a free port of this repository's test range
FITS = {"engine.expert-cache": 8}        # the tiny model has 8 expert slots: a fixed cache fits


def service(engine=None):
    tok = ByteTokenizer()
    return Service(engine or MockEngine(tok, ["</think>\n\nhello"]), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))


class InProcess(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": str(root), "BASALT_DATA": str(root)})
        self.env.start()
        self.a = tiny_basalt(root / "a.basalt", "Model A")
        self.b = tiny_basalt(root / "b.basalt", "Model B")
        self.cfg = root / "basalt_config.json"
        self.cfg.write_text(json.dumps({"server": {"model": str(self.a)}, "engine": {"max-context": 4096}}),
                            encoding="utf-8")
        self.svc = service()
        self.svc.standby = True
        self.svc.settings_info = {"config": str(self.cfg), "model": str(self.a), "cli": [], "running": {}}
        self.sup = self.svc.supervisor = supervisor.Supervisor(self.svc, ["--mock-engine", "--config", str(self.cfg)],
                                                               mock=True)
        self.sup.mock_delay = 0.1
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

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                c.request(method, path, body=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"} if body is not None else {})
                r = c.getresponse()
                raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw
        finally:
            c.close()

    def wait(self, cond, what):
        for _ in range(100):
            if cond():
                return
            time.sleep(0.05)
        self.fail(what)

    def test_nothing_runs_until_the_loader_loads(self):
        code, h = self.req("GET", "/health")
        self.assertEqual((code, h["loaded"], h["standby"]), (200, False, True))
        code, body = self.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 503, body)
        self.assertIn("Loader", body["error"]["message"])
        self.assertEqual(self.req("POST", "/v1/load", {})[0], 503)
        info = self.req("GET", "/api/loader")[1]
        self.assertTrue(info["standby"])
        self.assertEqual(self.req("POST", "/api/loader/load", {"values": FITS, "model": str(self.a)})[0], 202)
        self.wait(lambda: self.sup.status()["phase"] == "ready", "the load did not finish")
        self.assertFalse(self.svc.standby)
        code, body = self.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 200, body)
        self.assertFalse(self.req("GET", "/health")[1]["standby"])

    def test_another_model_restarts_the_server_when_its_owner_can(self):
        self.svc.relaunch_on_model_change = True
        code, body = self.req("POST", "/api/loader/load", {"values": {"engine.kv": "q4_0", **FITS}, "model": str(self.b)})
        self.assertEqual(code, 202, body)
        self.assertEqual((body["status"], body["notes"]), ("restarting", ["model_restart"]))
        self.wait(self.svc.exit_request.is_set, "the server was not asked to end")
        saved = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(Path(saved["server"]["model"]), self.b)        # the new start reads it from the file
        self.assertEqual(saved["engine"]["kv"], "q4_0")

    def test_without_an_owner_the_model_waits_for_the_next_start(self):
        code, body = self.req("POST", "/api/loader/load", {"values": FITS, "model": str(self.b)})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["notes"], ["model_on_restart"])
        self.assertFalse(self.svc.exit_request.is_set())


class NoModel(unittest.TestCase):
    def test_the_stand_in(self):
        svc = service(NoEngine())
        svc.standby = True
        self.assertFalse(svc.loaded())
        self.assertEqual(svc.engine.max_context, 0)

    def test_a_server_without_a_model_answers(self):
        """A real server process: --standby with only the engine binary, as the desktop app starts it on a new PC."""
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "BASALT_DATA": d, "BASALT_CACHE": d, "PYTHONUTF8": "1"}
            p = subprocess.Popen([sys.executable, "-m", "serve.server", "--standby", "--engine-bin",
                                  str(Path(d) / "basalt.exe"), "--port", str(PORT)], cwd=ROOT, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                health = None
                for _ in range(100):
                    try:
                        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
                        c.request("GET", "/health")
                        health = json.loads(c.getresponse().read())
                        c.close()
                        break
                    except OSError:
                        if p.poll() is not None:
                            break
                        time.sleep(0.1)
                self.assertIsNotNone(health, p.stdout.read() if p.poll() is not None else "no answer")
                self.assertEqual((health["loaded"], health["standby"]), (False, True))
                c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
                c.request("GET", "/api/loader")
                info = json.loads(c.getresponse().read())
                c.close()
                self.assertTrue(info["standby"])
            finally:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                p.stdout.close()

    def test_the_exit_code(self):
        self.assertEqual(RELAUNCH_EXIT_CODE, 75)


if __name__ == "__main__":
    unittest.main()
