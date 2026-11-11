"""The avatar on request: the Loader's Load leaves it out, `POST /api/voice/avatar/ensure` borrows its VRAM from the broker and loads only the
avatar (the model stays as it is), an idle avatar is unloaded and the VRAM given back, and every refusal leaves nothing half loaded.

Same stand-ins as test_voice_loader.py (a real server over HTTP, a mock engine, a voice service behind `voice_service.call`), plus an engine
that lends VRAM to the broker; no model is loaded, no GPU is read."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from serve import settings, voice_avatar_ensure, voice_catalog, voice_loader, voice_service, voice_settings
from serve.tests import test_voice_loader
from serve.tests.test_voice_loader import Base
from serve.vram_broker import BrokerPrefs, VramBroker

START_RESERVE = 1200
AVATAR_MIB = 4276                       # the catalogue's size of the avatar model


class Lender:
    """The model's engine as the broker sees it: keeps `reserve` MiB free, so asking for more reserve frees VRAM in the shared pool."""

    def __init__(self, pool, elastic=True):
        self.pool, self.elastic, self.lent, self.calls = pool, elastic, 0, []
        self.vram_reserve = None
        self.loading = False

    def loaded(self):
        return True

    def vram(self, reserve):
        self.calls.append(reserve)
        if not self.elastic:
            raise ValueError("VRAM needs an engine started with --vram-elastic (one NVIDIA GPU, --serve)")
        extra = (START_RESERVE if reserve is None else reserve) - START_RESERVE
        self.pool["free"] += extra - self.lent
        self.lent, self.vram_reserve = extra, reserve
        return {"status": "ok", "reserve_mib": reserve, "vram_free_mib": self.pool["free"], "lent_mib": extra}


