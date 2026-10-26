"""The test job of the Model converter page: the settings it tests with, asking before it repeats a test, storing only what it ran, and
what it says when the model does not load."""
from __future__ import annotations

import time
import unittest
from unittest import mock

from serve import benchrun, modelbench
from serve.tests.test_modelbench import Isolated, MEM, basalt


def speed_rows(relax, reps):
    bonus = {0.0: 0, 0.5: 10, 0.3: 20, 0.2: 30}
    return [{"relax": r, "prompt": p, "rep": k, "tokens": 800, "decode_tok_s": base + 20.0 * k + bonus.get(r, 0), "ttft_s": 0.4}
            for k in range(reps) for r in relax for p, base in (("conv", 150.0), ("prose", 160.0))]


def fake_runner(calls: list, **extra):
    def run(path, settings, steps, relax, reps, n, baseline, workdir, cancelled, say, progress):
        calls.append({"steps": list(steps), "relax": list(relax), "n": n, "settings": dict(settings), "baseline": baseline})
        say("working")
        progress(0.5)
        out = {"config": benchrun.config_object(path, settings, 0, "bench"), "power_limit_w": 600.0, "at": time.time(), "ready": True, "load_seconds": 50.0,
               "mem": MEM, "speed": speed_rows(relax, reps), "prefill_tok_s": 4000.0, "decode_off": 100.0, "image": [{"try": 0, "seconds": 2.0, "text": "a logo"}],
               "gsm8k": {"n": n, "correct": n - 3, "acc": round((n - 3) / n, 4)}}
        out.update(extra)
        return out
    return run


class Helpers(unittest.TestCase):
    def test_the_standard_settings_and_their_overrides(self):
        s = benchrun.standard_settings({"kv": "fp16", "nothing": 1, "spec": None})
        self.assertEqual((s["kv"], s["spec"], s["parallel"], s["vision-hold"], s["max-context"]), ("fp16", 7, 1, "release", 131072))
        self.assertNotIn("nothing", s)

    def test_the_config_file_of_a_test_server(self):
        c = benchrun.config_object("E:/m.basalt", benchrun.standard_settings(), 7301, "bench")
        self.assertEqual((c["server"]["port"], c["server"]["model"], c["server"]["vision"], c["server"]["vision-hold"]), (7301, "E:/m.basalt", "dynamic", "release"))
        self.assertEqual((c["engine"]["kv"], c["engine"]["spec"]), ("int8", 7))
        self.assertNotIn("parallel", c["engine"])
        off = benchrun.config_object("E:/m.basalt", {**benchrun.standard_settings(), "vision": "off"}, 7301, "bench")
        self.assertNotIn("vision-hold", off["server"])

    def test_the_final_number_of_an_answer(self):
        for text, want in (("so 12 apples.\nAnswer: 18", 18.0), ("Answer: $1,250.50", 1250.5), ("#### 7", 7.0), ("it is 3 then 40", 40.0), ("no numbers", None)):
            self.assertEqual(benchrun.final_number(text), want, text)


