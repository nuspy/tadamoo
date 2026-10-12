"""The configuration wizard and "Machine Default": where the monitor is, the VRAM and the share of RAM and commit
left to other programs, the computed values on different PCs (fake hardware), the measurement on the mock engine,
the saved profile and preset."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import hardware, loader, profiles, supervisor
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.tests.test_models import tiny_basalt
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
GiB, MiB = 1 << 30, 1 << 20
BLACKWELL = {"name": "NVIDIA GeForce RTX 5090", "cc": [12, 0], "vram_total": 32 * GiB, "vram_free": 31 * GiB,
             "display_active": False}
INTEL = {"name": "Intel(R) UHD Graphics 770", "id": "PCI\\VEN_8086&DEV_A780", "primary": True, "outputs": 1}
NVIDIA = {"name": "NVIDIA GeForce RTX 5090", "id": "PCI\\VEN_10DE&DEV_2B85", "primary": False, "outputs": 0}
HYBRID = {"logical": 32, "physical": 24, "p_cores": 8, "e_cores": 16, "hybrid": True}


def system(ram_gib=128, vram_gib=32, used_vram_gib=1.0, commit_used_gib=20, page_gib=16):
    return {"ram_total": ram_gib * GiB, "ram_available": int((ram_gib - 8) * GiB),
            "commit_limit": (ram_gib + page_gib) * GiB, "commit_used": commit_used_gib * GiB,
            "vram_total": vram_gib * GiB, "vram_free": int((vram_gib - used_vram_gib) * GiB)}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_DATA": self.dir.name, "BASALT_CACHE": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()


class Monitor(unittest.TestCase):
    def test_the_monitor_on_the_compute_gpu(self):
        on = hardware.monitor_on(BLACKWELL, [{**NVIDIA, "primary": True, "outputs": 1}])
        self.assertEqual(on["where"], "primary")

    def test_a_separate_integrated_gpu_drives_it(self):
        on = hardware.monitor_on(BLACKWELL, [INTEL, NVIDIA])
        self.assertEqual((on["where"], on["primary_adapter"]), ("none", "Intel(R) UHD Graphics 770"))

    def test_igpu_primary_and_a_secondary_monitor_on_the_gpu_switched_off(self):
        """This PC's layout: the main monitor on the iGPU, a secondary one on the RTX that is almost always off
        (NVML says display_active although no desktop output is on the card)."""
        on = hardware.monitor_on({**BLACKWELL, "display_active": True}, [INTEL, NVIDIA])
        self.assertEqual((on["where"], on["secondary_off"], on["primary_adapter"]),
                         ("secondary", True, "Intel(R) UHD Graphics 770"))
        on = hardware.monitor_on(BLACKWELL, [INTEL, {**NVIDIA, "outputs": 1}])          # the secondary one is on
        self.assertEqual((on["where"], on["secondary_off"]), ("secondary", False))

    def test_without_adapter_information(self):
        self.assertEqual(hardware.monitor_on({**BLACKWELL, "display_active": True}, None)["where"], "primary")
        self.assertEqual(hardware.monitor_on(BLACKWELL, None)["where"], "unknown")
        self.assertEqual(hardware.monitor_on({"name": None}, None)["where"], "unknown")    # no NVML

    def test_the_fake_readings(self):
        env = {"BASALT_FAKE_SYSTEM_OK": "1", "BASALT_FAKE_DISPLAYS": json.dumps([INTEL, NVIDIA]),
               "BASALT_FAKE_GPU": json.dumps({**BLACKWELL, "display_active": True}),
               "BASALT_FAKE_PAGEFILE": json.dumps({"automatic": True, "files": [{"name": "C:\\pagefile.sys"}]})}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(hardware.monitor_on(hardware.gpu(), hardware.displays())["where"], "secondary")
            self.assertTrue(hardware.page_file()["automatic"])
        with mock.patch.dict(os.environ, {"BASALT_FAKE_SYSTEM_OK": "1", "BASALT_FAKE_GPU": "null"}):
            self.assertIsNone(hardware.gpu()["name"])
            self.assertIn("Nessuna GPU NVIDIA", hardware.gpu_warning(hardware.gpu(), "it"))


class Reserve(unittest.TestCase):
    def test_by_where_the_monitor_is(self):
        s = system(used_vram_gib=1.0)
        self.assertEqual(loader.reserve_proposal(s, "none")[0], 512)
        self.assertEqual(loader.reserve_proposal(s, "secondary")[0], 1024)          # 512 + 384, rounded up
        self.assertEqual(loader.reserve_proposal(s, "primary")[0], 2048)
        self.assertEqual(loader.reserve_proposal(s, None)[0], 1536)                 # not confirmed yet
        self.assertIn("monitor principale", loader.reserve_proposal(s, "primary")[2])

    def test_what_other_programs_use_is_said_not_added(self):
        s = system(used_vram_gib=20)
        mib, en, it = loader.reserve_proposal(s, "none")
        self.assertEqual(mib, 512)                       # the auto cache already leaves their VRAM to them
        self.assertIn("20.0 GB", en)
        self.assertIn("20,0 GB", it)
        self.assertIn("1,0 GB", loader.reserve_proposal(s, "none", holds_vram=19 * GiB)[2])   # 19 GB are ours
        self.assertNotIn("usano ora", loader.reserve_proposal({"vram_total": None, "vram_free": None}, "none")[2])


class Proposals(Isolated):
    def hw(self, s=None, gpu=None, displays=(INTEL, NVIDIA), cpu=HYBRID):
        g = dict(gpu or BLACKWELL)
        return {"system": s or system(), "cpu": cpu, "gpu": g, "displays": list(displays),
                "monitor": hardware.monitor_on(g, list(displays)), "saved": {}}

    def test_the_tested_values_on_a_roomy_pc(self):
        p = loader.wizard_proposal(self.hw(), loader.DEMO_FACTS)
        v = p["values"]
        self.assertEqual((v["engine.kv"], v["engine.prefill"], v["engine.spec"], v["engine.spec-min-p"]),
                         ("q4_0", "auto", 7, 0.5))
        self.assertEqual(v["engine.max-context"], 262144)
        self.assertTrue(v["engine.resident-experts"])
        self.assertEqual(v["engine.pool-workers"], 15)
        self.assertEqual((p["monitor"], p["vram_reserve_mib"]), ("none", 512))
        self.assertTrue(p["fits"])
        self.assertEqual(p["sampling"]["top_k"], 20)

    def test_a_16_gb_card(self):
        s = system(vram_gib=16)
        p = loader.wizard_proposal(self.hw(s, {**BLACKWELL, "vram_total": 16 * GiB, "vram_free": 15 * GiB}),
                                   loader.DEMO_FACTS)
        self.assertTrue(p["fits"])
        self.assertLess(p["expert_slots"][0], loader.wizard_proposal(self.hw(), loader.DEMO_FACTS)["expert_slots"][0])

    def test_less_ram(self):
        p = loader.wizard_proposal(self.hw(system(ram_gib=64, commit_used_gib=8)), loader.DEMO_FACTS)
        self.assertTrue(p["fits"], p["problems"])
        self.assertEqual(p["values"]["engine.ple-io"], "direct")               # the PLE table would not leave 15%
        # 32 GB of RAM and a 16 GB card: this model's experts do not fit; the wizard says what is missing
        small = system(ram_gib=32, vram_gib=16, commit_used_gib=6)
        card = {**BLACKWELL, "vram_total": 16 * GiB, "vram_free": 15 * GiB}
        p = loader.wizard_proposal(self.hw(small, card), loader.DEMO_FACTS)
        self.assertFalse(p["fits"])
        self.assertEqual(p["values"]["engine.max-context"], loader.CONTEXT_STEPS[-1])
        self.assertIn("nessun contesto", p["why"]["engine.max-context"]["it"])
        self.assertIn("ram", [x["code"] for x in p["problems"]])
        p = loader.wizard_proposal(self.hw(small, card), loader.DEMO_FACTS, {"free_share": 0.10})
        self.assertEqual(p["free_share"], 0.10)
        self.assertEqual(p["keep"]["ram"], int(0.10 * 32 * GiB))
        # 32 GB of RAM and a 32 GB card with a small page file: Windows charges the VRAM to the commit
        p = loader.wizard_proposal(self.hw(system(ram_gib=32, commit_used_gib=6, page_gib=4)), loader.DEMO_FACTS)
        self.assertFalse(p["fits"])
        self.assertIn("commit", [x["code"] for x in p["problems"]])

    def test_the_monitor_on_the_compute_gpu_leaves_more_vram(self):
        same = self.hw(displays=[{**NVIDIA, "primary": True, "outputs": 1}])
        p = loader.wizard_proposal(same, loader.DEMO_FACTS)
        self.assertEqual((p["monitor"], p["vram_reserve_mib"]), ("primary", 2048))
        p = loader.wizard_proposal(same, loader.DEMO_FACTS, {"monitor": "none"})    # the user corrects it
        self.assertEqual(p["vram_reserve_mib"], 512)
        p = loader.wizard_proposal(same, loader.DEMO_FACTS, {"vram_reserve_mib": 4096})
        self.assertEqual(p["values"]["engine.vram-reserve-mib"], 4096)

    def test_a_card_that_is_not_blackwell_and_no_nvml(self):
        with mock.patch.dict(os.environ, {"BASALT_FAKE_SYSTEM_OK": "1",
                                          "BASALT_FAKE_GPU": json.dumps({"name": "NVIDIA GeForce RTX 4090",
                                                                         "cc": [8, 9]})}):
            self.assertIn("non è supportata", hardware.gpu_warning(hardware.gpu(), "it"))
        unknown = {**system(), "vram_total": None, "vram_free": None}
        p = loader.wizard_proposal(self.hw(unknown, {"name": None}, displays=()), loader.DEMO_FACTS)
        self.assertIsNone(p["monitor"])
        self.assertEqual(p["vram_reserve_mib"], 1536)
        self.assertIsNone(p["have"]["vram"])

    def test_bad_answers(self):
        for bad in ({"free_share": 0.9}, {"free_share": "15%"}, {"monitor": "left"}, {"vram_reserve_mib": -1}):
            with self.assertRaises(ValueError, msg=bad):
                loader.wizard_proposal(self.hw(), loader.DEMO_FACTS, bad)

    def test_shrink(self):
        v = {"engine.max-context": 32768, "engine.resident-experts": True}
        self.assertEqual(loader.shrink(v)["engine.max-context"], 16384)
        v = {"engine.max-context": 4096, "engine.resident-experts": True}
        self.assertFalse(loader.shrink(v)["engine.resident-experts"])
        self.assertIsNone(loader.shrink({"engine.max-context": 4096, "engine.resident-experts": False}))


class Save(Isolated):
    def test_machine_default_and_the_share_everywhere(self):
        model = str(tiny_basalt(Path(self.dir.name) / "m.basalt"))
        out = loader.wizard_save(model, {**loader.SAFE, "engine.max-context": 131072}, 0.20, 2048, "secondary",
                                 {"temperature": 1.0, "top_p": 0.95, "top_k": 20}, {"decode": 200.0}, 7240)
        self.assertEqual(profiles.default_name(), profiles.MACHINE_NAME)
        listing = profiles.listing()
        md = next(p for p in listing["load"] if p["name"] == profiles.MACHINE_NAME)
        self.assertEqual((md["values"]["engine.max-context"], md["values"]["engine.vram-reserve-mib"],
                          md["values"]["server.port"]), (131072, 2048, 7240))
        self.assertTrue(md["model"].endswith("m.basalt"))
        self.assertEqual(listing["inference_default"], profiles.MACHINE_NAME)
        preset = next(p for p in listing["inference"] if p["name"] == profiles.MACHINE_NAME)
        self.assertEqual(preset["settings"], {"temperature": 1.0, "top_p": 0.95, "top_k": 20})
        saved = loader.machine_settings()
        self.assertEqual((saved["free_share"], saved["vram_reserve_mib"], saved["monitor"]), (0.2, 2048, "secondary"))
        self.assertEqual(out["machine"]["wizard"]["measured"], {"decode": 200.0})
        # the share is the rule of every plan and of the safe values from now on
        p = loader.plan(loader.SAFE, loader.DEMO_FACTS, system())
        self.assertEqual(p["ram"]["keep"], int(0.2 * 128 * GiB))
        self.assertEqual(loader.safe(loader.DEMO_FACTS, system(), cpu=HYBRID)["engine.vram-reserve-mib"], 2048)
        self.assertIn("20%", loader.problem_text({"code": "ram", "need": 1, "have": 1, "keep": 1, "share": 0.2}, "it"))

    def test_the_floor(self):
        self.assertEqual(loader.keep_bytes(8 * GiB, 0.05), loader.FREE_FLOOR)


def make_service():
    tok = ByteTokenizer()
    return Service(MockEngine(tok, ["</think>\n\n" + "word " * 40], max_context=8192), tok,
                   ChatTemplate(ROOT / "serve/chat_template.jinja"))


class Measure(Isolated):
    def setUp(self):
        super().setUp()
        self.cfg = Path(self.dir.name) / "basalt_config.json"
        self.cfg.write_text(json.dumps({"server": {"port": 7286}, "engine": {"max-context": 4096}}), encoding="utf-8")
        self.svc = make_service()
        self.svc.settings_info = {"config": str(self.cfg), "model": None, "cli": [], "running": {}}
        self.sup = self.svc.supervisor = supervisor.Supervisor(self.svc, ["--mock-engine", "--config", str(self.cfg)],
                                                               mock=True)
        self.sup.mock_delay = 0.05
        self.patches = [mock.patch.object(loader, "system_now", return_value=system()),
                        mock.patch.object(supervisor, "CALIBRATION_PROMPT", 400),
                        mock.patch.object(supervisor, "MEASURE_ANSWER", 32)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def run_measure(self, values):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(self.sup.measure(values, wait=True))
        return self.sup.status()

    def test_load_calibrate_and_try_the_drafting(self):
        values = {**loader.SAFE, "engine.max-context": 8192}
        st = self.run_measure(values)
        self.assertEqual(st["phase"], "ready", st)
        out = json.loads(st["message"])
        self.assertIn(out["values"]["engine.spec"], supervisor.SPEC_TRIES)
        self.assertEqual(sorted(map(int, out["measured"]["spec"])), list(supervisor.SPEC_TRIES))   # all three shown
        spec = out["measured"]["spec"]
        best = max(spec.values())
        self.assertEqual(out["values"]["engine.spec"], min(int(s) for s, d in spec.items() if d == best))
        self.assertIn(out["values"]["engine.spec-min-p"], supervisor.SPEC_MIN_P_TRIES)
        self.assertEqual(sorted(map(float, out["measured"]["spec_min_p"])), list(supervisor.SPEC_MIN_P_TRIES))
        self.assertGreater(out["measured"]["decode"], 0)
        written = json.loads(self.cfg.read_text(encoding="utf-8"))["engine"]
        self.assertEqual(written["spec"], out["values"]["engine.spec"])         # the file has the kept values
        sent = [s for _, s in self.svc.engine.requests if "spec_min_p" in s]
        self.assertGreaterEqual(len(sent), 3)                                   # per request, no reload

    def test_the_drafting_floor_reaches_the_engine_per_request(self):
        from serve.engine.process import ProcessEngine
        self.assertIn("spec_min_p=0.3", ProcessEngine.sampling_keys({"spec_min_p": 0.3}))
        for bad in (1.5, -1, True, "0.5"):
            self.assertNotIn("spec_min_p", ProcessEngine.sampling_keys({"spec_min_p": bad}))

    def test_above_the_plan_it_shrinks(self):
        tight = {**system(), "vram_free": 100 * MiB}
        with mock.patch.object(loader, "system_now", return_value=tight):
            st = self.run_measure({**loader.SAFE, "engine.max-context": 8192, "engine.spec": 0})
        self.assertEqual(st["phase"], "failed", st)
        self.assertIn("above the plan", st["error"])
        log = json.loads(st["message"])["log"]
        self.assertEqual(len(log), 3)

    def test_refused_with_the_commit_too_high(self):
        with mock.patch.object(loader, "system_now", return_value=system(commit_used_gib=130)):
            st = self.run_measure(dict(loader.SAFE))
        self.assertEqual(st["phase"], "failed")
        self.assertIn("75%", st["error"])


class OverHttp(Isolated):
    def setUp(self):
        super().setUp()
        root = Path(self.dir.name)
        self.model = tiny_basalt(root / "a.basalt", "A")
        self.cfg = root / "basalt_config.json"
        self.cfg.write_text(json.dumps({"server": {"model": str(self.model), "port": 7287}}), encoding="utf-8")
        self.svc = make_service()
        self.svc.standby = True
        self.svc.settings_info = {"config": str(self.cfg), "model": str(self.model), "cli": [],
                                  "running": {"server.port": 7287}}
        self.sup = self.svc.supervisor = supervisor.Supervisor(self.svc, ["--mock-engine", "--config", str(self.cfg)],
                                                               mock=True)
        self.sup.mock_delay = 0.05
        env = {"BASALT_FAKE_SYSTEM_OK": "1", "BASALT_FAKE_SYSTEM": json.dumps(system()),
               "BASALT_FAKE_GPU": json.dumps({**BLACKWELL, "display_active": True}),
               "BASALT_FAKE_DISPLAYS": json.dumps([INTEL, NVIDIA]), "BASALT_FAKE_CPU": json.dumps(HYBRID),
               "BASALT_FAKE_PAGEFILE": json.dumps({"automatic": False, "files": []})}
        self.fake = mock.patch.dict(os.environ, env)
        self.fake.start()
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fake.stop()
        super().tearDown()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                c.request(method, path, body=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"} if body is not None else {})
                r = c.getresponse()
                raw = r.read()
            return r.status, json.loads(raw)
        finally:
            c.close()

    def test_detect_plan_save(self):
        code, info = self.req("GET", "/api/wizard")
        self.assertEqual(code, 200, info)
        hw = info["hardware"]
        self.assertEqual((hw["monitor"]["where"], hw["monitor"]["secondary_off"]), ("secondary", True))
        self.assertEqual(hw["monitor"]["primary_adapter"], "Intel(R) UHD Graphics 770")
        self.assertTrue(hw["can_measure"])
        self.assertTrue(info["same_model"])
        prop = info["proposal"]
        self.assertEqual((prop["monitor"], prop["vram_reserve_mib"]), ("secondary", 1024))
        code, plan = self.req("POST", "/api/wizard/plan", {"answers": {"monitor": "none", "free_share": 0.2}})
        self.assertEqual(code, 200, plan)
        self.assertEqual((plan["vram_reserve_mib"], plan["free_share"]), (512, 0.2))
        code, saved = self.req("POST", "/api/wizard/save", {"values": plan["values"], "free_share": 0.2,
                                                           "vram_reserve_mib": 512, "monitor": "none"})
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved["values"]["server.port"], 7287)
        self.assertEqual(self.req("GET", "/api/profiles")[1]["default"], profiles.MACHINE_NAME)
        self.assertEqual(self.req("GET", "/api/loader")[1]["free_share"], 0.2)

    def test_measure_over_http(self):
        plan = self.req("GET", "/api/wizard")[1]["proposal"]
        with mock.patch.object(supervisor, "CALIBRATION_PROMPT", 400), mock.patch.object(supervisor, "MEASURE_ANSWER", 32):
            code, body = self.req("POST", "/api/wizard/measure", {"values": {**plan["values"], "engine.max-context": 8192}})
            self.assertEqual(code, 202, body)
            for _ in range(300):
                st = self.req("GET", "/api/loader/status")[1]
                if st["phase"] in ("ready", "failed"):
                    break
                import time
                time.sleep(0.05)
        self.assertEqual(st["phase"], "ready", st)
        self.assertEqual(st["job"], "measure")
        self.assertFalse(self.svc.standby)

    def test_guards(self):
        with mock.patch("serve.settings.is_loopback", return_value=False):
            self.assertEqual(self.req("POST", "/api/wizard/save", {"values": {}})[0], 403)
            self.assertEqual(self.req("POST", "/api/wizard/plan", {})[0], 200)
        self.assertEqual(self.req("POST", "/api/wizard/nope", {})[0], 404)


class RealMemory(Isolated):
    """The engine's real memory with the model loaded, kept per model and added to later plans."""
    F = {**loader.DEMO_FACTS, "meta_hash": "00aa11bb22cc33dd", "name": "real"}

    def test_recorded_and_used(self):
        values = {**loader.SAFE, "engine.max-context": 65536}
        before = system()
        planned = loader.plan(values, self.F, before)
        after = {**before, "ram_available": before["ram_available"] - planned["ram"]["need"] - 1 * GiB,
                 "commit_used": before["commit_used"] + planned["commit"]["need"] + int(1.7 * GiB)}
        r = loader.record_load_memory(self.F, values, before, after)
        self.assertEqual((r["ram_extra"], r["commit_extra"]), (1 * GiB, int(1.7 * GiB)))
        again = loader.plan(values, self.F, before)
        self.assertEqual(again["ram"]["need"], planned["ram"]["need"] + 1 * GiB)
        self.assertEqual(again["commit"]["need"], planned["commit"]["need"] + int(1.7 * GiB))
        self.assertIn("measured", [i["key"] for i in again["ram"]["items"]])
        # a later load within the plan clears it
        loader.record_load_memory(self.F, values, before, {**before, "ram_available": before["ram_available"] - GiB,
                                                           "commit_used": before["commit_used"] + GiB})
        self.assertEqual(loader.plan(values, self.F, before)["commit"]["need"], planned["commit"]["need"])

    def test_not_for_the_demo_sizes_nor_without_readings(self):
        self.assertIsNone(loader.record_load_memory(loader.DEMO_FACTS, {}, system(), system()))
        self.assertIsNone(loader.record_load_memory(self.F, {}, {"ram_available": None}, system()))
        self.assertEqual(loader.memory_correction(loader.DEMO_FACTS), {"ram": 0, "commit": 0})

    def test_the_reload_records_it(self):
        readings = iter([system(), {**system(), "commit_used": 100 * GiB, "ram_available": 10 * GiB}])
        with mock.patch.object(loader, "system_now", side_effect=lambda *a: next(readings, system())), \
                contextlib.redirect_stdout(io.StringIO()):
            supervisor.record_memory(self.F, dict(loader.SAFE), loader.system_now(), loader.system_now())
        self.assertGreater(loader.memory_correction(self.F)["commit"], 0)