class FakeVoiceWithState:
    """The service's extra routes the watcher asks."""

    def __init__(self, voice):
        self.voice, self.clients, self.viewers, self.session = voice, 0, 0, False

    def call(self, method, route, body=None, timeout=3.0):
        if route == "/gateway":
            return 200, {"clients": self.clients}
        if route == "/avatar/state":
            return 200, {"outputs": {"preview": {"viewers": self.viewers}, "camera": {"running": False}}}
        if route == "/session/state":
            return 200, {"active": self.session, "avatar": self.session}
        return self.voice.call(method, route, body, timeout)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Ensure(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(test_voice_loader.COSTS, {"musetalk-v15": AVATAR_MIB})        # the fake service takes what the catalogue says
        p.start()
        self.addCleanup(p.stop)
        voice_settings.set_load_mode("avatar", "on_request")
        voice_settings.set_enabled("avatar", True)
        self.install("musetalk-v15")
        self.pool["free"] = START_RESERVE                                  # the engine keeps its own reserve free
        self.lender = Lender(self.pool)
        self.broker = VramBroker(self.lender, probe=lambda: self.pool["free"], start_reserve=lambda: START_RESERVE,
                                 prefs=BrokerPrefs(None), timer=False)
        self.svc.vram_broker = self.broker
        self.state = FakeVoiceWithState(self.voice)
        self.clock = Clock()
        self.watch = voice_avatar_ensure.OnRequest(self.svc, self.broker, clock=self.clock, tick_s=3600)
        self.svc.avatar_on_request = self.watch
        self.addCleanup(self.watch.shutdown)
        p = mock.patch.object(voice_service, "call", self.state.call)
        p.start()
        self.addCleanup(p.stop)

    def ensure(self, body=None):
        return self.req("POST", "/api/voice/avatar/ensure", body or {})

    def avatar_state(self):
        return self.voice.state["avatar"]["state"]


class Loading(Ensure):
    def test_the_loaders_load_leaves_a_request_avatar_out(self):
        code, p = self.req("POST", "/api/voice/functions/plan", {})
        item = next(i for i in p["items"] if i["id"] == "avatar")
        self.assertEqual((item["action"], item["load"], p["need_mib"], p["changes"]), ("on_request", "on_request", 0, False))
        info = self.functions()
        self.assertEqual(next(r for r in info["functions"] if r["id"] == "avatar")["action"], "on_request")
        self.assertEqual(info["set_need_mib"], 0)
        self.assertIsNone(voice_loader.before_load(self.svc, {}))
        self.assertEqual(self.log, [])

    def test_an_at_load_avatar_is_planned_as_before(self):
        voice_settings.set_load_mode("avatar", "at_load")
        code, p = self.req("POST", "/api/voice/functions/plan", {})
        self.assertEqual(next(i["action"] for i in p["items"] if i["id"] == "avatar"), "load")

    def test_ensure_lends_the_vram_loads_only_the_avatar_and_keeps_the_model(self):
        code, out = self.ensure()
        self.assertEqual((code, out["ok"], out["state"], out["lease"]), (200, True, "loaded", True), out)
        self.assertEqual(out["lent_mib"], AVATAR_MIB)                                # the avatar's size, from the catalogue
        self.assertEqual(self.lender.calls, [START_RESERVE + AVATAR_MIB])
        self.assertEqual(self.avatar_state(), "loaded")
        self.assertNotIn("llm_unload", self.kinds())
        self.assertNotIn("sup_reload", self.kinds())                           # the model was never unloaded or loaded again
        self.assertEqual([e[1] for e in self.log if e[0] == "service_load"], ["avatar"])
        self.assertEqual(self.broker.state()["leases"][0]["owner"], "avatar")
        self.assertTrue(voice_settings.enabled("avatar"))
        self.assertEqual(self.pool["free"], START_RESERVE)
        self.assertIsNone(voice_loader._pending)                               # no "reload the model" banner

    def test_a_second_ensure_only_says_it_is_loaded(self):
        self.ensure()
        loads = len(self.log)
        code, out = self.ensure()
        self.assertEqual((code, out["state"]), (200, "loaded"))
        self.assertEqual(len(self.log), loads)
        self.assertEqual(len(self.broker.state()["leases"]), 1)

    def test_a_measured_size_is_the_size_asked_for(self):
        voice_loader.record_measured("musetalk-v15", "auto", "vram", 5100)
        self.assertEqual(self.ensure()[1]["lent_mib"], 5100)

    def test_a_cpu_avatar_borrows_nothing(self):
        voice_settings.set_placement("avatar", "cpu", "ram")
        code, out = self.ensure()
        self.assertEqual((code, out["ok"], out["lease"]), (200, True, False))
        self.assertEqual((self.lender.calls, self.broker.state()["leases"]), ([], []))

    def test_at_load_mode_loads_without_a_lease(self):
        voice_settings.set_load_mode("avatar", "at_load")
        code, out = self.ensure()
        self.assertEqual((code, out["ok"], out["lease"]), (200, True, False))
        self.assertEqual(self.lender.calls, [])


class Refusals(Ensure):
    def assert_nothing_loaded(self):
        self.assertEqual(self.avatar_state(), "unloaded")
        self.assertEqual([c for c in self.voice.calls if c[0] == "POST"], [])
        self.assertEqual(self.broker.state()["leases"], [])

    def test_an_engine_that_cannot_lend_says_so_in_both_languages(self):
        self.lender.elastic = False
        code, out = self.ensure()
        self.assertEqual((code, out["ok"], out["reason"]), (409, False, "unavailable"))
        self.assertTrue(out["message"] and out["message_it"])
        self.assert_nothing_loaded()

    def test_another_job_holding_the_vram_is_busy(self):
        self.broker.acquire("docling", 3000, "pdf")
        code, out = self.ensure()
        self.assertEqual((code, out["reason"]), (409, "busy"))
        self.assertEqual(self.avatar_state(), "unloaded")
        self.assertEqual([c for c in self.voice.calls if c[0] == "POST"], [])

    def test_not_enough_vram_even_when_lent(self):
        self.broker.service.vram = lambda r: {"status": "ok"}                  # the engine could lend nothing
        code, out = self.ensure()
        self.assertEqual((code, out["reason"]), (409, "not_enough"))
        self.assert_nothing_loaded()

    def test_a_load_that_fails_gives_the_vram_back(self):
        self.voice.fail["avatar"] = "the worker did not start"
        code, out = self.ensure()
        self.assertEqual((code, out["reason"]), (502, "load_failed"))
        self.assertIn("the worker did not start", out["message"])
        self.assertEqual(self.broker.state()["leases"], [])
        self.assertEqual(self.pool["free"], START_RESERVE)
        self.assertEqual(self.lender.calls[-1], None)

    def test_an_avatar_turned_off_is_not_loaded_by_asking(self):
        voice_settings.set_enabled("avatar", False)
        code, out = self.ensure()
        self.assertEqual((code, out["reason"]), (409, "avatar_off"))
        self.assert_nothing_loaded()

    def test_an_avatar_that_is_not_installed(self):
        self.voice.installed.discard("musetalk-v15")
        for marker in self.models.rglob(voice_catalog.MARKER):
            marker.unlink()
        code, out = self.ensure()
        self.assertEqual((code, out["reason"]), (409, "not_installed"))
        self.assertEqual(self.lender.calls, [])

    def test_a_voice_service_that_is_down(self):
        self.voice.down = True
        code, out = self.ensure()
        self.assertEqual((code, out["reason"]), (503, "voice_service_down"))
        self.assertEqual(self.lender.calls, [])

    def test_only_this_pc_or_a_phone_with_its_key(self):
        with mock.patch.object(settings, "is_loopback", return_value=False):
            self.assertEqual(self.ensure()[0], 403)
        self.assertEqual(self.ensure({"wait_s": "x"})[0], 400)
        self.assertEqual(self.lender.calls, [])


class Idle(Ensure):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.ensure()[0], 200)
        self.idle = voice_settings.AVATAR_IDLE_S[2]

    def test_an_unused_avatar_is_unloaded_after_the_idle_time_and_the_vram_returns(self):
        self.clock.t += self.idle - 1
        self.assertFalse(self.watch.step())
        self.assertEqual(self.avatar_state(), "loaded")
        self.clock.t += 2
        self.assertTrue(self.watch.step())
        self.assertEqual(self.avatar_state(), "unloaded")
        self.assertEqual(self.broker.state()["leases"], [])
        self.assertEqual(self.lender.calls[-1], None)
        self.assertEqual(self.pool["free"], START_RESERVE)                     # the cache has its VRAM back
        self.assertTrue(voice_settings.enabled("avatar"))                      # still wanted: it loads again when asked for
        self.assertNotIn("sup_reload", self.kinds())
        self.assertIsNone(voice_loader._pending)

    def test_a_phone_on_the_gateway_keeps_it_and_the_time_counts_from_the_last_use(self):
        self.state.clients = 1
        self.clock.t += self.idle * 3
        self.assertFalse(self.watch.step())
        self.assertEqual(self.avatar_state(), "loaded")
        self.state.clients = 0
        self.clock.t += self.idle - 1
        self.assertFalse(self.watch.step())
        self.clock.t += 2
        self.assertTrue(self.watch.step())
        self.assertEqual(self.avatar_state(), "unloaded")

    def test_a_viewer_or_a_voice_session_with_the_avatar_counts_as_use(self):
        for attr, value in (("viewers", 1), ("session", True)):
            setattr(self.state, attr, value)
            self.clock.t += self.idle * 2
            self.assertFalse(self.watch.step(), attr)
            setattr(self.state, attr, 0)

    def test_asking_again_restarts_the_idle_time(self):
        self.clock.t += self.idle - 1
        self.assertEqual(self.ensure()[0], 200)
        self.clock.t += self.idle - 1
        self.assertFalse(self.watch.step())

    def test_the_idle_time_is_adjustable(self):
        voice_settings._write({**voice_settings._read(), "avatar": {"idle_unload_s": 60}})
        self.assertEqual(self.watch.idle_s(), 60)
        self.clock.t += 61
        self.assertTrue(self.watch.step())

    def test_an_avatar_unloaded_by_hand_ends_the_lease(self):
        self.voice.state["avatar"].update(state="unloaded")
        self.assertTrue(self.watch.step())
        self.assertEqual(self.broker.state()["leases"], [])

    def test_a_lost_lease_unloads_the_avatar(self):
        self.broker.release(self.broker.state()["leases"][0]["id"])
        self.assertTrue(self.watch.step())
        self.assertEqual(self.avatar_state(), "unloaded")

    def test_the_lease_is_kept_alive_while_the_avatar_is_used(self):
        lease = self.broker.state()["leases"][0]["id"]
        self.state.clients = 1
        before = self.broker._lease.expires_at
        self.broker.clock = lambda: before - 20
        self.watch.step()
        self.assertGreater(self.broker._lease.expires_at, before)
        self.assertEqual(self.broker.state()["leases"][0]["id"], lease)


