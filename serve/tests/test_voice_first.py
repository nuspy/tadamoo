"""The voice functions load before the model, by whatever path the model's engine starts, and a Load that would fail for the Windows commit is
reported before anything loads. A function added while the model runs borrows its VRAM instead of unloading the model.

Same stand-ins as test_voice_loader.py (a real server over HTTP, a mock engine, a voice service behind `voice_service.call`); no model is
loaded, no GPU is read."""
from __future__ import annotations

import io
import contextlib
import unittest
from unittest import mock

from serve import loader, profiles, settings, supervisor, voice_lend, voice_loader, voice_service, voice_settings
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.service import Service
from serve.tests.test_models import ROOMY
from serve.tests.test_voice_avatar_ensure import START_RESERVE, Lender
from serve.tests.test_voice_loader import FITS, MIB, ROOT, Base
from serve.util.text import ByteTokenizer
from serve.vram_broker import BrokerPrefs, VramBroker

GiB = 1 << 30


def posts(base):
    return [c for c in base.voice.calls if c[0] == "POST"]


class Commit(Base):
    """The commit of this PC as the Loader reads it, in bytes: `used` plus what each function that is loaded takes."""

    def setUp(self):
        super().setUp()
        self.commit = {"limit": 144 * GiB, "used": 10 * GiB, "per_load": {"tts": 600, "stt": 1200}}

        def now(gpu=0):
            used = self.commit["used"] + sum(self.commit["per_load"].get(f, 0) * MIB for f, s in self.voice.state.items() if s["state"] == "loaded")
            return {**ROOMY, "vram_total": 24000 * MIB, "vram_free": self.pool["free"] * MIB, "commit_limit": self.commit["limit"], "commit_used": used}
        p = mock.patch.object(loader, "system_now", now)
        self.patches.insert(0, p)                                                  # stopped before the base class's own patch, which it covers
        p.start()


class Estimate(Base):
    def test_the_commit_is_the_measured_figure_else_twice_the_vram_else_nothing(self):
        self.assertEqual(voice_loader.commit_estimate_mib("kokoro", "auto", "vram"), (394, "rule"))             # the catalogue's 197 MiB, doubled
        self.assertEqual(voice_loader.commit_estimate_mib("kokoro", "cpu", "ram"), (0, "cpu"))
        self.assertEqual(voice_loader.commit_estimate_mib("ultralight-wenet-encoder", "auto", "vram"), (None, "unknown"))
        voice_loader.record_measured("kokoro", "auto", "vram", 330)
        self.assertEqual(voice_loader.commit_estimate_mib("kokoro", "auto", "vram"), (660, "rule"))             # a VRAM measurement alone is still the rule
        voice_loader.record_commit("kokoro", "auto", "vram", 910)
        self.assertEqual(voice_loader.commit_estimate_mib("kokoro", "auto", "vram"), (910, "measured"))
        self.assertEqual(voice_loader.commit_estimate_mib("kokoro", "cuda:1", "vram")[1], "rule")                # measured for another placement
        self.assertEqual(voice_loader.estimate_mib("kokoro", "auto", "vram"), (330, "measured"))                 # the VRAM figure is untouched

    def test_a_commit_is_kept_only_beside_a_vram_measurement_of_the_same_placement(self):
        voice_loader.record_commit("kokoro", "auto", "vram", 910)
        self.assertEqual(voice_loader.measured(), {})
        voice_loader.record_measured("kokoro", "auto", "vram", 330)
        voice_loader.record_commit("kokoro", "cuda:1", "vram", 910)
        self.assertNotIn("commit_mib", voice_loader.measured()["kokoro"])
        voice_loader.record_measured("kokoro", "auto", "vram", 340)                                               # a new measurement drops the old commit
        voice_loader.record_commit("kokoro", "auto", "vram", 900)
        voice_loader.record_measured("kokoro", "auto", "vram", 350)
        self.assertNotIn("commit_mib", voice_loader.measured()["kokoro"])


