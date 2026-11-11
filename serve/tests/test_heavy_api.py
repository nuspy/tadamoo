"""The routes of the heavy-job swap, on a real server with a stand-in engine and a registry in a temporary data folder."""
import threading
import time
import unittest

from serve import heavy_api
from serve.heavy_swap import HeavyPrefs, HeavySwap
from serve.tests.test_vram_api import Server
from tadamoo.llm.credentials import FileCredentials
from tadamoo.llm.store import SecureRegistry


class Asleep:
    heavy_hold = None
    sleeps = wakes = 0

    def __init__(self):
        self.engine = self

    def loaded(self):
        return True

    def can_sleep(self):
        return True

    def sleep(self):
        self.sleeps += 1
        return "asleep"

    def wake_now(self):
        self.wakes += 1


class HeavyRoutes(Server):
    def setUp(self):
        super().setUp()
        from pathlib import Path
        base = Path(self.tmp.name)
        self.registry = SecureRegistry(base / "llm.json", credentials=FileCredentials(base / "creds.json"))
        self.cloud = self.registry.add({"type": "zai_coding", "api_key": "zk-123456789012"})
        self.model = Asleep()
        self.svc.heavy_swap = HeavySwap(self.model, self.registry, HeavyPrefs(base / "heavy.json"), pause=lambda s: None)

    def test_the_settings_are_shown_and_changed_and_a_wrong_value_is_a_400(self):
        code, out = self.req("GET", heavy_api.BASE)
        self.assertEqual((code, out["mode"], out["active"], out["pending"]), (200, "ask", None, []))
        code, out = self.req("POST", heavy_api.SETTINGS, {"mode": "allow", "provider": self.cloud["id"]})
        self.assertEqual((code, out["mode"], out["provider"]), (200, "allow", self.cloud["id"]))
        self.assertEqual(self.req("POST", heavy_api.SETTINGS, {"mode": "sometimes"})[0], 400)

    def test_a_job_begins_keeps_the_swap_and_ends_it_and_a_refusal_is_a_409_in_both_languages(self):
        code, out = self.req("POST", heavy_api.BEGIN, {"kind": "video", "purpose": "x"})
        self.assertEqual((code, out["reason"]), (409, "no_provider"))
        self.assertTrue(out["message"] and out["message_it"])
        self.req("POST", heavy_api.SETTINGS, {"mode": "allow", "provider": self.cloud["id"]})
        code, out = self.req("POST", heavy_api.BEGIN, {"kind": "video", "purpose": "a clip", "minutes": 3})
        self.assertEqual((code, out["ok"], out["swap"]["provider"] != ""), (200, True, True))
        sid = out["swap"]["id"]
        self.assertEqual(self.req("GET", heavy_api.BASE)[1]["active"]["id"], sid)
        self.assertEqual(self.req("POST", f"/api/heavy/{sid}/touch")[0], 200)
        self.assertEqual(self.req("POST", f"/api/heavy/{sid}/end")[0], 200)
        self.assertEqual(self.req("POST", f"/api/heavy/{sid}/end")[0], 404)
        self.assertEqual((self.model.sleeps, self.model.wakes), (1, 1))

    def test_a_question_waits_for_the_answer_the_page_sends(self):
        self.req("POST", heavy_api.SETTINGS, {"provider": self.cloud["id"]})
        done = {}
        t = threading.Thread(target=lambda: done.update(zip(("code", "out"), self.req("POST", heavy_api.BEGIN, {"kind": "video", "purpose": "x"}))))
        t.start()
        for _ in range(100):
            cards = self.req("GET", heavy_api.BASE)[1]["pending"]
            if cards:
                break
            time.sleep(0.05)
        self.assertEqual(cards[0]["kind"], "video")
        self.assertEqual(self.req("POST", heavy_api.ANSWER, {"id": cards[0]["id"], "allow": False})[0], 200)
        t.join(10)
        self.assertEqual((done["code"], done["out"]["reason"]), (409, "not_allowed"))
        self.assertEqual(self.model.sleeps, 0)


if __name__ == "__main__":
    unittest.main()
