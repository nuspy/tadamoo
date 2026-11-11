"""The VRAM broker with a stand-in service, a stand-in reading of the free VRAM and a clock of its own: grants, refusals, the queue, expiry, giving
the reserve back. Nothing reaches an engine or a GPU."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from serve.engine.errors import EngineDied, ModelBusy  # noqa: E402
from serve.vram_broker import BrokerPrefs, VramBroker  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeService:
    """Keeps `reserve` MiB free out of `total`: the engine's expert cache gives way down to `floor` MiB of cache."""

    def __init__(self, free_at_start=1500, cache=23000, floor=2000, loaded=True, elastic=True):
        self.free0, self.cache0, self.floor = free_at_start, cache, floor
        self.cache, self._loaded, self.elastic = cache, loaded, elastic
        self.vram_reserve = None
        self.calls = []
        self.loading = False
        self.error = None

    def loaded(self):
        return self._loaded

    @property
    def free(self):
        return self.free0 + (self.cache0 - self.cache)

    def vram(self, reserve):
        self.calls.append(reserve)
        if self.error is not None:
            raise self.error
        if not self._loaded:
            self.vram_reserve = reserve
            return {"status": "not loaded", "reserve_mib": reserve}
        if not self.elastic:
            raise ValueError("VRAM needs an engine started with --vram-elastic (one NVIDIA GPU, --serve)")
        want = 1500 if reserve is None else reserve                  # the start's reserve is 1500
        self.cache = max(self.floor, self.cache0 - (want - self.free0))
        self.vram_reserve = reserve
        return {"status": "ok", "reserve_mib": want, "vram_free_mib": self.free, "expert_cache_mib": self.cache,
                "lent_mib": self.cache0 - self.cache, "capped": 1 if self.cache == self.floor else 0}


def make(svc=None, **kw):
    svc = svc or FakeService()
    clock = kw.pop("clock", None) or FakeClock()
    prefs = kw.pop("prefs", None)
    probe = kw.pop("probe", None) or (lambda: svc.free)
    return VramBroker(svc, probe=probe, clock=clock, start_reserve=lambda: 1500, prefs=prefs, timer=False, **kw), svc, clock


class GrantTests(unittest.TestCase):
    def test_a_lease_asks_the_reserve_plus_its_size_and_checks_the_free_vram(self):
        b, svc, _ = make()
        r = b.acquire("docling", 3000, "pdf")
        self.assertTrue(r["ok"], r)
        self.assertEqual(svc.calls, [4500])
        self.assertEqual(r["lease"]["granted_mib"], 3000)
        self.assertEqual(r["lease"]["lent_mib"], 3000)
        self.assertGreaterEqual(svc.free, 3000)

    def test_the_last_reserve_is_the_base_when_one_was_set(self):
        b, svc, _ = make()
        svc.vram_reserve = 2000
        self.assertTrue(b.acquire("a", 1000)["ok"])
        self.assertEqual(svc.calls, [3000])
        b.release(b.state()["leases"][0]["id"])
        self.assertEqual(svc.calls[-1], 2000)                          # back to the reserve that was set before

    def test_free_vram_already_there_lends_nothing(self):
        svc = FakeService(free_at_start=9000)
        b, _, _ = make(svc)
        r = b.acquire("a", 3000)
        self.assertTrue(r["ok"])
        self.assertEqual((svc.calls, r["lease"]["lent_mib"]), ([], 0))

    def test_the_reply_may_carry_more_keys(self):
        b, svc, _ = make()
        orig = svc.vram
        svc.vram = lambda r: {**orig(r), "reserve_max_mib": 24000, "expert_slots_start": 99, "unknown": "x"}
        r = b.acquire("a", 3000)
        self.assertTrue(r["ok"])
        self.assertEqual(b.state()["last_reply"]["reserve_max_mib"], 24000)

    def test_bad_requests_are_answered_not_raised(self):
        b, _, _ = make()
        for args in (("", 100), ("a", 0), ("a", -5), ("a", "9"), ("a", True), (None, 5)):
            self.assertEqual(b.acquire(*args)["reason"], "bad_request", args)
        self.assertEqual(b.acquire("a", 5, hold_s=1)["reason"], "bad_request")
        self.assertEqual(b.acquire("a", 5, wait_s=-1)["reason"], "bad_request")

    def test_a_service_that_raises_is_not_an_exception_for_the_caller(self):
        b, svc, _ = make()
        svc.error = RuntimeError("boom")
        r = b.acquire("a", 3000)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "busy")


