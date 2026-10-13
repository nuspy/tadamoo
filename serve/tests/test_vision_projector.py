"""A vision projector the engine's image encoder would refuse (src/vision/mmproj.cpp, encoder_*.cpp: a qwen3vl_merger
with BF16 matrices and F32 vectors): the scan says so from the header, the conversion leaves it out, and "Valori
sicuri", the wizard and a plan do not turn image input on with it. The first real load of an F16 projector (an
mmproj-...-f16.gguf) stopped the engine at start with "tensor mm.2.weight has an unexpected type or size"."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import loader, models
from serve.tests.test_models import BF16, F16, F32, projector_gguf, tiny_basalt, tiny_qwen

ROOMY = {"ram_total": 128 << 30, "ram_available": 120 << 30, "commit_limit": 144 << 30, "commit_used": 10 << 30,
         "vram_total": 32 << 30, "vram_free": 31 << 30}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.env = mock.patch.dict(os.environ, {"BASALT_DATA": self.dir.name, "BASALT_CACHE": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()


class TheCheck(Isolated):
    def problem(self, **kw):
        return models.projector_problem(models.read_gguf(str(projector_gguf(self.root / "p.gguf", **kw))))

    def test_bf16_matrices_and_f32_vectors_are_read(self):
        self.assertIsNone(self.problem())

    def test_an_f16_projector_is_refused_with_the_reason(self):
        p = self.problem(mat=F16, vec=F16)
        self.assertIn("F16", p["en"])
        self.assertIn("mm.2.weight", p["en"])
        self.assertIn("BF16", p["it"])
        self.assertIn("BF16", self.problem(vec=BF16)["en"])              # vectors in BF16: refused as well

    def test_another_layout_or_size(self):
        self.assertIn("qwen3vl_merger", self.problem(**{"clip.projector_type": "mlp"})["en"])
        self.assertIn("embedding_length", self.problem(**{"clip.vision.embedding_length": 1024})["en"])
        bad = models.read_gguf(str(projector_gguf(self.root / "q.gguf")))
        bad["tensors"] = [(n, [7] if n == "mm.2.bias" else s, t, o) for n, s, t, o in bad["tensors"]]
        self.assertIn("mm.2.bias", models.projector_problem(bad)["en"])


class TheScanAndTheConversion(Isolated):
    def test_an_f16_projector_in_the_folder_is_left_out(self):
        models_dir = self.root / "models"
        models_dir.mkdir()
        with mock.patch.dict(models.GEOMETRY, {"n_embd": 256, "n_ff": 64, "n_layer": 2, "n_expert_max": 4}):
            tiny_qwen(models_dir)
            projector_gguf(models_dir / "mmproj-Tiny-f16.gguf", mat=F16, vec=F16)
            listing = models.scan([str(models_dir)])
        (m,) = [x for x in listing["models"] if x["role"] == "model"]
        self.assertEqual(m["status"], "convert", m["reason"])
        self.assertFalse(m["vision"])
        self.assertNotIn("mmproj", m)                                   # the conversion runs with --no-vision
        self.assertIn("F16", m["vision_problem"]["en"])
        self.assertTrue(any("without image input" in n["en"] for n in m["notes"]))
        (x,) = [x for x in listing["models"] if x["role"] == "mmproj"]
        self.assertIn("does not read", x["reason"]["en"])

    def test_a_basalt_with_an_f16_projector(self):
        good = tiny_basalt(self.root / "good.basalt")
        bad = tiny_basalt(self.root / "bad.basalt", projector=F16)
        self.assertIsNone(models.embedded_projector_problem(str(good)))
        self.assertIn("F16", models.embedded_projector_problem(str(bad))["en"])
        listing = models.scan([str(self.root)])
        items = {Path(x["path"]).name: x for x in listing["models"]}
        self.assertTrue(items["good.basalt"]["vision"])
        self.assertFalse(items["bad.basalt"]["vision"])
        self.assertEqual(items["bad.basalt"]["status"], "ready")         # still loads, without images
        self.assertTrue(any("image input" in n["en"] for n in items["bad.basalt"]["notes"]))


class TheDefaults(Isolated):
    def facts(self, projector):
        return loader.model_facts(str(tiny_basalt(self.root / f"m-{projector}.basalt", projector=projector)))

    def test_safe_values_turn_images_off_and_say_why(self):
        facts = {**loader.DEMO_FACTS, "vision_problem": {"it": "112 tensori sono F16", "en": "112 tensors are F16"}}
        why = {}
        self.assertEqual(loader.safe(facts, ROOMY, why=why)["server.vision"], "off")
        self.assertIn("112 tensors are F16", why["server.vision"][0])
        self.assertIn("112 tensori sono F16", why["server.vision"][1])
        self.assertEqual(loader.safe(loader.DEMO_FACTS, ROOMY)["server.vision"], "dynamic")

    def test_a_plan_with_images_on_is_refused(self):
        facts = {**loader.DEMO_FACTS, "vision_problem": {"it": "F16", "en": "F16"}}
        v = {**loader.safe(loader.DEMO_FACTS, ROOMY), "server.vision": "dynamic"}
        p = loader.plan(v, facts, ROOMY)
        self.assertFalse(p["fits"])
        (x,) = [x for x in p["problems"] if x["code"] == "vision"]
        self.assertIn("would stop at start", loader.problem_text(x, "en"))
        self.assertIn("«off»", loader.problem_text(x, "it"))
        self.assertTrue(loader.plan({**v, "server.vision": "off"}, facts, ROOMY)["fits"])

    def test_the_model_facts_carry_it(self):
        self.assertIsNone(self.facts(BF16)["vision_problem"])
        self.assertIn("F16", self.facts(F16)["vision_problem"]["en"])


if __name__ == "__main__":
    unittest.main()