class Job(Isolated):
    def setUp(self):
        super().setUp()
        self.jobs = benchrun.Jobs()
        self.path = basalt(self.root / "a.basalt")
        self.model = {"name": "A", "path": self.path, "quant": "IQ3_XXS", "size_bytes": 100 << 30, "experts_bytes": 50 << 30}
        patch = mock.patch.object(benchrun, "power_limit_w", lambda: 600.0)
        patch.start()
        self.addCleanup(patch.stop)

    def wait(self):
        deadline = time.time() + 10
        while self.jobs.running() and time.time() < deadline:
            time.sleep(0.02)
        return self.jobs.status()

    def start(self, reuse="ask", steps=modelbench.STEPS, calls=None, **kw):
        return self.jobs.start(self.model, list(steps), None, list(modelbench.RELAX_DEFAULT), 2, 200, True, reuse, runner=fake_runner([] if calls is None else calls, **kw))

    def test_a_first_run_runs_every_step_and_keeps_the_results(self):
        calls = []
        self.start(calls=calls)
        st = self.wait()
        self.assertEqual((st["phase"], st["stored"]), ("done", ["memory", "speed", "image", "quality"]))
        self.assertEqual(calls[0]["steps"], ["memory", "speed", "image", "quality"])
        self.assertEqual(calls[0]["relax"], [0.0, 0.2, 0.3, 0.5])
        fig = next(m for m in modelbench.overview([self.model])["models"] if m["kind"] == "local")["figures"]
        self.assertEqual((fig["decode"], fig["decode_off"], fig["prefill"], fig["gsm8k_pct"]), (165.0, 100.0, 4000.0, 98.5))

    def test_a_repeat_asks_before_testing_again(self):
        self.start()
        self.wait()
        with self.assertRaises(benchrun.Refused) as e:
            self.start()
        body = e.exception.body
        self.assertEqual((e.exception.status, body["error"]["code"]), (409, "reuse"))
        self.assertEqual(set(body["available"]), set(modelbench.STEPS))
        self.assertIn("at", body["available"]["speed"])

    def test_reusing_runs_nothing_and_redoing_runs_again(self):
        self.start()
        self.wait()
        calls = []
        st = self.start(reuse="reuse", calls=calls)
        self.assertEqual((st["phase"], calls, st["reused"]), ("done", [], list(modelbench.STEPS)))
        self.start(reuse="redo", calls=calls)
        self.wait()
        self.assertEqual(calls[0]["steps"], list(modelbench.STEPS))

    def test_each_step_can_be_reused_or_redone_on_its_own(self):
        self.start()
        self.wait()
        calls = []
        self.start(reuse={"memory": "reuse", "speed": "redo", "image": "reuse", "quality": "reuse"}, calls=calls)
        st = self.wait()
        self.assertEqual((calls[0]["steps"], st["stored"], sorted(st["reused"])), (["speed"], ["speed"], ["image", "memory", "quality"]))
        with self.assertRaises(benchrun.Refused):                      # a step with no answer yet stays unasked, one with an answer and no choice asks
            self.start(reuse={"memory": "reuse"})

    def test_only_the_steps_asked_for_run(self):
        calls = []
        self.start(steps=("quality",), calls=calls)
        st = self.wait()
        self.assertEqual((calls[0]["steps"], st["stored"]), (["quality"], ["quality"]))
        self.assertEqual(list(modelbench.availability(modelbench.fingerprints(self.path), {**benchrun.standard_settings(), "n": 200}, ("memory", "quality")).values())[0], None)

    def test_a_model_that_does_not_load_says_why_and_stores_nothing(self):
        self.start(ready=False, error="the engine stopped: not enough memory")
        st = self.wait()
        self.assertEqual((st["phase"], st["error"]), ("failed", "the engine stopped: not enough memory"))
        self.assertEqual(modelbench.read(modelbench.fingerprints(self.path)["fp"]), {})

    def test_the_precheck_can_refuse_and_a_second_job_waits(self):
        def refuse(path, settings):
            raise benchrun.Refused(409, {"error": {"code": "model_loaded", "message": "a model is loaded"}})
        with self.assertRaises(benchrun.Refused) as e:
            self.jobs.start(self.model, ["quality"], None, [0.0], 1, 10, False, "redo", precheck=refuse, runner=fake_runner([]))
        self.assertEqual(e.exception.body["error"]["code"], "model_loaded")
        self.assertEqual(self.jobs.status()["phase"], "idle")

        def slow(*a):
            time.sleep(0.4)
            return fake_runner([])(*a)
        self.jobs.start(self.model, ["quality"], None, [0.0], 1, 10, False, "redo", runner=slow)
        with self.assertRaises(benchrun.Refused) as e:
            self.jobs.start(self.model, ["quality"], None, [0.0], 1, 10, False, "redo", runner=slow)
        self.assertEqual(e.exception.body["error"]["code"], "busy")
        self.wait()

    def test_a_cancelled_run_stores_nothing(self):
        def slow(path, settings, steps, relax, reps, n, baseline, workdir, cancelled, say, progress):
            while not cancelled():
                time.sleep(0.02)
            return {}
        self.jobs.start(self.model, ["quality"], None, [0.0], 1, 10, False, "redo", runner=slow)
        self.assertTrue(self.jobs.cancel())
        self.assertEqual(self.wait()["phase"], "cancelled")
        self.assertEqual(modelbench.read(modelbench.fingerprints(self.path)["fp"]), {})

    def test_a_file_that_is_not_a_model_is_refused(self):
        with self.assertRaises(benchrun.Refused) as e:
            self.jobs.start({"path": str(self.root / "nope.basalt")}, ["quality"], None, [0.0], 1, 10, False, "ask", runner=fake_runner([]))
        self.assertEqual(e.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