class CommitPlan(Commit):
    def test_the_plan_says_what_the_functions_will_take_and_whether_the_commit_holds_it(self):
        self.install("kokoro", "whisper-small")
        code, p = self.req("POST", "/api/voice/functions/plan", {"desired": {"tts": {"on": True}, "stt": {"on": True}}})
        self.assertEqual(code, 200, p)
        tts = next(i for i in p["items"] if i["id"] == "tts")
        self.assertEqual((tts["commit_mib"], tts["commit_source"], tts["commit_ok"]), (394, "rule", True))
        self.assertEqual((p["commit_need_mib"], p["commit_ok"], p["commit_reason"]), (394 + 924, True, ""))

    def test_a_commit_that_cannot_hold_them_is_reported_in_both_languages_with_the_numbers(self):
        self.install("kokoro")
        self.commit["used"] = 144 * GiB - 2 * GiB                                  # 2 GiB left; 15% of the limit must stay free
        code, p = self.req("POST", "/api/voice/functions/plan", {"desired": {"tts": {"on": True}}})
        tts = next(i for i in p["items"] if i["id"] == "tts")
        self.assertEqual((p["commit_ok"], tts["commit_ok"]), (False, False))
        self.assertIn("Not enough commit", p["commit_reason"])
        self.assertIn("Manca memoria impegnabile (commit)", p["commit_reason_it"])
        self.assertIn("1.6 GiB left", p["commit_reason"])               # the 2 GiB, less what the functions take

    def test_apply_refuses_what_the_commit_cannot_hold_unless_forced(self):
        self.install("kokoro")
        self.commit["used"] = 144 * GiB - 2 * GiB
        code, out = self.apply({"tts": {"on": True}})
        self.assertEqual((code, out["error"]["code"]), (422, "not_enough_commit"))
        self.assertTrue(out["error"]["message"] and out["error"]["message_it"])
        self.assertEqual(posts(self), [])
        code, out = self.apply({"tts": {"on": True}}, force=True)
        self.assertEqual(code, 202, out)

    def test_nothing_to_load_asks_nothing_of_the_commit(self):
        code, p = self.req("POST", "/api/voice/functions/plan", {})
        self.assertEqual((p["commit_need_mib"], p["commit_ok"]), (0, True))

    def test_the_commit_that_rose_with_a_single_load_is_recorded(self):
        self.install("kokoro")
        code, out = self.apply({"tts": {"on": True}})
        self.wait_job()
        saved = voice_loader.measured()["kokoro"]
        self.assertEqual((saved["mib"], saved["commit_mib"]), (330, 600))

    def test_a_commit_that_rose_with_two_loads_is_not_shared_out_by_guessing(self):
        self.install("kokoro", "whisper-small")
        self.apply({"tts": {"on": True}, "stt": {"on": True}})
        self.wait_job()
        self.assertTrue(all("commit_mib" not in v for v in voice_loader.measured().values()))


class LoadersCommit(Commit):
    def setUp(self):
        super().setUp()
        self.svc.standby = True
        self.install("kokoro", "whisper-small")
        voice_settings.set_enabled("tts", True)
        voice_settings.set_enabled("stt", True)

    def tight(self):
        """Room for the model alone, with half a GiB over: not for the model and the functions."""
        with mock.patch.object(voice_loader, "pending_commit", lambda svc: 0):
            c = self.req("POST", "/api/loader/plan", {"values": FITS})[1]["commit"]
        self.commit["used"] = self.commit["limit"] - c["need"] - c["keep"] - 512 * MIB

    def test_the_loaders_plan_counts_the_functions_about_to_load(self):
        code, p = self.req("POST", "/api/loader/plan", {"values": FITS})
        self.assertEqual(code, 200, p)
        self.assertEqual(p["voice_commit"], (394 + 924) * MIB)
        self.assertFalse([x for x in p["problems"] if x["code"] == "commit"])
        self.tight()
        code, tight = self.req("POST", "/api/loader/plan", {"values": FITS})
        bad = next(x for x in tight["problems"] if x["code"] == "commit")
        self.assertEqual(bad["voice"], (394 + 924) * MIB)
        self.assertIn("voice functions that load first take about", bad["text"])
        self.assertIn("Le funzioni vocali che si caricano prima", bad["text_it"])

    def test_a_load_that_would_fail_for_commit_is_refused_before_any_function_loads(self):
        self.tight()
        with mock.patch.object(voice_loader, "pending_commit", lambda svc: 0):
            alone = self.req("POST", "/api/loader/plan", {"values": FITS})[1]
        self.assertFalse([x for x in alone["problems"] if x["code"] == "commit"], alone["problems"])     # the model alone fits
        code, out = self.req("POST", "/api/loader/load", {"values": FITS})
        self.assertEqual((code, out["error"]["code"]), (422, "does_not_fit"))
        self.assertIn("Not enough commit", out["error"]["message"])
        self.assertEqual([p["code"] for p in out["problems"]], ["commit"])
        self.assertTrue(out["problems"][0]["text_it"])
        self.assertEqual(posts(self), [])                                          # nothing was loaded, not even half
        self.assertEqual(self.log, [])
        self.assertEqual(self.sup.status()["phase"], "idle")

    def test_force_loads_anyway(self):
        self.tight()
        code, out = self.req("POST", "/api/loader/load", {"values": FITS, "force": True})
        self.assertEqual((code, out["status"]), (202, "voice_first"))
        self.wait_job()

    def test_a_load_with_room_goes_on_as_before(self):
        code, out = self.req("POST", "/api/loader/load", {"values": FITS})
        self.assertEqual((code, out["status"]), (202, "voice_first"))
        self.wait_job()
        self.assertEqual(sorted(e[1] for e in self.log if e[0] == "service_load"), ["stt", "tts"])


