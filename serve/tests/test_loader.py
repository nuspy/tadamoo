"""The Loader: memory plan, speed prediction, calibration, and /api/loader/* with the mock engine."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve import loader, loader_api, supervisor  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GiB = 1 << 30
ROOMY = {"ram_total": 128 * GiB, "ram_available": 120 * GiB, "commit_limit": 144 * GiB, "commit_used": 10 * GiB,
         "vram_total": 32 * GiB, "vram_free": 31 * GiB}
TIGHT = {**ROOMY, "commit_used": 123 * GiB, "ram_available": 44 * GiB}
F = loader.DEMO_FACTS
CONFIG = {"server": {"port": 7232, "vision": "dynamic"},
          "engine": {"max-context": 32768, "kv": "int8", "spec": 7, "spec-min-p": 0.5, "expert-cache": "auto",
                     "resident-experts": True, "vram-reserve-mib": 1200, "ple-io": "direct", "pool-workers": 15}}


class Plan(unittest.TestCase):
    def test_kv_follows_the_engine_s_bytes_per_token(self):
        for kv, per in loader.KV_TOKEN_BYTES.items():
            p = loader.plan({"engine.kv": kv, "engine.max-context": 100000}, F, ROOMY)
            kv_item = next(i for i in p["vram"]["items"] if i["key"] == "kv")
            self.assertEqual(kv_item["bytes"], per * 100000 + loader.SESSION_FIXED, kv)
        self.assertLess(loader.KV_TOKEN_BYTES["q4_0"], loader.KV_TOKEN_BYTES["k8v4"] < loader.KV_TOKEN_BYTES["int8"]
                        and loader.KV_TOKEN_BYTES["int8"])

    def test_slots_share_the_context_unless_full(self):
        shared = loader.plan({"engine.max-context": 65536, "server.parallel": 4}, F, ROOMY)
        full = loader.plan({"engine.max-context": 65536, "server.parallel": 4, "server.slot-full-context": True},
                           F, ROOMY)
        self.assertEqual(shared["slot_context"], 16384)
        self.assertEqual(full["slot_context"], 65536)
        kv = lambda p: next(i["bytes"] for i in p["vram"]["items"] if i["key"] == "kv")   # noqa: E731
        self.assertGreater(kv(full), 3 * kv(shared))

    def test_drafter_only_with_spec_2_or_more(self):
        drafter = lambda s: next(i["bytes"] for i in loader.plan({"engine.spec": s}, F, ROOMY)["vram"]["items"]  # noqa: E731
                                 if i["key"] == "drafter")
        self.assertEqual(drafter(0), 0)
        self.assertEqual(drafter(1), 0)
        self.assertGreater(drafter(7), F["mtp_bytes"])

    def test_auto_cache_takes_the_rest_and_resident_copy_the_others(self):
        p = loader.plan(loader.SAFE, F, ROOMY)
        v = p["vram"]
        fixed = sum(i["bytes"] for i in v["items"] if i["key"] != "experts")
        self.assertLessEqual(fixed + v["expert_slots"] * F["max_blob"] + v["reserve"], ROOMY["vram_free"])
        self.assertGreater(fixed + (v["expert_slots"] + 1) * F["max_blob"] + v["reserve"], ROOMY["vram_free"])
        arena = next(i["bytes"] for i in p["ram"]["items"] if i["key"] == "experts")
        self.assertEqual(arena, F["experts_bytes"] - v["expert_slots"] * F["max_blob"]
                         + loader.RESIDENT_SPARE_BLOBS * F["max_blob"])
        # the commit counts the committed RAM and the VRAM, not the file-backed PLE map
        self.assertEqual(p["commit"]["need"], p["ram"]["need"] + v["need"])
        self.assertTrue(p["fits"], p["problems"])

    def test_15_percent_of_ram_and_commit_stay_free(self):
        p = loader.plan(loader.SAFE, F, TIGHT)
        self.assertFalse(p["fits"])
        self.assertIn("commit", [x["code"] for x in p["problems"]])
        need = p["commit"]["need"]
        just = {**ROOMY, "commit_used": int(ROOMY["commit_limit"] * 0.85) - need}
        self.assertTrue(loader.plan(loader.SAFE, F, just)["fits"])
        over = {**ROOMY, "commit_used": int(ROOMY["commit_limit"] * 0.85) - need + (64 << 20)}
        self.assertEqual([x["code"] for x in loader.plan(loader.SAFE, F, over)["problems"]], ["commit"])
        low_ram = {**ROOMY, "ram_available": 30 * GiB}
        self.assertIn("ram", [x["code"] for x in loader.plan(loader.SAFE, F, low_ram)["problems"]])

    def test_the_running_engine_s_memory_counts_as_free(self):
        p = loader.plan(loader.SAFE, F, TIGHT, holds={"ram": 40 * GiB, "commit": 70 * GiB}, holds_vram=0)
        self.assertTrue(p["fits"], p["problems"])

    def test_vram_too_small(self):
        p = loader.plan(loader.SAFE, F, {**ROOMY, "vram_free": 6 * GiB})
        self.assertIn("vram", [x["code"] for x in p["problems"]])
        fixed = loader.plan({**loader.SAFE, "engine.expert-cache": 20000}, F, ROOMY)
        self.assertIn("vram", [x["code"] for x in fixed["problems"]])

    def test_ple_in_ram_only_when_it_fits(self):
        self.assertEqual(loader.safe(F, ROOMY)["engine.ple-io"], "ram")
        mid = {**ROOMY, "ram_available": 60 * GiB}
        self.assertEqual(loader.safe(F, mid)["engine.ple-io"], "direct")
        p = loader.plan({**loader.SAFE, "engine.ple-io": "ram"}, F, mid)
        self.assertEqual([x["code"] for x in p["problems"]], ["ple_ram"])
        self.assertIn("PLE direct", loader.problem_text(p["problems"][0], "it"))

    def test_unknown_readings_refuse_nothing(self):
        p = loader.plan(loader.SAFE, F, {k: None for k in ROOMY})
        self.assertTrue(p["fits"])
        self.assertEqual(sorted(p["unknown"]), ["commit", "ram", "vram"])

    def test_problem_texts_name_the_figures(self):
        for code in ("vram", "ram", "commit", "ple_ram"):
            for lang in ("en", "it"):
                t = loader.problem_text({"code": code, "need": 50 * GiB, "have": 40 * GiB, "keep": 19 * GiB}, lang)
                self.assertIn("50", t)
        self.assertIn("15%", loader.problem_text({"code": "commit", "need": 1, "have": 1, "keep": 1}, "it"))


class Speed(unittest.TestCase):
    def test_spec_factor_fits_the_seed(self):
        a = loader.accept_for(loader.SEED["decode_spec7"] / loader.SEED["decode_spec0"], 7)
        self.assertAlmostEqual(loader.spec_factor(7, a) * loader.SEED["decode_spec0"], loader.SEED["decode_spec7"],
                               delta=0.5)
        self.assertEqual(loader.spec_factor(0, a), 1.0)
        self.assertEqual(loader.spec_factor(1, a), 1.0)

    def test_hit_rate_curve(self):
        h = lambda s: loader.hit_rate(s, loader.SEED["cache_share"], loader.SEED["hit_rate"])   # noqa: E731
        self.assertAlmostEqual(h(loader.SEED["cache_share"]), loader.SEED["hit_rate"], places=6)
        self.assertLess(h(0.1), h(0.37))
        self.assertEqual(h(0), 0.0)
        self.assertEqual(h(1), 1.0)

    def seed_plan(self, spec):
        share = loader.SEED["cache_share"]
        slots = round(share * F["profile_slots"])
        return {"values": loader.effective({"engine.spec": spec, "engine.prefill": 512}),
                "vram": {"expert_slots": slots, "expert_slots_total": F["profile_slots"]}}

    def test_seed_numbers_come_back_at_the_seed_point(self):
        s0 = loader.predict_speed(self.seed_plan(0))
        s7 = loader.predict_speed(self.seed_plan(7))
        self.assertAlmostEqual(s0["decode"], 184, delta=1)
        self.assertAlmostEqual(s7["decode"], 320, delta=2)
        self.assertAlmostEqual(s0["prefill"], 892, delta=1)
        self.assertEqual(s0["source"]["kind"], "seed")
        self.assertIsNone(s0["decode_reasoning_visible"])

    def test_more_cache_is_faster_and_bigger_chunks_read_faster(self):
        small = loader.predict_speed(loader.plan({**loader.SAFE, "engine.vram-reserve-mib": 16000}, F, ROOMY))
        big = loader.predict_speed(loader.plan(loader.SAFE, F, ROOMY))
        self.assertGreater(big["decode"], small["decode"])
        chunk512 = loader.predict_speed(loader.plan({**loader.SAFE, "engine.prefill": 512}, F, ROOMY))
        self.assertGreater(big["prefill"], chunk512["prefill"])

    def test_a_calibration_wins(self):
        values = dict(loader.SAFE)
        p = loader.plan(values, F, ROOMY)
        cal = {"at": "2026-10-12T10:00:00+02:00", "fingerprint": loader.fingerprint(values), "decode_off": 250.0,
               "decode_on": 240.0, "visible_share": 0.4, "prefill": 1500.0, "prefill_chunk": 8192, "hit_rate": 0.97,
               "cache_share": 0.4, "spec": 7, "accept": 0.6}
        s = loader.predict_speed(p, cal)
        self.assertEqual((s["decode"], s["prefill"]), (250.0, 1500.0))
        self.assertEqual(s["decode_reasoning"], 240.0)
        self.assertEqual(s["decode_reasoning_visible"], 96.0)
        self.assertEqual(s["source"], {"kind": "calibration", "at": cal["at"], "exact": True})
        other = loader.predict_speed(loader.plan({**values, "engine.spec": 0}, F, ROOMY), cal)
        self.assertFalse(other["source"]["exact"])
        self.assertLess(other["decode"], 250.0)


def make_service(max_context=4096):
    tok = ByteTokenizer()
    return Service(MockEngine(tok, ["</think>\n\n" + "word " * 60], max_context=max_context), tok,
                   ChatTemplate(ROOT / "serve/chat_template.jinja"))


class Calibration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": self.dir.name, "BASALT_DATA": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_measures_and_stores_per_fingerprint(self):
        svc = make_service(16384)
        svc.settings_info = {"config": None, "running": {"engine.spec": 7, "engine.max-context": 16384}}
        sup = supervisor.Supervisor(svc, [], mock=True)
        with mock.patch.object(supervisor, "CALIBRATION_PROMPT", 600), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(sup.calibrate(wait=True))
        self.assertEqual(sup.status()["phase"], "ready", sup.status())
        runs = loader.read_calibrations(F)["runs"]
        self.assertEqual(len(runs), 1)
        rec = next(iter(runs.values()))
        self.assertEqual(rec["fingerprint"], loader.fingerprint(svc.settings_info["running"]))
        self.assertGreater(rec["decode_off"], 0)
        self.assertGreater(rec["prefill_tokens"], 500)
        self.assertTrue(loader.calibration_path(F).is_file())
        self.assertEqual(len(svc.engine.requests), 3)         # answer, answer with reasoning, long prompt
        self.assertEqual(loader.best_calibration(F, svc.settings_info["running"]), runs[rec["fingerprint"]])


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": self.dir.name, "BASALT_DATA": self.dir.name})
        self.env.start()
        self.path = os.path.join(self.dir.name, "basalt_config.json")
        Path(self.path).write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
        self.svc = make_service()
        self.svc.settings_info = {"config": self.path, "model": None, "cli": [], "running": {"engine.max-context": 4096}}
        self.sup = self.svc.supervisor = supervisor.Supervisor(self.svc, ["--mock-engine", "--config", self.path],
                                                               mock=True)
        self.sup.mock_delay = 0.2
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
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                c.request(method, path, body=data, headers=dict(headers or {}))
                r = c.getresponse()
                raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw
        finally:
            c.close()

    def post(self, what, body, **headers):
        return self.req("POST", f"/api/loader/{what}", body, {"Content-Type": "application/json", **headers})

    def wait_ready(self):
        for _ in range(100):
            st = self.req("GET", "/api/loader/status")[1]
            if st["phase"] in ("ready", "failed"):
                return st
            time.sleep(0.05)
        self.fail("the reload did not finish")

    def test_read(self):
        code, body = self.req("GET", "/api/loader")
        self.assertEqual(code, 200)
        self.assertTrue(body["editable"], body["reason"])
        self.assertEqual(body["values"]["engine.kv"], "int8")
        self.assertEqual(body["safe"]["engine.kv"], "q4_0")
        self.assertEqual(body["safe"]["engine.ple-io"], "ram")          # roomy: the PLE table fits
        self.assertIn("it", body["safe_why"]["engine.kv"])
        self.assertTrue(body["plan"]["fits"])
        self.assertEqual(body["plan"]["speed"]["source"]["kind"], "seed")
        self.assertIn("server.slot-full-context", body["fields"])

    def test_plan_is_a_computation(self):
        code, body = self.post("plan", {"values": {"engine.kv": "q4_0", "engine.max-context": 262144}})
        self.assertEqual(code, 200, body)
        self.assertIn("speed", body)
        self.assertEqual(body["slot_context"], 262144)
        self.assertEqual(self.post("plan", {"values": {"engine.kv": "fp8"}})[0], 400)
        self.assertEqual(self.post("plan", {"values": {"server.model": "x"}})[0], 400)
        self.assertEqual(json.loads(Path(self.path).read_text(encoding="utf-8")), CONFIG)

    def test_load_writes_the_file_and_reloads(self):
        code, body = self.post("load", {"values": {"engine.kv": "q4_0", "engine.max-context": 65536,
                                                   "server.parallel": 2, "server.slot-full-context": True}},
                               Origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code, 202, body)
        self.assertEqual(sorted(body["changed"]), ["engine.kv", "engine.max-context", "server.parallel",
                                                   "server.slot-full-context"])
        new = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual((new["engine"]["kv"], new["engine"]["max-context"]), ("q4_0", 65536))
        self.assertEqual(new["server"]["slot-full-context"], True)
        self.assertEqual(new["engine"]["resident-experts"], True)            # other keys kept
        self.assertEqual(json.loads(Path(self.path + ".bak").read_text(encoding="utf-8")), CONFIG)
        st = self.wait_ready()
        self.assertEqual(st["phase"], "ready", st)
        self.assertEqual(self.svc.engine.max_context, 65536)                # each slot holds the whole context
        self.assertEqual(self.svc.engine.batch, 2)
        self.assertEqual(self.svc.settings_info["running"]["engine.kv"], "q4_0")

    def test_a_plan_that_does_not_fit_is_refused(self):
        with mock.patch.object(loader, "system_now", return_value=dict(TIGHT)):
            code, body = self.post("load", {"values": {"engine.max-context": 262144}})
        self.assertEqual(code, 422, body)
        self.assertEqual(body["error"]["code"], "does_not_fit")
        self.assertIn("commit", [p["code"] for p in body["problems"]])
        self.assertIn("15%", body["problems"][0]["text_it"])
        self.assertEqual(json.loads(Path(self.path).read_text(encoding="utf-8")), CONFIG)
        self.assertEqual(self.sup.status()["phase"], "idle")

    def test_bad_values_are_400(self):
        for values in ({"engine.kv": "fp8"}, {"engine.ple-io": "disk"}, {"engine.pool-workers": 0},
                       {"engine.resident-experts": "yes"}, {"server.model": "x"}):
            self.assertEqual(self.post("load", {"values": values})[0], 400, values)
        self.assertEqual(json.loads(Path(self.path).read_text(encoding="utf-8")), CONFIG)

    def test_guards(self):
        self.assertEqual(self.post("load", {"values": {}}, Origin="http://evil.example.com")[0], 403)
        self.assertEqual(self.req("POST", "/api/loader/load", {"values": {}}, {"Content-Type": "text/plain"})[0], 415)
        with mock.patch("serve.settings.is_loopback", return_value=False):
            self.assertEqual(self.post("load", {"values": {}})[0], 403)
            self.assertEqual(self.post("calibrate", {})[0], 403)
            self.assertFalse(self.req("GET", "/api/loader")[1]["editable"])
            self.assertEqual(self.post("plan", {"values": {}})[0], 200)      # a plan changes nothing
        self.svc.api_key = "k"
        self.assertEqual(self.req("GET", "/api/loader")[0], 401)
        self.assertEqual(self.post("load", {"values": {}})[0], 401)
        self.assertEqual(self.post("plan", {"values": {}}, Authorization="Bearer k")[0], 200)
        self.assertEqual(self.post("nope", {}, Authorization="Bearer k")[0], 404)

    def test_one_job_at_a_time(self):
        self.sup.mock_delay = 1.0
        self.assertEqual(self.post("load", {"values": {"engine.spec": 5}})[0], 202)
        self.assertEqual(self.post("load", {"values": {"engine.spec": 6}})[0], 409)
        self.assertEqual(self.post("calibrate", {})[0], 409)
        self.wait_ready()

    def test_without_a_config_file(self):
        self.svc.settings_info = {"config": None, "running": {}}
        self.assertEqual(self.req("GET", "/api/loader")[1]["reason"], "no_config")
        self.assertEqual(self.post("load", {"values": {}})[0], 409)


class EngineRestart(unittest.TestCase):
    """The real reload path: the engine process is restarted in place with the new arguments."""

    def test_restart_in_place(self):
        class FakeEngine:
            def __init__(self):
                self.spawn = ("old-bin", ["--model", "m", "--kv", "int8"], None, "log", {})
                self.calls = []
                self.max_context = 4096

            def close(self):
                self.calls.append("close")

            def restart(self, tries=3):
                self.calls.append(("restart", tries, tuple(self.spawn[1])))
                seen.append(("restart", svc.loading))
                self.max_context = 65536

        svc = make_service()
        svc.engine = FakeEngine()
        seen = []
        orig = svc.fifo

        class Watch:
            def acquire(self, timeout=None):
                seen.append(("acquire", svc.loading))
                return orig.acquire(timeout=timeout)

            def release(self):
                seen.append(("release", svc.loading))
                orig.release()
        svc.fifo = Watch()
        cfg = {"engine_bin": "new-bin", "args": ["--model", "m", "--kv", "q4_0"], "log": "log2"}
        a = mock.Mock(model="m", engine_bin="new-bin", gpu=None, vision="off", parallel=None, slot_full_context=False,
                      port=7232, config=None, shared_context=None, sampling_launch={}, sampling_cli=set())
        sup = supervisor.Supervisor(svc, [], mock=False)
        sup.auto_calibrate = False
        with mock.patch.object(supervisor, "engine_config", return_value=cfg), \
                mock.patch.object(sup, "_parse", return_value=(a, ["--kv", "q4_0"])), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(sup.reload(wait=True))
        self.assertEqual(sup.status()["phase"], "ready", sup.status())
        self.assertEqual(svc.engine.calls, ["close", ("restart", 1, ("--model", "m", "--kv", "q4_0"))])
        self.assertEqual(svc.engine.spawn[0], "new-bin")
        self.assertEqual(svc.engine.spawn[3], "log2")
        self.assertEqual(seen, [("acquire", False), ("restart", True), ("release", False)])   # loading under the fifo
        self.assertFalse(svc.loading)
        self.assertEqual(svc.settings_info["running"]["engine.kv"], "q4_0")


if __name__ == "__main__":
    unittest.main()
