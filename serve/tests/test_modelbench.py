"""The measurements kept for each model: what a result is found under, what is reused, and the figures compared."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import modelbench

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from model_file import ModelWriter  # noqa: E402

CONFIG = {"server": {"parallel": 1, "vision": "dynamic", "vision-hold": "release"},
          "engine": {"max-context": 131072, "kv": "int8", "spec": 7, "prefill": "auto", "resident-experts": True,
                     "vram-reserve-mib": 1536, "pool-workers": 15, "ple-io": "direct"}}
MEM = [{"tag": "before", "ram_used_gib": 27.0, "commit_used_gib": 56.0, "vram_used_mib": 2000},
       {"tag": "loaded", "ram_used_gib": 69.0, "commit_used_gib": 128.0, "vram_used_mib": 30000}]
SPEED = [{"relax": r, "prompt": p, "rep": k, "tokens": 800, "decode_tok_s": v, "ttft_s": 0.4}
         for k, (a, b) in enumerate(((150.0, 160.0), (170.0, 180.0))) for r, p, v in
         ((0.0, "conv", a), (0.0, "prose", b), (0.5, "conv", a + 10), (0.5, "prose", b + 10), (0.3, "conv", a + 20), (0.3, "prose", b + 20))]


def basalt(path: Path, experts: bytes = b"E" * 64, profile: bytes = b"P" * 16) -> str:
    w = ModelWriter(str(path))
    w.add_bytes("meta", b'{"name": "t"}')
    w.add_bytes("dense", b"D" * 32)
    w.add_bytes("experts", experts)
    w.add_bytes("profile", profile)
    w.close()
    return str(path)


def a_run(**kw) -> dict:
    return {"config": CONFIG, "power_limit_w": 600.0, "load_seconds": 66.9, "ready": True, "mem": MEM, "speed": SPEED,
            "image": [{"try": 0, "seconds": 2.4, "text": "a logo"}], "gsm8k": {"n": 200, "correct": 150, "acc": 0.75}, **kw}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.root / "data"), "TADAMOO_CACHE": str(self.root / "cache")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.dir.cleanup)


class Identity(Isolated):
    def test_the_weights_and_the_whole_file_have_their_own_fingerprint(self):
        a = modelbench.fingerprints(basalt(self.root / "a.basalt"))
        same = modelbench.fingerprints(basalt(self.root / "b.basalt"))
        other_profile = modelbench.fingerprints(basalt(self.root / "c.basalt", profile=b"Q" * 16))
        other_weights = modelbench.fingerprints(basalt(self.root / "d.basalt", experts=b"X" * 64))
        self.assertEqual((a["fp"], a["weights_fp"]), (same["fp"], same["weights_fp"]))
        self.assertEqual(a["weights_fp"], other_profile["weights_fp"])            # quality does not depend on the profile
        self.assertNotEqual(a["fp"], other_profile["fp"])                         # speed does
        self.assertNotEqual(a["weights_fp"], other_weights["weights_fp"])
        self.assertIsNone(modelbench.fingerprints(str(self.root / "missing.basalt")))

    def test_a_result_is_found_only_under_the_settings_it_was_measured_with(self):
        base = modelbench.settings_of(CONFIG, relax=[0.0, 0.5, 0.3], reps=2, power=600.0)
        k = modelbench.step_key("speed", base)
        self.assertEqual(k, modelbench.step_key("speed", {**base, "power": 610.0}))           # the power limit within 50 W is one condition
        for change in ({"kv": "q4_0"}, {"spec": 5}, {"relax": [0.0, 0.5]}, {"power": 400.0}):
            self.assertNotEqual(k, modelbench.step_key("speed", {**base, **change}), change)
        self.assertEqual(k, modelbench.step_key("speed", {**base, "vision-hold": "keep"}))              # the projector's hold does not move the speed
        self.assertNotEqual(modelbench.step_key("memory", base), modelbench.step_key("memory", {**base, "vision-hold": "keep"}))
        self.assertEqual(modelbench.step_key("quality", {**base, "spec": 5, "n": 200}), modelbench.step_key("quality", {**base, "spec": 7, "n": 200}))
        self.assertNotEqual(modelbench.step_key("quality", {**base, "n": 200}), modelbench.step_key("quality", {**base, "n": 100}))


class Store(Isolated):
    def test_a_run_is_stored_by_step_and_found_again(self):
        path = basalt(self.root / "a.basalt")
        model = {"name": "A", "path": path, "quant": "IQ3_XXS", "size_bytes": 100 << 30, "experts_bytes": 50 << 30}
        self.assertEqual(modelbench.record_run(path, model, a_run()), ["memory", "speed", "image", "quality"])
        ident = modelbench.fingerprints(path)
        s = modelbench.settings_of(CONFIG, relax=[0.0, 0.3, 0.5], reps=2, power=600.0)
        left = modelbench.availability(ident, {**s, "n": 200})
        self.assertTrue(all(left[step] for step in modelbench.STEPS), left)
        self.assertIsNone(modelbench.availability(ident, {**s, "kv": "q4_0", "n": 200})["memory"])
        self.assertIsNone(modelbench.availability(ident, {**s, "n": 100})["quality"])

    def test_quality_is_shared_by_files_with_the_same_weights(self):
        a = basalt(self.root / "a.basalt")
        b = basalt(self.root / "b.basalt", profile=b"Q" * 16)
        modelbench.record_run(a, {"name": "A"}, a_run())
        ident_b = modelbench.fingerprints(b)
        s = {**modelbench.settings_of(CONFIG), "n": 200}
        found = modelbench.availability(ident_b, s)
        self.assertIsNotNone(found["quality"])
        self.assertIsNone(found["speed"] or found["memory"])

    def test_only_the_latest_results_stay(self):
        path = basalt(self.root / "a.basalt")
        ident = modelbench.fingerprints(path)
        for i in range(modelbench.KEEP + 3):
            modelbench.record(ident, {"name": "A"}, "image", f"k{i}", {}, {"tries": []}, at=1000 + i)
        rows = modelbench.read(ident["fp"])["results"]["image"]
        self.assertEqual(len(rows), modelbench.KEEP)
        self.assertEqual(rows[0]["key"], f"k{modelbench.KEEP + 2}")


class Figures(Isolated):
    def test_the_comparable_numbers(self):
        path = basalt(self.root / "a.basalt")
        model = {"name": "A", "path": path, "quant": "IQ3_XXS", "size_bytes": 100 << 30, "experts_bytes": 50 << 30}
        modelbench.record_run(path, model, a_run())
        view = modelbench.overview([model])
        mine = next(m for m in view["models"] if m["kind"] == "local")
        f = mine["figures"]
        self.assertEqual((f["size_gib"], f["experts_gib"]), (100.0, 50.0))
        self.assertEqual((f["ram_gib"], f["commit_gib"], f["vram_gib"], f["load_s"]), (42.0, 72.0, 27.3, 66.9))
        self.assertEqual((f["decode"], f["decode_r05"], f["decode_r03"]), (165.0, 175.0, 185.0))
        self.assertEqual(f["relax_gain_pct"], round((185.0 / 165.0 - 1) * 100, 1))
        self.assertEqual((f["gsm8k_pct"], f["image_ok"], f["ttft_s"]), (75.0, True, 0.4))
        self.assertEqual(set(mine["tests"]), set(modelbench.STEPS))
        self.assertGreaterEqual(len([m for m in view["models"] if m["kind"] == "published"]), 8)
        self.assertTrue({m["id"] for m in modelbench.METRICS} >= {"decode", "ram_gib", "gsm8k_pct", "kld"})

    def test_a_file_never_tested_has_only_what_its_header_says(self):
        path = basalt(self.root / "a.basalt")
        view = modelbench.overview([{"path": path, "name": "A", "quant": "Q4", "size_bytes": 10 << 30, "experts_bytes": 5 << 30}])
        mine = next(m for m in view["models"] if m["kind"] == "local")
        self.assertEqual(mine["figures"], {"size_gib": 10.0, "experts_gib": 5.0})
        self.assertEqual(mine["tests"], {})


if __name__ == "__main__":
    unittest.main()
