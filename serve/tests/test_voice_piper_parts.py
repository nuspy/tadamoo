"""Piper is a set of independent voices: the models catalogue, the voice service and the engine agree on what "installed" means for it (at least one
voice with its .onnx and .onnx.json), and the catalogue says how many of the voices there are."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from serve import voice_catalog

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "serve" / "web"


def service_side():
    sys.path.insert(0, str(ROOT / "voice"))
    try:
        from callbridge import models_store
        from callbridge.engines import catalog, tts_router
        from callbridge.engines.piper_tts import PiperTtsEngine
        from callbridge.config import Config
    finally:
        sys.path.pop(0)
    return models_store, catalog, tts_router, PiperTtsEngine, Config


class PiperPartsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.entry = voice_catalog.find("piper")
        self.folder = os.path.join(self.root, *voice_catalog.folder_of(self.entry).split("/"))
        os.makedirs(self.folder)

    def put(self, name, config=True):
        Path(self.folder, name + ".onnx").write_bytes(b"x")
        if config:
            Path(self.folder, name + ".onnx.json").write_bytes(b"{}")

    def everyone(self):
        """(catalogue, voice service's store, engine, router-less availability) as a tuple of booleans."""
        store, catalog, router, Engine, Config = service_side()
        cfg = Config()
        cfg.kokoro_model_dir = os.path.join(self.root, "tts", "kokoro")
        cfg.piper_model_dir = self.folder
        spec = catalog.spec("piper")
        return (voice_catalog.installed(self.entry, self.root), store.is_installed("piper", self.root), Engine(self.folder).available,
                router.disponibile_senza_router(cfg, spec))

    def test_nothing_installed_is_not_installed_for_all_of_them(self):
        self.assertEqual(self.everyone(), (False, False, False, False))

    def test_one_complete_voice_is_installed_for_all_of_them_and_the_catalogue_counts_it(self):
        self.put("it_IT-riccardo-x_low")
        self.assertEqual(self.everyone(), (True, True, True, True))
        public = voice_catalog.public(self.entry, self.root)
        self.assertTrue(public["installed"])
        self.assertEqual(public["parts"], {"installed": ["it_IT-riccardo-x_low"], "total": 4})

    def test_a_voice_without_its_config_does_not_count_and_cannot_speak(self):
        self.put("it_IT-riccardo-x_low", config=False)
        self.assertEqual(self.everyone(), (False, False, False, False))
        store, _, _, Engine, _ = service_side()
        self.assertFalse(Engine(self.folder).has_voice("it_IT-riccardo-x_low"))
        self.assertEqual(voice_catalog.public(self.entry, self.root)["parts"], {"installed": [], "total": 4})

    def test_all_the_voices_are_installed_when_all_four_are_there(self):
        for v in self.entry["probe"]:
            self.put(v[:-len(".onnx")])
        public = voice_catalog.public(self.entry, self.root)
        self.assertTrue(public["installed"] and len(public["parts"]["installed"]) == public["parts"]["total"] == 4)

    def test_other_entries_have_no_parts_and_keep_their_rule(self):
        public = voice_catalog.public(voice_catalog.find("kokoro"), self.root)
        self.assertIsNone(public["parts"])
        self.assertFalse(public["installed"])

    def test_the_page_says_how_many_voices_and_keeps_the_download_for_the_rest(self):
        js = (WEB / "voice.js").read_text(encoding="utf-8")
        i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
        self.assertIn("vo.state.partial", js)
        self.assertEqual(i18n.count('"vo.state.partial":'), 2)
        self.assertIn("if (!m.installed || partial) box.append(downloadPart(svc, m))", js)


if __name__ == "__main__":
    unittest.main()