class Cli(Isolated):
    def test_without_questions(self):
        env = {"BASALT_FAKE_SYSTEM_OK": "1", "BASALT_FAKE_SYSTEM": json.dumps(system()),
               "BASALT_FAKE_GPU": json.dumps(BLACKWELL), "BASALT_FAKE_DISPLAYS": json.dumps([INTEL, NVIDIA]),
               "BASALT_FAKE_CPU": json.dumps(HYBRID), "BASALT_FAKE_PAGEFILE": json.dumps({"files": []})}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = loader.main(["wizard", "--json", "--free-share", "0.2", "--save", "--port", "7240"])
        self.assertEqual(code, 0, out.getvalue())
        data = json.loads(out.getvalue())
        self.assertEqual((data["proposal"]["monitor"], data["proposal"]["free_share"]), ("none", 0.2))
        self.assertEqual(data["saved"]["values"]["server.port"], 7240)
        self.assertEqual(profiles.default_name(), profiles.MACHINE_NAME)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            self.assertEqual(loader.main(["wizard", "--monitor", "primary", "--vram-libera", "3"]), 0)
        self.assertIn("monitor principale", out.getvalue())
        self.assertIn("engine.vram-reserve-mib = 3072", out.getvalue())


class PendingMeasure(Isolated):
    def test_the_measurement_after_the_restart_with_the_model(self):
        from serve import loader_api
        svc = make_service()
        svc.settings_info = {"config": None, "model": "C:/m/a.basalt", "running": {}}
        svc.supervisor = mock.Mock()
        self.assertFalse(loader_api.pending_measure(svc))
        loader.save_machine_settings({"pending_measure": {"model": "C:/m/b.basalt", "values": {}}})
        self.assertFalse(loader_api.pending_measure(svc))                       # another model: not this one
        loader.save_machine_settings({"pending_measure": {"model": "C:/m/a.basalt", "values": {"engine.kv": "q4_0"}}})
        self.assertTrue(loader_api.pending_measure(svc))
        svc.supervisor.measure.assert_called_once_with({"engine.kv": "q4_0"})
        self.assertIsNone(loader.machine_settings()["pending_measure"])


if __name__ == "__main__":
    unittest.main()