class RefusalTests(unittest.TestCase):
    def test_no_service_is_no_engine(self):
        b = VramBroker(None, timer=False)
        self.assertEqual(b.acquire("a", 10)["reason"], "no_engine")

    def test_an_engine_without_vram_elastic_is_unavailable(self):
        b, svc, _ = make(FakeService(elastic=False))
        r = b.acquire("a", 3000)
        self.assertEqual(r["reason"], "unavailable")
        self.assertIn("message_it", r)
        self.assertEqual(b.state()["leases"], [])

    def test_not_enough_even_with_the_whole_cache_lent(self):
        svc = FakeService(cache=23000, floor=21000)                     # only 2000 MiB can be lent
        b, _, _ = make(svc)
        r = b.acquire("a", 6000)
        self.assertEqual(r["reason"], "not_enough")
        self.assertEqual(svc.calls[-1], None)                           # the reserve went back at once
        self.assertEqual(svc.cache, 23000)

    def test_an_engine_that_ended(self):
        b, svc, _ = make()
        svc.error = EngineDied("the engine is not running")
        self.assertEqual(b.acquire("a", 3000)["reason"], "engine_died")

    def test_a_request_still_running_is_busy(self):
        b, svc, _ = make()
        svc.error = ModelBusy("a request is still running")
        self.assertEqual(b.acquire("a", 3000)["reason"], "busy")

    def test_a_model_that_is_loading_is_busy(self):
        b, svc, _ = make()
        svc.loading = True
        self.assertEqual(b.acquire("a", 3000)["reason"], "busy")

    def test_the_broker_turned_off_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            prefs = BrokerPrefs(Path(d) / "b.json")
            prefs.write({"enabled": False})
            b, _, _ = make(prefs=prefs)
            self.assertEqual(b.acquire("a", 10)["reason"], "disabled")

    def test_a_model_not_loaded_only_checks_the_free_vram(self):
        svc = FakeService(loaded=False, free_at_start=5000)
        b, _, _ = make(svc, probe=lambda: 5000)
        self.assertTrue(b.acquire("a", 3000)["ok"])
        self.assertEqual(svc.calls, [])
        b.release(b.state()["leases"][0]["id"])
        r = b.acquire("a", 6000)
        self.assertEqual((r["reason"], r["free_mib"], r["needed_mib"]), ("not_enough", 5000, 6000))

    def test_an_unreadable_gpu_with_no_model_is_unavailable(self):
        b, _, _ = make(FakeService(loaded=False), probe=lambda: None)
        self.assertEqual(b.acquire("a", 100)["reason"], "unavailable")


class HoldTests(unittest.TestCase):
    def test_a_second_lease_is_busy_while_the_first_holds(self):
        b, _, _ = make()
        first = b.acquire("a", 3000)
        r = b.acquire("b", 1000)
        self.assertEqual((r["reason"], r["holder"]), ("busy", "a"))
        b.release(first["lease"]["id"])
        self.assertTrue(b.acquire("b", 1000)["ok"])

    def test_release_puts_the_reserve_back_to_the_starts(self):
        b, svc, _ = make()
        r = b.acquire("a", 3000)
        self.assertEqual(b.release(r["lease"]["id"])["ok"], True)
        self.assertEqual(svc.calls, [4500, None])
        self.assertIsNone(svc.vram_reserve)
        self.assertEqual(svc.cache, svc.cache0)
        self.assertEqual(b.release(r["lease"]["id"])["reason"], "not_found")

    def test_a_lease_that_is_not_touched_expires_and_gives_back(self):
        b, svc, clock = make()
        r = b.acquire("a", 3000, hold_s=30)
        clock.t += 29
        b.reap()
        self.assertEqual(len(b.state()["leases"]), 1)
        clock.t += 2
        b.reap()
        self.assertEqual(b.state()["leases"], [])
        self.assertEqual(svc.calls[-1], None)
        self.assertEqual(b.touch(r["lease"]["id"])["reason"], "not_found")

    def test_touch_extends_the_hold(self):
        b, svc, clock = make()
        r = b.acquire("a", 3000, hold_s=30)
        for _ in range(5):
            clock.t += 20
            self.assertTrue(b.touch(r["lease"]["id"])["ok"])
            b.reap()
        self.assertEqual(len(b.state()["leases"]), 1)
        self.assertEqual(svc.calls, [4500])

    def test_the_default_hold_comes_from_the_settings(self):
        with tempfile.TemporaryDirectory() as d:
            prefs = BrokerPrefs(Path(d) / "b.json")
            prefs.write({"hold_s": 90})
            b, _, _ = make(prefs=prefs)
            self.assertEqual(b.acquire("a", 100)["lease"]["hold_s"], 90)
            self.assertEqual(BrokerPrefs().read(), {"enabled": True, "hold_s": 60})

    def test_the_engine_that_died_while_lent_gives_its_reserve_to_the_next_one(self):
        b, svc, _ = make()
        r = b.acquire("a", 3000)
        svc.error = EngineDied("ended")
        b.release(r["lease"]["id"])
        self.assertIsNone(svc.vram_reserve)
        self.assertFalse(b.state()["changed"])

    def test_a_busy_engine_at_the_end_is_tried_again_by_the_tick(self):
        b, svc, _ = make()
        r = b.acquire("a", 3000)
        svc.error = ModelBusy("running")
        b.release(r["lease"]["id"])
        self.assertTrue(b.state()["changed"])
        svc.error = None
        b._restore_if_idle()
        self.assertFalse(b.state()["changed"])
        self.assertEqual(svc.cache, svc.cache0)

    def test_the_shutdown_gives_back_what_is_lent(self):
        b, svc, _ = make()
        b.acquire("a", 3000)
        b.shutdown()
        self.assertEqual(svc.calls[-1], None)
        self.assertEqual(svc.cache, svc.cache0)

    def test_the_state_shows_the_lease_and_the_free_vram(self):
        b, svc, _ = make()
        b.acquire("a", 3000, "pdf")
        s = b.state()
        self.assertEqual((s["engine"], s["leases"][0]["owner"], s["leases"][0]["purpose"]), ("running", "a", "pdf"))
        self.assertEqual((s["free_mib"], s["lent_mib"], s["reserve_mib"]), (svc.free, 3000, 4500))


