"""GET /api/bench/overview and POST /api/bench/availability | run | cancel, over HTTP, and the conversion options of /api/models/plan."""
from __future__ import annotations

import http.client
import json
import time
import unittest
from unittest import mock

from serve import ROOT, benchrun, loader, modelbench, models
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.tests.test_benchrun import fake_runner
from serve.tests.test_models import Base, ROOMY, tiny_basalt, tiny_qwen
from serve.util.text import ByteTokenizer

JS = {"Content-Type": "application/json"}
REAL_PLAN = loader.plan


class Bench(Base):
    def setUp(self):
        super().setUp()
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.settings_info = {"config": None, "model": None, "cli": [], "running": {}}
        self.patches = [mock.patch.object(loader, "system_now", return_value=dict(ROOMY)), mock.patch.object(benchrun, "power_limit_w", lambda: 600.0),
                        mock.patch.object(benchrun, "run_tests", fake_runner([])), mock.patch.object(models, "_commit_share", return_value=0.5),
                        mock.patch.object(benchrun, "JOBS", benchrun.Jobs()),
                        mock.patch.object(loader, "plan", return_value={"fits": True, "problems": []})]       # a tiny model has too few expert slots for the real check
        for p in self.patches:
            p.start()
        self.model = tiny_basalt(self.models / "m.basalt")
        tiny_qwen(self.models)
        models.set_dirs([str(self.models)])
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self.patches:
            p.stop()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers if headers is not None else JS)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def wait(self):
        deadline = time.time() + 10
        while benchrun.JOBS.running() and time.time() < deadline:
            time.sleep(0.02)
        return benchrun.JOBS.status()

    def test_the_overview_lists_what_to_compare_and_what_to_offer(self):
        code, body = self.req("GET", "/api/bench/overview")
        self.assertEqual(code, 200)
        local = [m for m in body["models"] if m["kind"] == "local"]
        self.assertEqual([m["path"] for m in local], [self.model.as_posix()])
        self.assertGreaterEqual(len([m for m in body["models"] if m["kind"] == "published"]), 8)
        self.assertEqual(len(body["convertible"]), 1)
        self.assertTrue({p["id"] for p in body["tuned_packs"]} >= {"basalt-iq3_xxs"})
        self.assertEqual((body["standard"]["kv"], body["standard"]["spec"], body["power_w"]), ("int8", 7, 600.0))
        self.assertIn("decode", {m["id"] for m in body["metrics"]})
        self.assertEqual(body["job"]["phase"], "idle")

    def test_a_test_runs_asks_before_repeating_and_can_reuse(self):
        body = {"path": str(self.model), "steps": ["speed", "quality"], "n": 50}
        self.assertEqual(self.req("POST", "/api/bench/availability", body)[1]["available"], {"speed": None, "quality": None})
        code, st = self.req("POST", "/api/bench/run", body)
        self.assertEqual((code, st["phase"]), (202, "running"))
        self.assertEqual(self.wait()["stored"], ["speed", "quality"])
        found = self.req("POST", "/api/bench/availability", body)[1]["available"]
        self.assertTrue(found["speed"] and found["quality"])
        code, err = self.req("POST", "/api/bench/run", body)
        self.assertEqual((code, err["error"]["code"], sorted(err["available"])), (409, "reuse", ["quality", "speed"]))
        code, st = self.req("POST", "/api/bench/run", {**body, "reuse": "reuse"})
        self.assertEqual((code, st["phase"], sorted(st["reused"])), (202, "done", ["quality", "speed"]))
        code, st = self.req("POST", "/api/bench/run", {**body, "reuse": {"speed": "redo", "quality": "reuse"}})
        self.assertEqual((code, st["phase"]), (202, "running"))
        self.assertEqual(self.wait()["stored"], ["speed"])
        figures = next(m for m in self.req("GET", "/api/bench/overview")[1]["models"] if m["kind"] == "local")["figures"]
        self.assertEqual(figures["decode"], 165.0)

    def test_a_loaded_model_or_a_model_that_does_not_fit_stops_the_test_before_it_starts(self):
        body = {"path": str(self.model), "steps": ["quality"]}
        with mock.patch.object(loader, "engine_holds", return_value={"ram": 40 << 30, "commit": 70 << 30}):
            code, err = self.req("POST", "/api/bench/run", body)
        self.assertEqual((code, err["error"]["code"]), (409, "model_loaded"))
        plan = REAL_PLAN(loader.effective({}), loader.DEMO_FACTS, {**ROOMY, "ram_available": 1 << 30})
        self.assertFalse(plan["fits"])
        with mock.patch.object(loader, "plan", return_value=plan):
            code, err = self.req("POST", "/api/bench/run", body)
        self.assertEqual((code, err["error"]["code"]), (422, "does_not_fit"))
        self.assertTrue(err["problems"])
        self.assertEqual(self.req("GET", "/api/bench/job")[1]["phase"], "idle")

    def test_the_routes_are_for_this_pc(self):
        body = {"path": str(self.model)}
        with mock.patch("serve.settings.is_loopback", return_value=False):
            self.assertEqual(self.req("POST", "/api/bench/run", body)[0], 403)
        self.assertEqual(self.req("POST", "/api/bench/explode", {})[0], 404)
        self.assertEqual(self.req("POST", "/api/bench/run", {"path": str(self.models / "missing.basalt")})[0], 404)
        self.assertEqual(self.req("POST", "/api/bench/cancel", {})[1]["cancelled"], False)

    def test_unloading_and_loading_again_go_through_the_service(self):
        with mock.patch.object(self.svc, "unload", return_value="unloaded") as unload:
            self.assertEqual(self.req("POST", "/api/bench/unload", {}), (200, {"status": "unloaded"}))
            unload.assert_called_once()
        with mock.patch.object(self.svc, "unload", return_value="busy"):
            self.assertEqual(self.req("POST", "/api/bench/unload", {})[0], 409)
        with mock.patch.object(self.svc, "load") as load:
            self.assertEqual(self.req("POST", "/api/bench/reload", {}), (200, {"status": "loaded"}))
            load.assert_called_once()

    def test_the_conversion_plan_takes_the_options(self):
        conv = next(m for m in self.req("GET", "/api/models")[1]["models"] if m["status"] == "convert")
        code, plan = self.req("POST", "/api/models/plan", {"id": conv["id"], "out_dir": str(self.models), "options": {"vision": "none", "name": "Mine"}})
        self.assertEqual((code, plan["ok"], plan["options"]["vision"]["mode"]), (200, True, "none"))
        self.assertIn("--no-vision", plan["args"])
        code, err = self.req("POST", "/api/models/plan", {"id": conv["id"], "out_dir": str(self.models), "options": {"mtp": "sometimes"}})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
