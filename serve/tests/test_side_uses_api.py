"""The settings route of the technical session's uses, and the count of its work by use."""
import json
import os
import tempfile
import threading
from unittest import mock

from tadamoo import side_uses
from serve.tests import test_api_plans as base


class SideUsesApiTest(base.PlansApiBase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"BASALT_DATA": cls.data.name})
        cls.env.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.env.stop()
        cls.data.cleanup()

    def setUp(self):
        side_uses.reset()

    def test_the_route_lists_every_use_with_its_numbers_prompt_and_work(self):
        code, v = self.call("GET", "/api/side-uses")
        self.assertEqual(code, 200)
        self.assertEqual([u["id"] for u in v["uses"]], [u.id for u in side_uses.USES])
        self.assertFalse(v["session"]["available"])                              # the mock engine has no technical session
        triage = next(u for u in v["uses"] if u["id"] == "triage")
        self.assertTrue(triage["on"] and triage["prompt"] and not triage["changed"])
        self.assertEqual({p["key"] for p in triage["params"]}, {"max_tokens", "timeout_s"})
        self.assertEqual(triage["stats"]["calls"], 0)

    def test_a_use_is_switched_off_changed_and_reset_and_a_bad_value_is_refused(self):
        code, v = self.call("POST", "/api/side-uses", {"use": "judge", "on": False})
        self.assertEqual(code, 200)
        self.assertFalse(next(u for u in v["uses"] if u["id"] == "judge")["on"])
        self.assertFalse(side_uses.enabled("judge"))
        code, v = self.call("POST", "/api/side-uses", {"use": "triage", "params": {"timeout_s": 60}})
        self.assertEqual(next(p for u in v["uses"] if u["id"] == "triage" for p in u["params"] if p["key"] == "timeout_s")["value"], 60)
        self.assertEqual(self.call("POST", "/api/side-uses", {"use": "triage", "params": {"timeout_s": 9999}})[0], 400)
        self.assertEqual(self.call("POST", "/api/side-uses", {"use": "judge", "system": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/side-uses", {"use": "nothing", "on": True})[0], 400)
        code, v = self.call("POST", "/api/side-uses", {"reset": "judge"})
        self.assertTrue(side_uses.enabled("judge") and side_uses.param("triage", "timeout_s") == 60)
        code, v = self.call("POST", "/api/side-uses", {"reset": "all"})
        self.assertTrue(all(not u["changed"] for u in v["uses"]))

    def test_the_route_of_the_quick_jobs_and_the_master_switch_for_outside_providers(self):
        code, v = self.call("GET", "/api/side-uses")
        self.assertEqual((v["route"]["provider"], v["external"]["on"], v["session"]["available"]), ("", False, False))
        code, out = self.call("POST", "/api/llm/providers", {"type": "openai_compat", "base_url": "https://api.example.invalid/v1", "api_key": "k" * 12, "model": "small"})
        self.assertEqual(code, 200)
        pid = out["provider"]["id"]
        self.assertEqual(self.call("POST", "/api/llm/consent", {"function": "technical", "allowed": True})[0], 200)
        self.assertEqual(self.call("POST", "/api/llm/assign", {"function": "technical", "provider": pid})[0], 200)
        code, v = self.call("GET", "/api/side-uses")
        self.assertEqual(v["route"]["provider"], pid)
        self.assertIn(pid, [p["id"] for p in v["route"]["providers"]])
        self.assertFalse(v["session"]["available"])                               # the master switch is off: nothing runs outside
        code, v = self.call("POST", "/api/side-uses", {"external": True})
        self.assertEqual((code, v["external"]["on"], v["session"]["available"], v["session"]["tokens"], v["session"]["local"]), (200, True, True, 30000, False))
        self.assertEqual(self.call("POST", "/api/side-uses", {"external": "yes"})[0], 400)
        self.call("POST", "/api/llm/assign", {"function": "technical", "provider": None})
        self.call("POST", "/api/side-uses", {"external": False})
        self.call("POST", "/api/llm/providers/%s/remove" % pid, {})

    def test_the_free_text_prompt_is_replaced_and_stop_ends_a_running_job(self):
        code, v = self.call("POST", "/api/side-uses", {"use": "chat_abstract", "system": "One line."})
        self.assertEqual(next(u for u in v["uses"] if u["id"] == "chat_abstract")["prompt"], "One line.")
        job = side_uses.job("compaction", "summary")
        job.progress(2, 9)
        code, v = self.call("GET", "/api/side-uses")
        self.assertEqual([(r["use"], r["done"], r["total"]) for r in v["running"]], [("compaction", 2, 9)])
        self.assertFalse(job.stopped())
        self.assertEqual(self.call("POST", "/api/side-uses", {"stop": True})[0], 200)
        self.assertTrue(job.stopped())
        job.close()

    def test_the_session_s_work_is_counted_under_the_use_the_call_named(self):
        def ask(**extra):
            from serve.frontend import openai_to_messages
            msgs, tools, kw = openai_to_messages({"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "none"})
            ids, thinking, max_new = self.svc.prepare(msgs, tools, kw, 8)
            return [x for kind, x in self.svc.run(ids, thinking, None, max_new, {"temperature": 0.0, **extra}, threading.Event()) if kind == "done"][0]
        ask(cache_slot="side", side_use="judge")
        ask(cache_slot="side", side_use="judge")
        ask(cache_slot="side")                                                   # no name: not counted
        ask(side_use="router")                                                   # not for the session: not counted
        row = self.svc.side_stats_view()["judge"]
        self.assertEqual((row["calls"], row["main"], row["cut"]), (2, 2, 2))     # no session in the mock: both ran in an ordinary slot, and the 8-token cap cut both
        self.assertNotIn("router", self.svc.side_stats_view())

    def test_a_bad_name_in_a_request_is_a_400(self):
        from serve.api.cache_slot import check_cache_slot
        check_cache_slot({"side_use": "chat_summary"})
        for bad in ("", "a b", "x" * 41, 5, "a-b"):
            with self.assertRaises(ValueError):
                check_cache_slot({"side_use": bad})


class StrictSideTest(base.PlansApiBase):
    """A request for the session alone is refused, not served by an ordinary slot, when it does not fit."""

    def test_side_only_is_refused_over_http_when_the_session_cannot_take_it(self):
        body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8, "cache_slot": "side_only", "side_use": "compaction"}
        code, out = self.call("POST", "/v1/chat/completions", body)
        self.assertEqual(code, 400)
        self.assertIn("technical session", json.dumps(out))
        code, out = self.call("POST", "/v1/chat/completions", {**body, "cache_slot": "side"})
        self.assertEqual(code, 200)                                                  # the plain request is still served by an ordinary slot
        self.assertEqual(self.call("POST", "/v1/chat/completions", {**body, "cache_slot": "elsewhere"})[0], 400)