class Autoload(Base):
    """POST /api/loader/autoload: the Machine Default, the voice functions first and then the model."""

    def setUp(self):
        super().setUp()
        self.install("kokoro", "whisper-small")
        self.svc.standby = True
        self.save({"stt": {"on": False, "model": "whisper-small", "device": "auto", "memory": "vram"},
                   "tts": {"on": True, "model": "kokoro", "device": "auto", "memory": "vram"}})

    def save(self, voice, name=profiles.MACHINE_NAME):
        profiles.change({"action": "save", "name": name, "values": FITS, "voice": voice})

    def test_the_machine_default_loads_its_voice_functions_first_and_then_the_model(self):
        self.assertFalse(voice_settings.enabled("tts"))
        code, out = self.req("POST", "/api/loader/autoload", {})
        self.assertEqual((code, out["status"], out["profile"]), (202, "voice_first", "Machine Default"))
        self.wait_job()
        self.assertTrue(voice_settings.enabled("tts"))                                 # the profile's voice choices were written
        self.assertFalse(voice_settings.enabled("stt"))
        self.assertEqual([k for k in self.kinds()], ["sup_hold", "service_load", "sup_reload"])
        self.assertEqual(self.log[1][1], "tts")
        self.assertFalse(self.svc.standby)

    def test_without_a_machine_default_the_default_profile_is_used(self):
        profiles.change({"action": "delete", "name": profiles.MACHINE_NAME})
        self.save({"tts": {"on": False, "model": "kokoro", "device": "auto", "memory": "vram"},
                   "stt": {"on": False, "model": "whisper-small", "device": "auto", "memory": "vram"}}, "Mine")
        profiles.change({"action": "default", "name": "Mine"})
        code, out = self.req("POST", "/api/loader/autoload", {})
        self.assertEqual((code, out["profile"]), (202, "Mine"))
        self.wait(lambda: self.sup.status()["phase"] == "ready", "the load did not finish")

    def test_it_is_this_pcs_only(self):
        with mock.patch.object(settings, "is_loopback", return_value=False):
            code, out = self.req("POST", "/api/loader/autoload", {})
        self.assertEqual(code, 403)
        self.assertEqual(self.log, [])

    def test_a_busy_slot_is_refused_with_busy(self):
        self.svc.standby = False
        self.assertTrue(self.sup.hold("calibrate", "calibrating"))
        code, out = self.req("POST", "/api/loader/autoload", {})
        self.assertEqual((code, out["error"]["code"]), (409, "busy"))


