"""A vision projector the engine's image encoder reads (src/vision/mmproj.cpp, encoder_*.cpp: a qwen3vl_merger with BF16
matrices and F32 vectors) and one it would refuse. F32, F16 and BF16 tensors in the wrong places are not refused: the load
rewrites them (serve/projector.py), and the scan, the conversion and the plan treat the projector as usable and a little larger.
What a rewrite does not solve (another layout or size, quantized tensors) is said from the header, the conversion leaves it out,
and "Valori sicuri", the wizard and a plan do not turn image input on with it. The first real load of an F16 projector stopped
the engine at start with "tensor mm.2.weight has an unexpected type or size"."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import loader, models
from serve.tests.test_models import BF16, F16, F32, Q8_0, projector_gguf, tiny_basalt, tiny_qwen

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

    def test_float_tensors_in_other_types_are_rewritten_not_refused(self):
        for kw in ({"mat": F16, "vec": F16}, {"vec": BF16}, {"mat": F32}):
            self.assertIsNone(self.problem(**kw), kw)
        g = models.read_gguf(str(projector_gguf(self.root / "f.gguf", mat=F16, vec=F16)))
        fix = models.projector_fix(g)
        self.assertEqual((fix["tensors"], fix["from"]), (22, ["F16"]))
        self.assertEqual(models.projector_fix(models.read_gguf(str(projector_gguf(self.root / "g.gguf"))))["tensors"], 0)

    def test_quantized_tensors_are_refused_with_the_reason(self):
        p = self.problem(mat=Q8_0)
        self.assertIn("Q8_0", p["en"])
        self.assertIn("mm.2.weight", p["en"])
        self.assertIn("BF16", p["it"])

    def test_another_layout_or_size(self):
        self.assertIn("qwen3vl_merger", self.problem(**{"clip.projector_type": "mlp"})["en"])
        self.assertIn("embedding_length", self.problem(**{"clip.vision.embedding_length": 1024})["en"])
        bad = models.read_gguf(str(projector_gguf(self.root / "q.gguf")))
        bad["tensors"] = [(n, [7] if n == "mm.2.bias" else s, t, o) for n, s, t, o in bad["tensors"]]
        self.assertIn("mm.2.bias", models.projector_problem(bad)["en"])


class TheScanAndTheConversion(Isolated):
    def test_an_unreadable_projector_in_the_folder_is_left_out(self):
        models_dir = self.root / "models"
        models_dir.mkdir()
        with mock.patch.dict(models.GEOMETRY, {"n_embd": 256, "n_ff": 64, "n_layer": 2, "n_expert_max": 4}):
            tiny_qwen(models_dir)
            projector_gguf(models_dir / "mmproj-Tiny-q8.gguf", mat=Q8_0)
            listing = models.scan([str(models_dir)])
        (m,) = [x for x in listing["models"] if x["role"] == "model"]
        self.assertEqual(m["status"], "convert", m["reason"])
        self.assertFalse(m["vision"])
        self.assertNotIn("mmproj", m)                                   # the conversion runs with --no-vision
        self.assertIn("Q8_0", m["vision_problem"]["en"])
        self.assertTrue(any("without image input" in n["en"] for n in m["notes"]))
        (x,) = [x for x in listing["models"] if x["role"] == "mmproj"]
        self.assertIn("does not read", x["reason"]["en"])

    def test_an_f16_projector_in_the_folder_goes_into_the_conversion(self):
        models_dir = self.root / "models"
        models_dir.mkdir()
        with mock.patch.dict(models.GEOMETRY, {"n_embd": 256, "n_ff": 64, "n_layer": 2, "n_expert_max": 4}):
            tiny_qwen(models_dir)
            projector_gguf(models_dir / "mmproj-Tiny-f16.gguf", mat=F16, vec=F16)
            listing = models.scan([str(models_dir)])
        (m,) = [x for x in listing["models"] if x["role"] == "model"]
        self.assertTrue(m["vision"])
        self.assertTrue(m["mmproj"].endswith("mmproj-Tiny-f16.gguf"))
        self.assertFalse(m.get("vision_problem"))

    def test_a_basalt_with_an_f16_projector_keeps_its_images_and_one_with_a_quantized_one_loses_them(self):
        good = tiny_basalt(self.root / "good.basalt")
        f16 = tiny_basalt(self.root / "f16.basalt", projector=F16)
        bad = tiny_basalt(self.root / "bad.basalt", projector=Q8_0)
        self.assertIsNone(models.embedded_projector_problem(str(good)))
        self.assertIsNone(models.embedded_projector_problem(str(f16)))
        self.assertGreater(models.embedded_projector_fix(str(f16))["tensors"], 0)
        self.assertIn("Q8_0", models.embedded_projector_problem(str(bad))["en"])
        listing = models.scan([str(self.root)])
        items = {Path(x["path"]).name: x for x in listing["models"]}
        self.assertTrue(items["good.basalt"]["vision"])
        self.assertTrue(items["f16.basalt"]["vision"])
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
        good, f16, bad = self.facts(BF16), self.facts(F16), self.facts(Q8_0)
        self.assertEqual((good["vision_problem"], good["vision_fix"]), (None, None))
        self.assertEqual(f16["vision_problem"], None)
        self.assertGreater(f16["vision_fix"]["tensors"], 0)
        self.assertEqual(f16["mmproj_bytes"], f16["vision_fix"]["bytes"])           # the rewritten file is the one held in RAM
        self.assertIn("Q8_0", bad["vision_problem"]["en"])


class TheChoice(Isolated):
    """Which projector image input reads with: the model file's own, or a file of the models folders."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(models.GEOMETRY, {"n_embd": 256})              # the test projectors are 256 wide
        patch.start()
        self.addCleanup(patch.stop)

    def files(self):
        folder = self.root / "projectors"
        folder.mkdir()
        projector_gguf(folder / "mmproj-good-BF16.gguf")
        projector_gguf(folder / "mmproj-other-F16.gguf", mat=F16, vec=F16)
        projector_gguf(folder / "mmproj-q8.gguf", mat=Q8_0)
        projector_gguf(folder / "mmproj-wide.gguf", proj=512)               # another model's: its width is not this model's
        return folder

    def test_the_folder_lists_the_projectors_the_engine_can_use_with_what_each_keeps_in_ram(self):
        folder = self.files()
        found = models.projector_files([str(folder)], ttl=0)
        by = {p["name"]: p for p in found}
        self.assertEqual(sorted(by), ["mmproj-good-BF16.gguf", "mmproj-other-F16.gguf"])
        self.assertEqual(by["mmproj-good-BF16.gguf"]["fix"], 0)
        self.assertEqual(by["mmproj-other-F16.gguf"]["fix"], 22)
        self.assertEqual([p["name"] for p in found][0], "mmproj-good-BF16.gguf")             # the one read as it is comes first

    def facts(self, **kw):
        return {**loader.DEMO_FACTS, **kw}

    def test_auto_takes_the_models_own_projector_when_it_reads_else_a_file(self):
        file = {"path": "E:/p/mmproj-BF16.gguf", "name": "mmproj-BF16.gguf", "bytes": 907542944, "fix": 0, "from": []}
        own = loader.pick_projector("auto", self.facts(projectors=[file]))
        self.assertEqual(own["value"], "embedded")
        bad = {"it": "F16", "en": "Q8_0"}
        picked = loader.pick_projector("auto", self.facts(vision_problem=bad, projectors=[file]))
        self.assertEqual((picked["value"], picked["bytes"]), (file["path"], 907542944))
        none = loader.pick_projector("auto", self.facts(mmproj_bytes=0, projectors=[]))
        self.assertIsNone(none)
        self.assertEqual(loader.pick_projector("embedded", self.facts(vision_problem=bad, projectors=[file]))["problem"], bad)
        self.assertEqual(loader.pick_projector(file["path"], self.facts(vision_problem=bad, projectors=[file]))["value"], file["path"])

    def test_a_plan_counts_the_chosen_projectors_ram_and_a_missing_file_is_refused(self):
        file = {"path": "E:/p/mmproj-BF16.gguf", "name": "mmproj-BF16.gguf", "bytes": 907542944, "fix": 0, "from": []}
        bad = {"it": "Q8_0", "en": "Q8_0"}
        facts = self.facts(vision_problem=bad, projectors=[file])
        v = {**loader.safe(loader.DEMO_FACTS, ROOMY), "server.vision": "dynamic"}
        p = loader.plan(v, facts, ROOMY)
        self.assertTrue(p["fits"], p["problems"])
        self.assertEqual(p["projector"]["value"], file["path"])
        self.assertEqual([i["bytes"] for i in p["ram"]["items"] if i["key"] == "vision"], [907542944])
        self.assertFalse(loader.plan({**v, "server.vision-projector": "embedded"}, facts, ROOMY)["fits"])
        gone = loader.plan({**v, "server.vision-projector": str(self.root / "nowhere.gguf")}, facts, ROOMY)
        self.assertFalse(gone["fits"])
        self.assertIn("is not there", [x for x in gone["problems"] if x["code"] == "vision"][0]["en"])
        nothing = loader.plan(v, self.facts(mmproj_bytes=0, projectors=[]), ROOMY)
        self.assertIn("no vision projector", [x for x in nothing["problems"] if x["code"] == "vision"][0]["en"])
        self.assertTrue(loader.plan({**v, "server.vision": "off"}, self.facts(mmproj_bytes=0, projectors=[]), ROOMY)["fits"])

    def test_safe_values_turn_images_on_with_a_file_when_the_models_own_is_unusable(self):
        file = {"path": "E:/p/mmproj-BF16.gguf", "name": "mmproj-BF16.gguf", "bytes": 907542944, "fix": 0, "from": []}
        facts = self.facts(vision_problem={"it": "Q8_0", "en": "Q8_0"}, projectors=[file])
        self.assertEqual(loader.safe(facts, ROOMY)["server.vision"], "dynamic")
        self.assertEqual(loader.safe({**facts, "projectors": []}, ROOMY)["server.vision"], "off")

    def test_the_setting_is_checked_and_reaches_the_server_as_an_option(self):
        from serve import config, settings
        self.assertEqual(settings.check_value("server", "vision-projector", " AUTO "), "auto")
        self.assertEqual(settings.check_value("server", "vision-projector", "E:/p/x.gguf"), "E:/p/x.gguf")
        for bad in ("", 3, None, "a" + chr(10) + "b"):
            with self.assertRaises(ValueError):
                settings.check_value("server", "vision-projector", bad)
        self.assertIn(("server", "vision-projector"), settings.LOADER_FIELDS)
        cfg = self.root / "c.json"
        cfg.write_text('{"server": {"vision": "dynamic", "vision-projector": "embedded"}}', encoding="utf-8")
        a = config.parse_settings(config.build_parser(), ["--config", str(cfg)])[0]
        self.assertEqual((a.vision, a.vision_projector), ("dynamic", "embedded"))
        self.assertEqual(config.parse_settings(config.build_parser(), [])[0].vision_projector, "auto")

    def test_releasing_the_projector_takes_it_out_of_the_steady_need_and_says_so(self):
        v = {**loader.safe(loader.DEMO_FACTS, ROOMY), "server.vision": "dynamic"}
        keep = loader.plan(v, loader.DEMO_FACTS, ROOMY)
        free = loader.plan({**v, "server.vision-hold": "release"}, loader.DEMO_FACTS, ROOMY)
        held = loader.DEMO_FACTS["mmproj_bytes"]
        self.assertEqual(keep["vision_hold"], {"release": False, "bytes": 0})
        self.assertEqual(free["vision_hold"], {"release": True, "bytes": held})
        self.assertEqual(keep["ram"]["need"] - free["ram"]["need"], held)
        self.assertEqual(keep["commit"]["need"] - free["commit"]["need"], held)
        self.assertEqual([i["bytes"] for i in free["ram"]["items"] if i["key"] == "vision"], [0])
        other = loader.plan({**v, "server.vision": "cpu", "server.vision-hold": "release"}, loader.DEMO_FACTS, ROOMY)
        self.assertFalse(other["vision_hold"]["release"])                     # only the engine's own encoder lets go of it

    def test_the_cpu_encoder_keeps_the_projector_in_ram_and_the_sidecar_gives_it_back(self):
        v = {**loader.safe(loader.DEMO_FACTS, ROOMY), "server.vision": "cpu"}
        held = loader.DEMO_FACTS["mmproj_bytes"]
        cpu = loader.plan(v, loader.DEMO_FACTS, ROOMY)
        off = loader.plan({**v, "server.vision": "off"}, loader.DEMO_FACTS, ROOMY)
        self.assertEqual(cpu["ram"]["need"] - off["ram"]["need"], held)
        from types import SimpleNamespace
        from serve import loader_api
        me = SimpleNamespace(proc=SimpleNamespace(pid=1), alive=lambda: True)
        fixed = SimpleNamespace(rss=100, private=300)
        with mock.patch("psutil.Process", lambda pid: SimpleNamespace(memory_info=lambda: fixed)):
            both = loader_api._holds(SimpleNamespace(engine=me, vision=me, settings_info={}, gpu_index=0), None)[0]
            alone = loader_api._holds(SimpleNamespace(engine=me, vision=None, settings_info={}, gpu_index=0), None)[0]
        self.assertEqual((alone["ram"], alone["commit"]), (100, 300))
        self.assertEqual((both["ram"], both["commit"]), (200, 600))

    def test_the_hold_is_checked_and_reaches_the_server_as_an_option(self):
        from serve import config, settings
        self.assertEqual(settings.check_value("server", "vision-hold", " Release "), "release")
        for bad in ("", "never", 1, None):
            with self.assertRaises(ValueError):
                settings.check_value("server", "vision-hold", bad)
        self.assertIn(("server", "vision-hold"), settings.LOADER_FIELDS)
        cfg = self.root / "c.json"
        cfg.write_text('{"server": {"vision": "dynamic", "vision-hold": "release"}}', encoding="utf-8")
        a = config.parse_settings(config.build_parser(), ["--config", str(cfg)])[0]
        self.assertEqual(a.vision_hold, "release")
        self.assertEqual(config.parse_settings(config.build_parser(), [])[0].vision_hold, "keep")

    def test_the_load_hands_the_engine_the_file_chosen(self):
        folder = self.files()
        model = tiny_basalt(self.root / "m.basalt", projector=Q8_0)
        with mock.patch.object(models, "dirs", lambda: [str(folder)]), mock.patch.dict(models._PROJECTORS, {"key": None}):
            self.assertEqual(loader.projector_path("auto", str(model)), models._posix(str(folder / "mmproj-good-BF16.gguf")))
            with self.assertRaises(ValueError):
                loader.projector_path("embedded", str(model))
            with self.assertRaises(ValueError):
                loader.projector_path(str(folder / "mmproj-q8.gguf"), str(model))
        good = tiny_basalt(self.root / "g.basalt")
        with mock.patch.object(models, "dirs", lambda: [str(folder)]), mock.patch.dict(models._PROJECTORS, {"key": None}):
            self.assertIsNone(loader.projector_path("auto", str(good)))          # its own, unpacked from the file


if __name__ == "__main__":
    unittest.main()