class QueueTests(unittest.TestCase):
    def test_waiters_go_in_the_order_they_came(self):
        b, svc, _ = make()
        first = b.acquire("a", 1000)
        order, threads = [], []

        def waiter(name):
            r = b.acquire(name, 1000, wait_s=10)
            order.append((name, r["ok"]))
            if r["ok"]:
                time.sleep(0.05)
                b.release(r["lease"]["id"])

        for name in ("b", "c", "d"):
            t = threading.Thread(target=waiter, args=(name,))
            t.start()
            threads.append(t)
            while len(b._queue) < len(threads):                          # b is queued before c comes
                time.sleep(0.005)
        b.release(first["lease"]["id"])
        for t in threads:
            t.join(10)
        self.assertEqual(order, [("b", True), ("c", True), ("d", True)])
        self.assertEqual(svc.calls[-1], None)

    def test_the_reserve_is_not_put_back_between_two_waiting_jobs(self):
        b, svc, _ = make()
        first = b.acquire("a", 1000)
        got = []
        t = threading.Thread(target=lambda: got.append(b.acquire("b", 2000, wait_s=10)))
        t.start()
        while not b._queue:
            time.sleep(0.005)
        b.release(first["lease"]["id"])
        t.join(10)
        self.assertTrue(got[0]["ok"])
        self.assertEqual(svc.calls, [2500, 3500])                         # no None between the two

    def test_a_waiter_that_runs_out_of_time_is_busy_and_nothing_stays_lent(self):
        b, svc, clock = make(clock=time.monotonic)
        first = b.acquire("a", 1000, hold_s=5)
        t0 = time.monotonic()
        r = b.acquire("b", 1000, wait_s=0.3)
        self.assertEqual(r["reason"], "busy")
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        self.assertEqual(b.state()["queue"], 0)
        b.release(first["lease"]["id"])
        self.assertIsNone(svc.vram_reserve)

    def test_an_expiry_hands_the_vram_to_the_waiter(self):
        clock = FakeClock()
        b, svc, _ = make(clock=clock)
        b.acquire("a", 1000, hold_s=5)
        got = []
        t = threading.Thread(target=lambda: got.append(b.acquire("b", 1000, wait_s=100)))
        t.start()
        while not b._queue:
            time.sleep(0.005)
        clock.t += 6
        t.join(10)
        self.assertTrue(got and got[0]["ok"])

    def test_concurrent_acquires_grant_exactly_one(self):
        b, svc, _ = make()
        results, gate = [], threading.Barrier(8)

        def go(i):
            gate.wait()
            results.append(b.acquire(f"w{i}", 500))

        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertEqual(sum(1 for r in results if r["ok"]), 1)
        self.assertEqual(sum(1 for r in results if r.get("reason") == "busy"), 7)
        self.assertEqual(len(svc.calls), 1)


class PrefsTests(unittest.TestCase):
    def test_values_are_checked_and_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = BrokerPrefs(Path(d) / "b.json")
            self.assertEqual(p.write({"hold_s": 120}), {"enabled": True, "hold_s": 120})
            self.assertEqual(p.write({"enabled": False})["hold_s"], 120)
            for bad in ({"hold_s": 1}, {"hold_s": "9"}, {"hold_s": True}, {"enabled": "no"}):
                with self.assertRaises(ValueError):
                    p.write(bad)
            self.assertEqual(p.read(), {"enabled": False, "hold_s": 120})


if __name__ == "__main__":
    unittest.main()