class First(Base):
    """`ensure_first`: the one place of the rule that the functions load before the model."""

    def setUp(self):
        super().setUp()
        self.install("kokoro", "whisper-small")
        voice_settings.set_enabled("tts", True)
        voice_settings.set_enabled("stt", True)
        self.out = io.StringIO()

    def first(self, svc=None):
        with contextlib.redirect_stdout(self.out):
            voice_loader.ensure_first(svc or self.svc, sleep=lambda s: None)

    def loaded(self):
        return sorted(e[1] for e in self.log if e[0] == "service_load")

    def test_functions_that_are_on_and_not_loaded_load_and_the_call_waits_for_them(self):
        self.first()
        self.assertEqual(self.loaded(), ["stt", "tts"])
        self.assertEqual(len(posts(self)), 1)
        self.assertEqual(self.voice.state["tts"]["state"], "loaded")
        self.assertEqual(voice_loader.measured()["kokoro"]["mib"], 330)             # what it measured is kept, as with the Loader's own apply

    def test_it_is_idempotent_and_does_nothing_when_nothing_is_to_load(self):
        self.first()
        before = len(posts(self))
        self.first()
        self.first()
        self.assertEqual(len(posts(self)), before)
        voice_settings.set_enabled("tts", False)
        voice_settings.set_enabled("stt", False)
        self.voice.state["tts"]["state"] = self.voice.state["stt"]["state"] = "unloaded"
        self.first()
        self.assertEqual(len(posts(self)), before)

    def test_a_voice_service_that_is_down_is_a_quiet_return(self):
        self.voice.down = True
        self.first()
        self.assertEqual((self.log, self.out.getvalue()), ([], ""))

    def test_a_function_that_fails_does_not_stop_the_others_or_the_start(self):
        self.voice.fail["tts"] = "the engine did not warm up"
        self.first()
        self.assertEqual(self.loaded(), ["stt"])
        self.assertIn("voice function tts did not load: the engine did not warm up", self.out.getvalue())
        self.voice.state["tts"]["state"] = "error"                                    # as the service reports a function that failed
        posts_before = len(posts(self))
        self.first()                                                                  # a function that failed is not tried again by itself
        self.assertEqual(len(posts(self)), posts_before)

    def test_an_error_of_any_kind_is_one_line_and_never_raises(self):
        with mock.patch.object(voice_service, "call", side_effect=RuntimeError("boom")):
            self.first()
        self.assertIn("the voice functions were not loaded before the model (boom)", self.out.getvalue())
        self.assertEqual(self.out.getvalue().count("\n"), 1)

    def test_an_on_request_avatar_is_not_loaded(self):
        self.install("musetalk-v15")
        voice_settings.set_enabled("avatar", True)
        voice_settings.set_load_mode("avatar", "on_request")
        self.first()
        self.assertNotIn("avatar", self.loaded())
        self.assertEqual(self.voice.state["avatar"]["state"], "unloaded")

    def test_it_waits_while_a_function_is_loading_and_then_goes_on(self):
        self.voice.state["tts"]["state"] = "loading"
        waits = []

        def sleep(s):
            waits.append(s)
            self.voice.state["tts"].update(state="loaded", loaded_with={"model": "kokoro", "device": "auto", "memory": "vram"})
        with contextlib.redirect_stdout(self.out):
            voice_loader.ensure_first(self.svc, sleep=sleep)
        self.assertEqual(waits, [1.0])
        self.assertEqual(self.loaded(), ["stt"])

    def test_a_commit_that_cannot_hold_the_functions_and_the_model_skips_the_functions_with_one_line(self):
        p = mock.patch.object(loader, "system_now", lambda gpu=0: {**ROOMY, "commit_limit": 40 * GiB, "commit_used": 10 * GiB,
                                                                   "vram_total": 24000 * MIB, "vram_free": self.pool["free"] * MIB})
        self.patches.insert(0, p)
        p.start()
        self.first()
        self.assertEqual(posts(self), [])
        self.assertIn("not loaded before the model", self.out.getvalue())

    def test_a_server_whose_engine_starts_before_its_service_exists_does_it_too(self):
        facts = loader.model_facts(str(self.a))
        boot = voice_loader.Booting({"config": str(self.cfg), "model": str(self.a), "cli": [], "running": {}}, facts)
        self.first(boot)
        self.assertEqual(self.loaded(), ["stt", "tts"])


class Restartable(MockEngine):
    """A mock engine that is a process: it is down until something starts it."""
    up = False

    def alive(self):
        return self.up

    def restart(self, tries=1):
        self.up = True


