"""The loading order and the VRAM accounting of the voice functions: the plan (what loads, what unloads, whether it fits), adding with the
engine running, removing without touching it, 'Change services in memory', the Loader's own Load, and a service that is down.

The server is the real one over HTTP with a mock engine and the real mock Supervisor. The voice service is a stand-in behind
`voice_service.call` that keeps a fake GPU's free VRAM; no model is loaded, no GPU is read, no network is used."""
from __future__ import annotations

import contextlib
import http.client
import http.server
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from serve import loader, supervisor, userdata, voice_catalog, voice_loader, voice_service, voice_settings
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.tests.test_models import ROOMY, tiny_basalt
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
MIB = 1 << 20
COSTS = {"whisper-small": 600, "whisper-tiny": 150, "kokoro": 330, "musetalk-v15": 6800}      # what the fake service's models take, MiB
FITS = {"engine.expert-cache": 8}


class FakeVoice:
    """The voice service behind `voice_service.call`: the functions' state, a fake GPU's free VRAM, and a log of everything that happened."""

    def __init__(self, log, pool):
        self.log, self.pool, self.down, self.installed, self.fail = log, pool, False, set(), {}
        self.state = {f: {"state": "unloaded", "model": voice_settings.selected(f), "loaded_with": None, "measured_mib": None, "error": None}
                      for f in voice_loader.FUNCTIONS}
        self.calls: list = []

    def call(self, method, route, body=None, timeout=3.0):
        if self.down:
            raise voice_service.ServiceDown("the voice service has not started: it leaves no info file")
        self.calls.append((method, route, body))
        if route == "/functions" and method == "GET":
            return 200, {"functions": {f: dict(s) for f, s in self.state.items()}, "free_vram_mib": self.pool["free"]}
        assert (method, route) == ("POST", "/functions/apply"), (method, route)
        report = {}
        for f, want in body["functions"].items():
            s = self.state[f]
            if s["state"] == "loaded" and (not want["on"] or s["loaded_with"] != {k: want[k] for k in ("model", "device", "memory")}):
                self.pool["free"] += s["measured_mib"] or 0
                self.log.append(("service_unload", f))
                report[f] = {"action": "unload", "state": "unloaded", "measured_mib": s["measured_mib"], "error": None}
                s.update(state="unloaded", loaded_with=None, measured_mib=None)
            if want["on"] and s["state"] != "loaded":
                if want["model"] in self.installed and f not in self.fail:
                    cost = COSTS[want["model"]] if want["memory"] == "vram" else 0
                    before, self.pool["free"] = self.pool["free"], self.pool["free"] - cost
                    self.log.append(("service_load", f, want["model"], want["estimated_mib"]))
                    s.update(state="loaded", model=want["model"], measured_mib=cost or None, error=None,
                             loaded_with={k: want[k] for k in ("model", "device", "memory")})
                    report[f] = {"action": "load", "state": "loaded", "measured_mib": cost or None, "before_free_mib": before,
                                 "after_free_mib": self.pool["free"], "error": None}
                else:
                    msg = self.fail.get(f) or "not installed"
                    s.update(state="unloaded", error=msg)
                    report[f] = {"action": "load", "state": "unloaded", "measured_mib": None, "error": msg}
            else:
                report.setdefault(f, {"action": "keep", "state": s["state"], "measured_mib": None, "error": None})
        return 200, {"report": report, "free_vram_mib": self.pool["free"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.env = mock.patch.dict(os.environ, {"TADAMOO_CACHE": str(root), "TADAMOO_DATA": str(root)})
        self.env.start()
        self.models = root / "voice-models"
        voice_settings.set_models_dir(str(self.models))
        voice_loader._job.clear()
        voice_loader._job["phase"] = "idle"
        voice_loader._pending = None
        self.log: list = []
        self.pool = {"free": 12000}
        self.voice = FakeVoice(self.log, self.pool)
        self.patches = [mock.patch.object(voice_service, "call", self.voice.call)]
        self.a = tiny_basalt(root / "a.basalt", "Model A")
        self.b = tiny_basalt(root / "b.basalt", "Model B")
        self.cfg = root / "tadamoo_config.json"
        self.cfg.write_text(json.dumps({"server": {"model": str(self.a)}, "engine": {"max-context": 4096, "vram-reserve-mib": 1200}}), encoding="utf-8")
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, ["</think>\n\nhello"]), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.settings_info = {"config": str(self.cfg), "model": str(self.a), "cli": [], "running": {}}
        self.sup = self.svc.supervisor = supervisor.Supervisor(self.svc, ["--mock-engine", "--config", str(self.cfg)], mock=True)
        self.sup.mock_delay = 0.1
        log, pool = self.log, self.pool
        real_reload = self.sup.reload

        def reload(wait=False, held=False):
            log.append(("sup_reload", held, pool["free"]))
            return real_reload(wait, held)
        self.sup.reload = reload
        real_hold, real_release = self.sup.hold, self.sup.release
        self.sup.hold = lambda job, phase: (log.append(("sup_hold", job)), real_hold(job, phase))[1]
        self.sup.release = lambda error=None: (log.append(("sup_release", error)), real_release(error))[1]

        def unload(idle_for=None):
            log.append(("llm_unload",))
            pool["free"] += 5000
            return "unloaded"
        self.svc.unload = unload
        self.patches += [mock.patch.object(loader, "system_now", lambda gpu=0: {**ROOMY, "vram_total": 24000 * MIB, "vram_free": pool["free"] * MIB})]
        for p in self.patches:
            p.start()
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.dir.cleanup()

    # ---- helpers
    def install(self, *ids):
        for model in ids:
            e = voice_catalog.find(model)
            folder = self.models.joinpath(*voice_catalog.folder_of(e).split("/"))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "w.bin").write_bytes(b"x")
            (folder / voice_catalog.MARKER).write_text(json.dumps({"files": [{"name": "w.bin", "size": 1}]}), encoding="utf-8")
            self.voice.installed.add(model)

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                c.request(method, path, body=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                r = c.getresponse()
                raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw
        finally:
            c.close()

    def wait(self, cond, what):
        for _ in range(200):
            if cond():
                return
            time.sleep(0.05)
        self.fail(what)

    def wait_job(self):
        self.wait(lambda: voice_loader._job.get("phase") in ("done", "failed"), "the voice job did not end")
        self.wait(lambda: self.sup.status()["phase"] in ("idle", "ready", "failed"), "the supervisor did not settle")
        return dict(voice_loader._job)

    def functions(self):
        code, body = self.req("GET", "/api/voice/functions")
        self.assertEqual(code, 200, body)
        return body

    def kinds(self):
        return [e[0] for e in self.log]

    def apply(self, desired, **extra):
        return self.req("POST", "/api/voice/functions/apply", {"desired": desired, "confirm": True, **extra})


class Settings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TADAMOO_DATA": self.dir.name, "TADAMOO_CACHE": self.dir.name})
        self.env.start()
        voice_settings.set_models_dir(str(Path(self.dir.name) / "vm"))

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_enabled_is_off_except_for_an_installed_stt_and_is_validated(self):
        self.assertEqual([f for f in voice_catalog.SERVICES if voice_settings.enabled(f)], [])
        e = voice_catalog.find("whisper-small")
        folder = Path(self.dir.name, "vm", *voice_catalog.folder_of(e).split("/"))
        folder.mkdir(parents=True)
        (folder / "small.pt").write_bytes(b"x")
        self.assertEqual([f for f in voice_catalog.SERVICES if voice_settings.enabled(f)], ["stt"])
        voice_settings.set_enabled("stt", False)
        voice_settings.set_enabled("tts", True)
        self.assertEqual([f for f in voice_catalog.SERVICES if voice_settings.enabled(f)], ["tts"])
        data = json.loads(userdata.path("voice.json").read_text(encoding="utf-8"))
        self.assertIs(data["services"]["tts"]["enabled"], True)
        for bad in ("yes", 1, None):
            with self.assertRaises(voice_settings.VoiceSettingsError):
                voice_settings.set_enabled("tts", bad)
        with self.assertRaises(voice_settings.VoiceSettingsError):
            voice_settings.set_enabled("nope", True)

    def test_estimates_prefer_what_was_measured_and_never_guess(self):
        self.assertEqual(voice_loader.estimate_mib("musetalk-v15", "auto", "vram"), (6676, "catalog"))
        self.assertEqual(voice_loader.estimate_mib("whisper-small", "auto", "vram"), (None, "unknown"))      # the catalog has no size for it
        self.assertEqual(voice_loader.estimate_mib("musetalk-v15", "cpu", "ram"), (0, "cpu"))
        voice_loader.record_measured("whisper-small", "auto", "vram", 812)
        self.assertEqual(voice_loader.estimate_mib("whisper-small", "auto", "vram"), (812, "measured"))
        self.assertEqual(voice_loader.estimate_mib("whisper-small", "cuda:1", "vram"), (None, "unknown"))    # measured elsewhere
        self.assertEqual(voice_loader.estimate_mib("whisper-small", "auto", "ram"), (0, "cpu"))


class Plan(Base):
    def test_the_diff_says_what_loads_unloads_and_stays(self):
        self.install("whisper-small", "kokoro", "musetalk-v15")
        voice_settings.set_enabled("avatar", True)
        self.apply({"stt": {"on": True}, "avatar": {"on": True}})
        self.wait_job()
        self.log.clear()
        code, p = self.req("POST", "/api/voice/functions/plan", {"desired": {"stt": {"on": True}, "tts": {"on": True}, "avatar": {"on": False}}})
        self.assertEqual(code, 200, p)
        acts = {i["id"]: i["action"] for i in p["items"]}
        self.assertEqual(acts, {"stt": "keep", "tts": "load", "avatar": "unload"})
        tts = next(i for i in p["items"] if i["id"] == "tts")
        self.assertEqual((tts["estimated_mib"], tts["estimate_source"]), (325, "catalog"))
        self.assertEqual(p["need_mib"], 325)
        self.assertEqual(p["recover_mib"], 6800)                                  # what the service measured when it loaded the avatar
        self.assertEqual(p["free_mib"], self.pool["free"])
        self.assertEqual(p["reserve_mib"], 1200)
        self.assertEqual(p["left_for_llm_mib"], self.pool["free"] + 6800 - 325 - 1200)
        self.assertTrue(p["fits"] and p["changes"])
        self.assertEqual(self.log, [])                                            # a plan changes nothing
        self.assertEqual([c for c in self.voice.calls if c[0] == "POST"][-1][1], "/functions/apply")
        self.assertEqual(len([c for c in self.voice.calls if c[0] == "POST"]), 1)  # only the earlier apply

    def test_a_missing_model_is_named_and_not_counted(self):
        code, p = self.req("POST", "/api/voice/functions/plan", {"desired": {"tts": {"on": True}}})
        tts = next(i for i in p["items"] if i["id"] == "tts")
        self.assertEqual(tts["action"], "missing")
        self.assertEqual(tts["reason"], "Kokoro is not installed: install it from Settings > Voice.")
        self.assertEqual((p["need_mib"], p["changes"]), (0, False))

    def test_a_changed_model_is_a_reload_and_what_does_not_fit_says_so(self):
        self.install("whisper-small", "whisper-tiny", "musetalk-v15")
        self.apply({"stt": {"on": True}})
        self.wait_job()
        code, p = self.req("POST", "/api/voice/functions/plan", {"desired": {"stt": {"on": True, "model": "whisper-tiny"}}})
        self.assertEqual(next(i["action"] for i in p["items"] if i["id"] == "stt"), "reload")
        self.pool["free"] = 3000
        code, p = self.req("POST", "/api/voice/functions/plan", {"desired": {"avatar": {"on": True}}})
        self.assertEqual((p["need_mib"], p["fits"]), (6676, False))
        self.assertLess(p["left_for_llm_mib"], 0)
        self.assertTrue(p["llm_running"] and p["llm_reload"])

    def test_an_unknown_estimate_leaves_fits_open(self):
        self.install("whisper-small")
        code, p = self.req("POST", "/api/voice/functions/plan", {"desired": {"stt": {"on": True}}})
        self.assertEqual((p["unknown"], p["fits"]), (["stt"], None))
        self.assertEqual(self.apply({"stt": {"on": True}})[0], 202)                      # an open question does not stop an apply
        self.wait_job()

    def test_a_cpu_function_needs_no_vram_and_no_reload(self):
        self.install("musetalk-v15")
        code, p = self.req("POST", "/api/voice/functions/plan", {"desired": {"avatar": {"on": True, "device": "cpu", "memory": "ram"}}})
        self.assertEqual((p["need_mib"], p["llm_reload"], p["fits"]), (0, False, True))

    def test_bad_requests_are_refused(self):
        for desired in ({"nope": {"on": True}}, {"stt": {"on": "yes"}}, {"stt": {"on": True, "model": "kokoro"}}, {"stt": {"on": True, "device": "tpu"}},
                        {"stt": {"on": True, "device": "cpu", "memory": "vram"}}, "text"):
            code, body = self.req("POST", "/api/voice/functions/plan", {"desired": desired})
            self.assertEqual(code, 400, (desired, body))


class Adding(Base):
    def test_with_the_engine_running_the_model_is_unloaded_the_service_applies_and_the_model_loads_again(self):
        self.install("kokoro", "musetalk-v15")
        code, out = self.apply({"tts": {"on": True}, "avatar": {"on": True}})
        self.assertEqual((code, out["status"]), (202, "started"))
        self.assertTrue(out["plan"]["llm_reload"])
        job = self.wait_job()
        self.assertEqual(job["phase"], "done", job)
        order = [e[0] if e[0] != "service_load" else e[:2] for e in self.log]
        self.assertEqual(order, ["sup_hold", "llm_unload", ("service_load", "tts"), ("service_load", "avatar"), "sup_reload"])
        reload = next(e for e in self.log if e[0] == "sup_reload")
        self.assertEqual(reload[1:], (True, 12000 + 5000 - 330 - 6800))             # it reads the free VRAM the functions left, and takes the held slot
        self.assertEqual(job["report"]["avatar"]["measured_mib"], 6800)
        self.assertEqual(self.sup.status()["phase"], "ready")
        self.assertFalse(self.svc.standby)
        self.assertEqual(voice_loader.measured()["musetalk-v15"]["mib"], 6800)       # kept as the next estimate
        estimates = [e[3] for e in self.log if e[0] == "service_load"]
        self.assertEqual(estimates, [325, 6676])                                     # the service was given the catalog estimates
        on = {f: voice_settings.enabled(f) for f in voice_loader.FUNCTIONS}
        self.assertEqual(on, {"stt": False, "tts": True, "avatar": True})            # the choice is written to voice.json first
        self.assertIsNone(voice_loader._pending)

    def test_the_model_is_never_loaded_again_without_the_apply_having_run(self):
        self.install("kokoro")
        self.voice.down = False
        self.apply({"tts": {"on": True}})
        self.wait_job()
        self.assertLess(self.kinds().index("service_load"), self.kinds().index("sup_reload"))

    def test_with_no_model_loaded_nothing_is_unloaded_or_reloaded(self):
        self.install("kokoro")
        self.svc.standby = True
        code, out = self.apply({"tts": {"on": True}})
        self.assertEqual(code, 202)
        self.assertFalse(out["plan"]["llm_reload"])
        self.wait_job()
        self.assertEqual(self.kinds(), ["sup_hold", "service_load", "sup_release"])

    def test_a_cpu_function_is_added_without_touching_the_model(self):
        self.install("musetalk-v15")
        self.apply({"avatar": {"on": True, "device": "cpu", "memory": "ram"}})
        self.wait_job()
        self.assertNotIn("llm_unload", self.kinds())
        self.assertNotIn("sup_reload", self.kinds())

    def test_a_function_that_fails_does_not_stop_the_others_or_the_reload(self):
        self.install("kokoro", "musetalk-v15")
        self.voice.fail["tts"] = "the helper died"
        self.apply({"tts": {"on": True}, "avatar": {"on": True}})
        job = self.wait_job()
        self.assertEqual(job["phase"], "done")
        self.assertEqual(job["problems"], ["tts: the helper died"])
        self.assertEqual(job["report"]["avatar"]["state"], "loaded")
        self.assertIn("sup_reload", self.kinds())

    def test_a_service_that_dies_in_the_middle_still_gets_the_model_back(self):
        self.install("kokoro")
        real = self.voice.call

        def dying(method, route, body=None, timeout=3.0):
            if route == "/functions/apply":
                raise voice_service.ServiceDown("the voice service does not answer on port 1 (ConnectionResetError)")
            return real(method, route, body, timeout)
        with mock.patch.object(voice_service, "call", dying):
            self.apply({"tts": {"on": True}})
            job = self.wait_job()
        self.assertEqual(job["phase"], "failed")
        self.assertIn("does not answer", job["error"])
        self.assertIn("sup_reload", self.kinds())                                    # it had been unloaded: it comes back

    def test_a_request_in_progress_stops_the_unload_and_the_apply(self):
        self.install("kokoro")
        self.svc.unload = lambda idle_for=None: "busy"
        self.apply({"tts": {"on": True}})
        job = self.wait_job()
        self.assertEqual(job["phase"], "failed")
        self.assertEqual(job["error_code"], "busy")
        self.assertEqual([c for c in self.voice.calls if c[0] == "POST"], [])
        self.assertNotIn("sup_reload", self.kinds())

    def test_the_apply_needs_confirm_and_refuses_what_does_not_fit(self):
        self.install("musetalk-v15")
        code, body = self.req("POST", "/api/voice/functions/apply", {"desired": {"avatar": {"on": True}}})
        self.assertEqual((code, body["error"]["code"]), (400, "confirm_required"))
        code, body = self.req("POST", "/api/voice/functions/apply", {"desired": {"avatar": {"on": True}}, "confirm": "yes"})
        self.assertEqual(code, 400)
        self.assertEqual(self.log, [])
        self.pool["free"] = 4000
        code, body = self.apply({"avatar": {"on": True}})
        self.assertEqual((code, body["error"]["code"]), (422, "does_not_fit"))
        self.assertEqual(self.log, [])
        self.assertFalse(voice_settings.enabled("avatar"))                           # nothing was written
        self.assertEqual(self.apply({"avatar": {"on": True}}, force=True)[0], 202)
        self.wait_job()

    def test_nothing_to_change_is_said_and_takes_no_slot(self):
        code, body = self.apply({"stt": {"on": False}})
        self.assertEqual((code, body["status"]), (200, "nothing"))
        self.assertEqual(self.log, [])

    def test_a_running_job_or_reload_refuses_a_second_apply(self):
        self.install("kokoro")
        self.assertTrue(self.sup.hold("calibrate", "calibrating"))
        code, body = self.apply({"tts": {"on": True}})
        self.assertEqual((code, body["error"]["code"]), (409, "busy"))
        self.assertNotIn("llm_unload", self.kinds())


class Removing(Base):
    def setUp(self):
        super().setUp()
        self.install("kokoro", "musetalk-v15")
        self.apply({"tts": {"on": True}, "avatar": {"on": True}})
        self.wait_job()
        self.log.clear()

    def test_the_functions_unload_now_and_the_model_is_not_touched(self):
        code, out = self.apply({"avatar": {"on": False}})
        self.assertEqual((code, out["status"]), (202, "started"))
        self.assertFalse(out["plan"]["llm_reload"])
        self.assertTrue(out["plan"]["reload_suggested"])
        job = self.wait_job()
        self.assertEqual(job["phase"], "done")
        self.assertEqual(self.kinds(), ["sup_hold", "service_unload", "sup_release"])           # no llm_unload, no sup_reload
        self.assertEqual(self.pool["free"], 12000 + 5000 - 330 - 6800 + 6800)
        info = self.functions()
        self.assertEqual(info["pending_reload"]["recovered_mib"], 6800)
        self.assertEqual(info["pending_reload"]["removed"], ["avatar"])
        self.assertFalse(voice_settings.enabled("avatar"))
        self.assertTrue(info["llm_running"])

    def test_change_services_in_memory_needs_confirm_then_reloads_and_clears_the_notice(self):
        self.apply({"avatar": {"on": False}})
        self.wait_job()
        self.log.clear()
        code, body = self.req("POST", "/api/voice/functions/reload-llm", {})
        self.assertEqual((code, body["error"]["code"]), (400, "confirm_required"))
        code, body = self.req("POST", "/api/voice/functions/reload-llm", {"confirm": False})
        self.assertEqual(code, 400)
        self.assertEqual(self.log, [])
        self.assertIsNotNone(self.functions()["pending_reload"])
        code, body = self.req("POST", "/api/voice/functions/reload-llm", {"confirm": True})
        self.assertEqual((code, body["status"]), (202, "reloading"))
        self.assertEqual(self.kinds(), ["sup_reload"])
        self.assertEqual(self.log[0][1], False)                                      # an ordinary reload: the Loader plans in all the free VRAM
        self.wait(lambda: self.sup.status()["phase"] == "ready", "the reload did not finish")
        self.assertIsNone(self.functions()["pending_reload"])

    def test_reload_is_refused_while_a_job_runs(self):
        self.assertTrue(self.sup.hold("calibrate", "calibrating"))
        code, body = self.req("POST", "/api/voice/functions/reload-llm", {"confirm": True})
        self.assertEqual((code, body["error"]["code"]), (409, "busy"))

    def test_removing_everything_with_no_model_loaded_has_nothing_to_reload(self):
        self.svc.standby = True
        self.apply({"tts": {"on": False}, "avatar": {"on": False}})
        self.wait_job()
        self.assertIsNone(self.functions()["pending_reload"])


class ServiceDown(Base):
    def test_every_route_that_needs_the_service_says_voice_service_down_and_changes_nothing(self):
        self.install("kokoro")
        self.voice.down = True
        before = (userdata.path("voice.json").read_text(encoding="utf-8") if userdata.path("voice.json").exists() else "")
        for path, body in (("/api/voice/functions/plan", {"desired": {"tts": {"on": True}}}), ("/api/voice/functions/apply", {"desired": {"tts": {"on": True}}, "confirm": True})):
            code, out = self.req("POST", path, body)
            self.assertEqual((code, out["error"]["code"]), (503, "voice_service_down"), path)
            self.assertIn("voice service", out["error"]["message"])
        self.assertEqual(self.log, [])
        self.assertEqual(self.sup.status()["phase"], "idle")
        after = userdata.path("voice.json").read_text(encoding="utf-8") if userdata.path("voice.json").exists() else ""
        self.assertEqual(before, after)

    def test_the_page_can_still_read_the_wanted_state(self):
        self.voice.down = True
        info = self.functions()
        self.assertFalse(info["service"]["running"])
        self.assertIn("voice service", info["service"]["reason"])
        self.assertEqual([f["id"] for f in info["functions"]], ["stt", "tts", "avatar"])
        self.assertIsNotNone(info["vram"]["free_mib"])

    def test_the_loaders_load_goes_on_without_the_service(self):
        voice_settings.set_enabled("tts", True)
        self.install("kokoro")
        self.voice.down = True
        code, body = self.req("POST", "/api/loader/load", {"values": FITS})
        self.assertEqual((code, body["status"]), (202, "reloading"))
        self.wait(lambda: self.sup.status()["phase"] == "ready", "the load did not finish")


class TheLoadersLoad(Base):
    def test_functions_that_are_on_and_not_loaded_load_first_then_the_load_runs_with_the_slot_held(self):
        self.install("kokoro", "musetalk-v15")
        voice_settings.set_enabled("tts", True)
        voice_settings.set_enabled("avatar", True)
        code, body = self.req("POST", "/api/loader/load", {"values": FITS})
        self.assertEqual((code, body["status"], body["notes"]), (202, "voice_first", ["voice_first"]))
        job = self.wait_job()
        self.assertEqual(job["phase"], "done", job)
        order = [e[0] if e[0] != "service_load" else e[:2] for e in self.log]
        self.assertEqual(order, ["sup_hold", "llm_unload", ("service_load", "tts"), ("service_load", "avatar"), "sup_reload"])
        self.assertEqual(self.log[-1][1], True)
        self.assertEqual(self.sup.status()["phase"], "ready")

    def test_nothing_to_load_means_a_plain_load(self):
        code, body = self.req("POST", "/api/loader/load", {"values": FITS})
        self.assertEqual((code, body["status"]), (202, "reloading"))
        self.wait(lambda: self.sup.status()["phase"] == "ready", "the load did not finish")
        self.assertEqual([k for k in self.kinds() if k != "sup_reload"], [])
        self.assertEqual([c for c in self.voice.calls if c[0] == "POST"], [])

    def test_a_standby_server_loads_the_functions_first_and_has_no_model_to_unload(self):
        self.svc.standby = True
        self.install("kokoro")
        voice_settings.set_enabled("tts", True)
        code, body = self.req("POST", "/api/loader/load", {"values": FITS})
        self.assertEqual(body["status"], "voice_first")
        self.wait_job()
        self.assertEqual([k for k in self.kinds()], ["sup_hold", "service_load", "sup_reload"])
        self.assertFalse(self.svc.standby)

    def test_the_load_refused_after_the_functions_loaded_is_shown_with_the_job(self):
        self.svc.standby = True
        self.install("kokoro")
        voice_settings.set_enabled("tts", True)
        code, body = self.req("POST", "/api/loader/load", {"values": {"engine.expert-cache": 999999}})
        self.assertEqual((code, body["status"]), (202, "voice_first"))
        job = self.wait_job()
        self.assertEqual((job["phase"], self.log[-1]), ("failed", ("sup_release", None)))
        self.assertIn("service_load", self.kinds())                                  # the functions did load; the load itself was then refused
        self.assertEqual(self.sup.status()["phase"], "idle")                         # the slot is free again

    def test_another_model_ends_the_server_and_never_reaches_the_service(self):
        self.svc.relaunch_on_model_change = True
        self.install("kokoro", "musetalk-v15")
        voice_settings.set_enabled("tts", True)
        self.apply({"tts": {"on": True}})
        self.wait_job()
        voice_settings.set_enabled("avatar", True)                                     # a function waiting to load
        self.voice.calls.clear()
        self.log.clear()
        code, body = self.req("POST", "/api/loader/load", {"values": {"engine.kv": "q4_0", **FITS}, "model": str(self.b)})
        self.assertEqual((code, body["status"], body["notes"]), (202, "restarting", ["model_restart"]))
        self.wait(self.svc.exit_request.is_set, "the server was not asked to end")
        self.assertEqual(self.voice.calls, [])                                         # not asked, not unloaded, not loaded
        self.assertEqual(self.log, [])
        self.assertEqual(self.voice.state["tts"]["state"], "loaded")                   # still loaded for the next start


class TheCall(unittest.TestCase):
    """voice_service.call over a real loopback socket: the token, the body and the refusals."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TADAMOO_DATA": self.dir.name})
        self.env.start()
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.append((self.command, self.path, self.headers.get("Authorization"), self.rfile.read(n) if n else b""))
                code, body = (401, b"{}") if self.headers.get("Authorization") != "Bearer " + "b" * 64 else (200, b'{"ok": true}')
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = _go

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.stop()
        self.dir.cleanup()

    def test_it_sends_the_token_and_the_json_body_to_loopback(self):
        with self.assertRaises(voice_service.ServiceDown):
            voice_service.call("GET", "/functions")                                      # no info file
        userdata.path("voice-service.json").write_text(json.dumps({"host": "127.0.0.1", "port": self.httpd.server_address[1], "pid": 1}), encoding="utf-8")
        with self.assertRaises(voice_service.ServiceDown):
            voice_service.call("GET", "/functions")                                      # no token
        userdata.path("voice-service.token").write_text("b" * 64, encoding="utf-8")
        self.assertEqual(voice_service.call("POST", "/functions/apply", {"functions": {}}), (200, {"ok": True}))
        self.assertEqual(self.seen[-1], ("POST", "/functions/apply", "Bearer " + "b" * 64, b'{"functions": {}}'))
        userdata.path("voice-service.token").write_text("c" * 64, encoding="utf-8")
        with self.assertRaises(voice_service.ServiceDown):
            voice_service.call("GET", "/functions")                                      # the service refused the token
        userdata.path("voice-service.json").write_text(json.dumps({"host": "10.0.0.5", "port": 1, "pid": 1}), encoding="utf-8")
        with self.assertRaises(voice_service.ServiceDown):
            voice_service.call("GET", "/functions")                                      # never a host that is not this PC


if __name__ == "__main__":
    unittest.main()
