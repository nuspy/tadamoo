"""The real defaults: a model's chat values from its source GGUF (general.sampling.*), and the default load profile
and inference preset ("Machine Default" from the configuration wizard, else Valori sicuri / the model's own)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from model_file import ModelWriter, gguf_sampling, meta_sampling  # noqa: E402
from serve import loader, models, profiles  # noqa: E402
from serve.engine import modelfile  # noqa: E402
from serve.tests.test_models import Base, tiny_basalt  # noqa: E402

ORCA = {"general.sampling.temp": 1.0, "general.sampling.top_p": 0.949999988079071, "general.sampling.top_k": 20}


class FromTheGguf(unittest.TestCase):
    def test_the_keys_and_their_checks(self):
        self.assertEqual(gguf_sampling(ORCA), {"temperature": 1.0, "top_p": 0.95, "top_k": 20})
        self.assertEqual(gguf_sampling({"general.sampling.top_k": 0, "general.sampling.top_p": 0,
                                        "general.sampling.temp": -1, "general.sampling.min_p": 0.05,
                                        "general.sampling.penalty_repeat": 1.1,
                                        "general.sampling.penalty_last_n": 128}),
                         {"min_p": 0.05, "repetition_penalty": 1.1, "penalty_last_n": 128})
        self.assertEqual(gguf_sampling({"general.sampling.temp": "hot", "general.sampling.top_k": True}), {})
        self.assertEqual(gguf_sampling({}), {})

    def test_the_converter_copies_them(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "convert"))
        try:
            import convert
        except ImportError as e:                          # numpy missing: the converter cannot run here
            self.skipTest(str(e))
        block, source = convert.model_sampling({**ORCA, "general.sampling.top_k": 40})
        self.assertEqual(source, "gguf")
        self.assertEqual((block["temperature"], block["top_p"], block["top_k"]), (1.0, 0.95, 40))
        self.assertEqual(block["presence_penalty"], 0.0)          # a key the GGUF does not name keeps the fallback
        self.assertEqual(convert.model_sampling({}), (convert.SAMPLING, "converter"))

    def test_a_file_converted_before(self):
        old = {"sampling": {"temperature": 0.6, "top_p": 0.9, "top_k": 50}, "gguf_kv": dict(ORCA)}
        self.assertEqual(meta_sampling(old), ({"temperature": 1.0, "top_p": 0.95, "top_k": 20}, "gguf"))
        self.assertEqual(meta_sampling({"sampling": {"temperature": 0.6}, "gguf_kv": {}}), ({"temperature": 0.6}, "file"))
        new = {"sampling": {"temperature": 0.7}, "sampling_source": "gguf", "gguf_kv": dict(ORCA)}
        self.assertEqual(meta_sampling(new), ({"temperature": 0.7}, "gguf"))   # written by the new converter: as is


def basalt_with(path, meta_extra):
    w = ModelWriter(str(path))
    meta = {"format": "basalt", "version": 1, "name": "S", "gguf_kv": {}, "sources": [], "tensors": {}, **meta_extra}
    w.add_bytes("meta", json.dumps(meta).encode())
    for name, data in (("dense", b"\1" * 64), ("experts", b"\2" * 64), ("profile", b"\0" * 64),
                       ("experts.layout", b"# n_expert 4\n0 18 20 0 16\n")):
        w.add_bytes(name, data)
    w.close()
    return Path(path)


class InTheServer(Base):
    def test_the_model_file_gives_the_server_and_the_loader_its_values(self):
        p = basalt_with(self.models / "s.basalt", {"sampling": {"temperature": 0.6, "top_k": 50}, "gguf_kv": dict(ORCA)})
        self.assertEqual(loader.model_facts(str(p))["sampling"], {"temperature": 1.0, "top_k": 20, "top_p": 0.95})
        models.set_dirs([str(self.models)])
        item = next(m for m in models.listing(True)["models"] if m["path"].endswith("s.basalt"))
        self.assertEqual(item["sampling"]["top_p"], 0.95)
        self.assertEqual(item["sampling_source"], "gguf")
        with mock.patch.object(modelfile, "extract_tokenizer", return_value=self.root):
            cfg = modelfile.model_config(str(p), "basalt.exe", gpus=1)
        self.assertEqual(cfg["sampling"], {"temperature": 1.0, "top_p": 0.95, "top_k": 20})


class DefaultProfile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": self.dir.name, "BASALT_DATA": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def save(self, name, kind="load"):
        body = {"values": {"engine.kv": "q4_0"}} if kind == "load" else {"settings": {"temperature": 0.7}}
        profiles.change({"action": "save", "kind": kind, "name": name, **body})

    def test_valori_sicuri_without_a_machine_default(self):
        self.assertEqual(profiles.default_name(), profiles.SAFE_NAME)
        self.assertIsNone(profiles.listing()["inference_default"])

    def test_machine_default_once_the_wizard_saved_it(self):
        self.save("Altro")
        self.save(profiles.MACHINE_NAME)
        self.assertEqual(profiles.default_name(), profiles.MACHINE_NAME)
        self.save(profiles.MACHINE_NAME, "inference")
        self.assertEqual(profiles.listing()["inference_default"], profiles.MACHINE_NAME)

    def test_a_profile_chosen_by_hand_stays_the_default(self):
        self.save("Predefinito")
        profiles.change({"action": "default", "kind": "load", "name": "Predefinito"})
        self.save(profiles.MACHINE_NAME)
        self.assertEqual(profiles.default_name(), "Predefinito")
        profiles.change({"action": "delete", "kind": "load", "name": "Predefinito"})
        self.assertEqual(profiles.default_name(), profiles.MACHINE_NAME)     # its default gone: Machine Default
        profiles.change({"action": "delete", "kind": "load", "name": profiles.MACHINE_NAME})
        self.assertEqual(profiles.default_name(), profiles.SAFE_NAME)

    def test_an_old_file_keeps_its_default(self):
        Path(self.dir.name, "profiles.json").write_text(json.dumps(
            {"load": {"Predefinito": {"values": {}}}, "inference": {}, "default": "Predefinito"}), encoding="utf-8")
        self.save(profiles.MACHINE_NAME)
        self.assertEqual(profiles.default_name(), "Predefinito")
        self.assertEqual(profiles.listing()["default"], "Predefinito")


if __name__ == "__main__":
    unittest.main()