class EveryStartPath(Base):
    def setUp(self):
        super().setUp()
        self.calls = []
        p = mock.patch.object(voice_loader, "ensure_first", lambda svc: self.calls.append(svc))
        p.start()
        self.addCleanup(p.stop)

    def test_the_first_request_of_a_lazy_server_calls_it_once_before_the_engine_starts(self):
        tok = ByteTokenizer()
        engine = Restartable(tok, ["x"])
        svc = Service(engine, tok, ChatTemplate.from_file(ROOT / "serve/chat_template.jinja"))
        order = []
        self.calls.clear()
        engine.restart = lambda tries=1: (order.append("engine"), setattr(engine, "up", True))
        with mock.patch.object(voice_loader, "ensure_first", lambda s: order.append("voice")):
            svc.load()
            svc.load()                                                              # already running: not again
        self.assertEqual(order, ["voice", "engine"])

    def test_a_mock_engine_leaves_the_voice_service_alone(self):
        self.svc.standby = False
        self.svc.engine.unloaded = True
        self.svc._ensure_loaded()
        self.assertEqual(self.calls, [])

    def test_a_reload_with_the_loaders_settings_calls_it_after_the_engine_ended_and_before_it_starts(self):
        order = []

        class Eng(Restartable):
            spawn = ("bin", [], None, None, None)
            max_context = 1

            def close(self):
                order.append("close")

            def restart(self, tries=1):
                order.append("start")
        svc = mock.Mock(spec=["engine", "vision", "gpu_index", "gpu_indices"])
        svc.engine, svc.vision, svc.gpu_index = Eng.__new__(Eng), None, 0
        with mock.patch.object(supervisor, "voice_first", lambda s: order.append("voice")), \
                mock.patch.object(supervisor, "engine_config", lambda a, w: {"engine_bin": "b", "log": None}), \
                mock.patch.object(supervisor, "sampling_defaults_of", lambda a, c: {}), \
                mock.patch.object(supervisor, "child_env", lambda c, s: {}), \
                mock.patch.object(supervisor, "engine_args", lambda c: []), \
                mock.patch.object(supervisor, "grammar_words", lambda f: []), \
                mock.patch.object(supervisor, "engine_flags_of", lambda b: None), \
                mock.patch.object(supervisor, "record_memory", lambda *a: None), \
                mock.patch.object(supervisor, "settings_info", lambda a, w, argv: {"running": {}}), \
                mock.patch.object(supervisor, "make_vision", lambda c, e: None), \
                mock.patch.object(supervisor, "gpus_in_play", lambda c, a: [0]):
            sup = supervisor.Supervisor(svc, [], mock=False, facts={})
            sup._reload_engine(mock.Mock(sampling_cli=set(), sampling_launch={}), [])
        self.assertEqual(order, ["close", "voice", "start"])