class Settings(Ensure):
    def test_the_load_choice_is_saved_and_only_the_avatar_has_it(self):
        code, out = self.req("POST", "/api/voice/load-mode", {"service": "avatar", "mode": "at_load"})
        self.assertEqual(code, 200)
        self.assertEqual(next(s["load"] for s in out["services"] if s["id"] == "avatar"), "at_load")
        self.assertEqual(json.loads(voice_settings._file().read_text(encoding="utf-8"))["services"]["avatar"]["load"], "at_load")
        for body in ({"service": "tts", "mode": "on_request"}, {"service": "avatar", "mode": "later"}, {"service": "nope", "mode": "at_load"}):
            self.assertEqual(self.req("POST", "/api/voice/load-mode", body)[0], 400, body)
        self.assertEqual(next(s["load"] for s in self.req("GET", "/api/voice/models")[1]["services"] if s["id"] == "tts"), "at_load")

    def test_the_idle_time_is_saved_through_the_avatar_settings(self):
        self.assertEqual(self.req("GET", "/api/voice/avatar")[1]["idle_unload_s"], 300)
        self.assertEqual(self.req("POST", "/api/voice/avatar", {"idle_unload_s": 600})[1]["idle_unload_s"], 600)
        for bad in (5, "600", True, 100000):
            self.assertEqual(self.req("POST", "/api/voice/avatar", {"idle_unload_s": bad})[0], 400, bad)

    def test_the_default_is_on_request_and_only_the_avatar_may_be(self):
        voice_settings._write({})
        self.assertEqual((voice_settings.load_mode("avatar"), voice_settings.load_mode("stt"), voice_settings.load_mode("tts")),
                         ("on_request", "at_load", "at_load"))


if __name__ == "__main__":
    unittest.main()
