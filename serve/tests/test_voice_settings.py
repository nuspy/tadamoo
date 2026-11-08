"""voice.json: the models folder, the model chosen per service and where it is loaded."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import models, userdata, voice_settings as vs


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.root / "data"), "TADAMOO_CACHE": str(self.root / "cache")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def stored(self):
        return json.loads((self.root / "data" / "voice.json").read_text(encoding="utf-8"))


class ModelsDirTest(Base):
    def test_without_a_models_folder_the_default_is_in_the_user_data(self):
        self.assertEqual(vs.models_dir(), str(self.root / "data" / "voice-models"))

    def test_with_a_models_folder_the_default_is_voice_inside_the_first(self):
        first = self.root / "models"
        first.mkdir()
        with mock.patch.object(models, "dirs", return_value=[str(first), str(self.root / "other")]):
            self.assertEqual(vs.models_dir(), str(first / "voice"))
            self.assertEqual(vs.default_models_dir(), str(first / "voice"))

    def test_a_chosen_folder_is_kept_and_nothing_is_moved_or_created(self):
        old = self.root / "old"
        (old / "tts" / "kokoro").mkdir(parents=True)
        (old / "tts" / "kokoro" / "x.bin").write_bytes(b"x")
        new = self.root / "fresh" / "voice"
        self.assertEqual(vs.set_models_dir(str(new)), str(new))
        self.assertEqual(vs.models_dir(), str(new))
        self.assertFalse(new.exists())                                                  # creatable, not created
        self.assertTrue((old / "tts" / "kokoro" / "x.bin").is_file())
        self.assertEqual(self.stored()["models_dir"], str(new))

    def test_bad_folders_are_refused_with_a_code(self):
        a_file = self.root / "f.txt"
        a_file.write_text("x")
        for raw, code in (("", "path"), (None, "path"), (7, "path"), ("relative/dir", "path_relative"), (str(a_file), "path_file"),
                          (os.path.abspath(os.sep), "path_root"), ("C:\\a\0b", "path")):
            with self.assertRaises(vs.VoiceSettingsError, msg=repr(raw)) as c:
                vs.set_models_dir(raw)
            self.assertEqual(c.exception.code, code, repr(raw))
            self.assertTrue(c.exception.it)
        self.assertFalse((self.root / "data" / "voice.json").exists())

    def test_a_folder_under_a_file_cannot_be_created(self):
        a_file = self.root / "f.txt"
        a_file.write_text("x")
        with self.assertRaises(vs.VoiceSettingsError) as c:
            vs.set_models_dir(str(a_file / "sub"))
        self.assertEqual(c.exception.code, "path_uncreatable")

    def test_a_damaged_file_falls_back_to_the_default(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "voice.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(vs.models_dir(), vs.default_models_dir())
        self.assertEqual(vs.selected("tts"), "kokoro")


class SelectionTest(Base):
    def test_defaults(self):
        self.assertEqual((vs.selected("stt"), vs.selected("tts"), vs.selected("embedding")), ("whisper-small", "kokoro", ""))
        self.assertEqual(vs.placement("stt"), {"device": "auto", "memory": "vram"})
        self.assertEqual(vs.placement("diarization"), {"device": "cpu", "memory": "ram"})

    def test_a_choice_is_kept_per_service(self):
        vs.select("tts", "xtts-v2")
        vs.select("stt", "whisper-large-v3")
        self.assertEqual((vs.selected("tts"), vs.selected("stt")), ("xtts-v2", "whisper-large-v3"))
        self.assertEqual(self.stored()["services"]["tts"], {"selected": "xtts-v2"})

    def test_a_model_of_another_service_or_unknown_is_refused(self):
        for service, model, code in (("tts", "whisper-small", "model"), ("tts", "nope", "model"), ("nope", "kokoro", "service"),
                                     ("stt", None, "model"), (None, "kokoro", "service")):
            with self.assertRaises(vs.VoiceSettingsError, msg=(service, model)) as c:
                vs.select(service, model)
            self.assertEqual(c.exception.code, code)
        self.assertFalse((self.root / "data" / "voice.json").exists())

    def test_a_stored_model_that_left_the_catalog_gives_the_default(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "voice.json").write_text(json.dumps({"services": {"tts": {"selected": "gone"}, "stt": {"selected": "kokoro"}}}), encoding="utf-8")
        self.assertEqual((vs.selected("tts"), vs.selected("stt")), ("kokoro", "whisper-small"))

    def test_placement_is_validated(self):
        self.assertEqual(vs.set_placement("tts", "cuda:1", "vram"), {"device": "cuda:1", "memory": "vram"})
        self.assertEqual(vs.placement("tts"), {"device": "cuda:1", "memory": "vram"})
        self.assertEqual(vs.set_placement("tts", "cpu", "ram"), {"device": "cpu", "memory": "ram"})
        self.assertEqual(vs.set_placement("stt", "auto", "ram"), {"device": "auto", "memory": "ram"})
        for service, device, memory, code in (("tts", "gpu", "vram", "device"), ("tts", "cuda:", "vram", "device"), ("tts", "cuda:100", "vram", "device"),
                                              ("tts", None, "vram", "device"), ("tts", "auto", "disk", "memory"), ("tts", "cpu", "vram", "cpu_vram"),
                                              ("nope", "auto", "vram", "service")):
            with self.assertRaises(vs.VoiceSettingsError, msg=(device, memory)) as c:
                vs.set_placement(service, device, memory)
            self.assertEqual(c.exception.code, code)
        self.assertEqual(vs.placement("tts"), {"device": "cpu", "memory": "ram"})          # refusals changed nothing

    def test_the_file_is_written_whole_and_leaves_no_temporary(self):
        vs.select("tts", "kokoro")
        vs.set_placement("tts", "auto", "vram")
        self.assertEqual(sorted(p.name for p in (self.root / "data").iterdir() if p.name.startswith("voice")), ["voice.json"])
        self.assertEqual(self.stored()["services"]["tts"], {"selected": "kokoro", "device": "auto", "memory": "vram"})

    def test_a_hand_edited_bad_placement_is_read_as_the_default(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "voice.json").write_text(json.dumps({"services": {"tts": {"device": "tpu", "memory": "x"}}}), encoding="utf-8")
        self.assertEqual(vs.placement("tts"), {"device": "auto", "memory": "vram"})

    def test_userdata_path_is_the_one_used(self):
        self.assertEqual(vs._file(), userdata.path("voice.json"))


if __name__ == "__main__":
    unittest.main()