class Lending(Base):
    """A function added while the model runs borrows its VRAM when the engine lends; else the model is unloaded and loaded again."""

    def setUp(self):
        super().setUp()
        voice_settings.set_enabled("avatar", False)
        voice_settings.set_enabled("stt", False)
        self.install("kokoro", "whisper-small")
        self.pool["free"] = START_RESERVE
        self.lender = Lender(self.pool)
        self.broker = VramBroker(self.lender, probe=lambda: self.pool["free"], start_reserve=lambda: START_RESERVE, prefs=BrokerPrefs(None), timer=False)
        self.svc.vram_broker = self.broker
        self.lend = self.svc.voice_lend = voice_lend.Lent(self.svc, self.broker, timer=False)
        self.addCleanup(self.lend.shutdown)
        self.svc.settings_info["running"] = {"engine.vram-elastic": True}

    def test_an_added_function_borrows_the_vram_and_the_model_is_not_touched(self):
        code, out = self.apply({"tts": {"on": True}})
        self.assertEqual((code, out["lent"]), (202, {"used": True, "refused": None}))
        self.wait_job()
        self.assertEqual(self.voice.state["tts"]["state"], "loaded")
        self.assertNotIn("llm_unload", self.kinds())
        self.assertNotIn("sup_reload", self.kinds())
        self.assertEqual(self.lender.calls, [START_RESERVE + 197])
        lease = self.broker.state()["leases"][0]
        self.assertEqual((lease["owner"], lease["mib"]), ("voice", 197))
        self.assertTrue(self.lend.holds("tts"))
        self.assertIsNone(voice_loader._pending)

    def test_a_second_function_makes_the_one_lease_larger(self):
        self.apply({"tts": {"on": True}})
        self.wait_job()
        self.apply({"stt": {"on": True}})
        self.wait_job()
        leases = self.broker.state()["leases"]
        self.assertEqual((len(leases), leases[0]["mib"]), (1, 197 + 462))
        self.assertEqual(self.lend.sizes(), {"tts": 197, "stt": 462})
        self.assertNotIn("llm_unload", self.kinds())

    def test_removing_a_borrowed_function_gives_its_share_back_and_there_is_nothing_to_reload(self):
        self.apply({"tts": {"on": True}, "stt": {"on": True}})
        self.wait_job()
        self.apply({"tts": {"on": False}})
        self.wait_job()
        self.assertEqual(self.lend.sizes(), {"stt": 462})
        self.assertEqual(self.broker.state()["leases"][0]["mib"], 462)
        self.assertIsNone(voice_loader._pending)                                       # the cache refills by itself
        self.apply({"stt": {"on": False}})
        self.wait_job()
        self.assertEqual((self.broker.state()["leases"], self.lender.calls[-1]), ([], None))
        self.assertEqual(self.pool["free"], START_RESERVE)

    def test_a_function_that_does_not_load_keeps_no_share(self):
        self.voice.fail["tts"] = "no warm-up"
        self.apply({"tts": {"on": True}})
        job = self.wait_job()
        self.assertTrue(job["problems"])
        self.assertEqual((self.lend.sizes(), self.broker.state()["leases"]), ({}, []))

    def test_a_refusal_of_the_broker_falls_back_to_unloading_the_model(self):
        self.lender.elastic = False
        code, out = self.apply({"tts": {"on": True}}, force=True)
        self.assertEqual((code, out["lent"]), (202, {"used": False, "refused": "unavailable"}))
        self.wait_job()
        self.assertIn("llm_unload", self.kinds())
        self.assertEqual(self.lend.sizes(), {})

    def test_a_commit_with_no_room_for_the_function_is_named_as_the_reason_the_lend_was_not_used(self):
        def commit_check(svc, need, with_model):
            return {"ok": False, "reason": ("reload figures" if with_model else "the function alone"),
                    "reason_it": ("cifre della ricarica" if with_model else "la funzione da sola")}
        self.pool["free"] = 12000                                                  # the reload would fit the VRAM: only the commit refuses
        with mock.patch.object(voice_loader, "commit_check", commit_check):
            code, out = self.apply({"tts": {"on": True}})
        self.assertEqual((code, out["error"]["code"], out["error"]["lend"]), (422, "not_enough_commit", "commit"))
        self.assertIn("the function alone", out["error"]["message"])
        self.assertNotIn("reload figures", out["error"]["message"])
        self.assertIn("la funzione da sola", out["error"]["message_it"])
        self.assertEqual(self.lender.calls, [])                                    # nothing was lent and the model was not touched
        self.assertNotIn("llm_unload", self.kinds())

    def test_an_engine_that_does_not_lend_keeps_todays_behaviour(self):
        self.svc.settings_info["running"] = {"engine.vram-elastic": False}        # lending is on by default: this engine was started without it
        self.pool["free"] = 12000
        code, out = self.apply({"tts": {"on": True}})
        self.assertEqual(out["lent"], {"used": False, "refused": None})
        self.wait_job()
        self.assertIn("llm_unload", self.kinds())
        self.assertEqual(self.lender.calls, [])

    def test_a_busy_job_gives_the_lease_back(self):
        self.assertTrue(self.sup.hold("calibrate", "calibrating"))
        code, out = self.apply({"tts": {"on": True}})
        self.assertEqual(code, 409)
        self.assertEqual((self.lend.sizes(), self.broker.state()["leases"]), ({}, []))

    def test_the_shared_lease_is_kept_alive_and_lost_when_it_is_released_elsewhere(self):
        self.apply({"tts": {"on": True}})
        self.wait_job()
        self.assertTrue(self.lend.touch()["ok"])
        self.broker.release(self.broker.state()["leases"][0]["id"])
        self.assertFalse(self.lend.touch()["ok"])


if __name__ == "__main__":
    unittest.main()
