"""The catalog of voice models: its shape, what is grounded in the voice code, what is marked to verify and what may be downloaded."""
import os
import re
import tempfile
import unittest
from pathlib import Path

from serve import voice_catalog as vc

ROOT = Path(__file__).resolve().parents[2]


class CatalogShapeTest(unittest.TestCase):
    def test_every_entry_has_the_fields_the_page_and_the_downloader_read(self):
        for e in vc.CATALOG:
            for key in ("id", "service", "engine", "name", "family", "size", "license", "noncommercial", "sources", "languages", "notes", "venv", "verify", "probe"):
                self.assertIn(key, e, e["id"])
            self.assertIn(e["service"], vc.SERVICES)
            self.assertIn(e["license"], vc.LICENSES)
            self.assertTrue(e["notes"]["en"] and e["notes"]["it"], e["id"])
            self.assertTrue(e["venv"]["en"] and e["venv"]["it"], e["id"])
            self.assertTrue(re.match(r"^[a-z0-9][a-z0-9.-]{0,40}$", e["id"]))

    def test_every_license_has_both_languages(self):
        for key, lic in vc.LICENSES.items():
            self.assertTrue(lic["name"] and lic["en"] and lic["it"], key)

    def test_the_services_and_engines_of_the_plan_are_all_there(self):
        engines = {(e["service"], e["engine"]) for e in vc.CATALOG}
        for want in [("tts", n) for n in ("kokoro", "piper", "chatterbox", "xtts", "cosyvoice", "qwen3tts", "orpheus", "dia")] + [
                ("avatar", "musetalk"), ("diarization", "sherpa-onnx"), ("vad", "silero"), ("stt", "whisper")]:
            self.assertIn(want, engines)
        self.assertEqual({e["id"] for e in vc.models_of("stt")}, {f"whisper-{s}" for s in ("tiny", "base", "small", "medium", "large-v3")})

    def test_the_embedding_service_has_no_entry_because_the_voice_code_names_no_model(self):
        self.assertEqual(vc.models_of("embedding"), [])
        self.assertEqual(vc.DEFAULTS["embedding"], "")

    def test_the_defaults_name_models_of_their_own_service(self):
        for service, model in vc.DEFAULTS.items():
            if model:
                self.assertEqual(vc.find(model)["service"], service)

    def test_the_tts_engines_match_the_voice_services_own_catalog(self):
        text = (ROOT / "voice/callbridge/engines/catalog.py").read_text(encoding="utf-8")
        ids = set(re.findall(r'EngineSpec\(\s*id="(\w+)"', text))
        self.assertEqual({e["engine"] for e in vc.models_of("tts")}, ids)
        langs = {m.group(1): re.findall(r'"(\w\w)"', m.group(2)) for m in
                 re.finditer(r'id="(xtts|cosyvoice|qwen3tts|orpheus|dia|chatterbox)".*?langs=\(([^)]*)\)', text, re.S)}
        for e in vc.models_of("tts"):
            if e["engine"] in langs:
                self.assertEqual(sorted(e["languages"]), sorted(langs[e["engine"]]), e["id"])


class GroundingTest(unittest.TestCase):
    def test_the_repositories_are_the_ones_the_voice_code_names(self):
        code = "".join((ROOT / "voice/callbridge/engines" / n).read_text(encoding="utf-8") for n in
                       ("cosyvoice_tts_helper.py", "xtts_tts_helper.py", "dia_tts_helper.py", "orpheus_tts_helper.py", "qwen3tts_tts_helper.py"))
        code += (ROOT / "voice/avatar/musetalk/download_weights.sh").read_text(encoding="utf-8")
        for e in vc.CATALOG:
            for s in e["sources"]:
                if s["type"] != "hf" or e["kind"] == "engine-pack":                  # the packs' repositories are checked in test_avatar_engine_packs.py
                    continue
                repo = s["repo"]
                if repo.startswith("Qwen/Qwen3-TTS-12Hz-"):
                    self.assertIn("Qwen/Qwen3-TTS-12Hz-", code)
                    self.assertRegex(repo, r"^Qwen/Qwen3-TTS-12Hz-(0\.6B|1\.7B)-(Base|CustomVoice)$")
                else:
                    self.assertIn(repo, code, repo)
                self.assertEqual(s["revision"], "main")

    def test_the_whisper_addresses_carry_the_sha256_of_the_file(self):
        for e in vc.models_of("stt"):
            (s,) = e["sources"]
            self.assertEqual(s["url"].split("/")[-2], s["sha256"])
            self.assertTrue(s["url"].endswith("/" + s["name"]))
            self.assertRegex(s["sha256"], r"^[0-9a-f]{64}$")

    def test_the_kokoro_and_diarization_files_are_the_ones_the_voice_code_names(self):
        names = {s["name"] for e in vc.CATALOG for s in e["sources"] if s["type"] == "url"}
        for name in ("kokoro-v1.0.fp16.onnx", "voices-v1.0.bin", "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
                     "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"):
            self.assertIn(name, names)
        urls = [s["url"] for e in vc.CATALOG for s in e["sources"] if s["type"] == "url" and e["kind"] == "model" and not s["url"].startswith(vc._WHISPER_HOST)]
        self.assertEqual(len(urls), 4)
        for u in urls:
            self.assertTrue(u.startswith("https://github.com/"))

    def test_a_size_is_given_only_where_the_voice_code_states_it(self):
        sized = {e["id"] for e in vc.CATALOG if e["size"]}
        packs = {e["id"] for e in vc.CATALOG if e["kind"] == "engine-pack"}                  # their sizes are those of the pinned blobs
        self.assertEqual(sized - packs, {"whisper-large-v3", "kokoro", "xtts-v2", "musetalk-v15", "sherpa-diarization", "wav2lip-int8"})
        for e in vc.CATALOG:
            if e["size"] is None and e["kind"] == "model":
                self.assertIn("size", e["verify"], e["id"])


class LicenseAndVerifyTest(unittest.TestCase):
    def test_the_known_licenses(self):
        want = {"xtts-v2": "cpml-1.0", "cosyvoice-3": "apache-2.0", "chatterbox": "mit", "qwen3tts-0.6b": "apache-2.0", "qwen3tts-1.7b": "apache-2.0",
                "musetalk-v15": "musetalk"}
        for model, lic in want.items():
            self.assertEqual(vc.find(model)["license"], lic)
        for model in ("kokoro", "piper", "orpheus-3b", "dia-1.6b", "silero-vad-v4", "whisper-small", "sherpa-diarization"):
            self.assertEqual(vc.find(model)["license"], "verify")
            self.assertIn("license", vc.find(model)["verify"])

    def test_only_xtts_and_the_wav2lip_file_are_non_commercial(self):
        self.assertEqual([e["id"] for e in vc.CATALOG if e["noncommercial"]], ["xtts-v2", "wav2lip-int8", "wav2lipv2-code", "wav2lipv2-weights"])
        self.assertIn("Non-commercial", vc.LICENSES["cpml-1.0"]["en"])
        self.assertIn("non-commercial", vc.LICENSES["wav2lip-research"]["en"])

    def test_the_wav2lip_file_is_for_the_phones_with_a_known_place_and_hash_and_no_source(self):
        e = vc.find("wav2lip-int8")
        self.assertEqual((e["service"], e["engine"], e["license"], e["noncommercial"], e["selectable"]), ("avatar", "wav2lip", "wav2lip-research", True, False))
        self.assertEqual((e["size"], e["sha256"]), (36664106, "2e297b310dcd397ac94e467814570eaf18edec322c29abd14565dcd3b6896f97"))
        self.assertEqual((e["probe"], vc.folder_of(e)), (["wav2lip_int8.onnx"], "avatar/wav2lip-int8"))
        self.assertEqual(set(e["verify"]), {"license", "source"})                                      # provenance is undeclared: verify is on
        self.assertEqual(vc.downloadable(e), (False, "no_source"))
        self.assertTrue(all(m["selectable"] for m in vc.models_of("avatar") if m["id"] != "wav2lip-int8" and m["kind"] == "model"))
        self.assertEqual(vc.DEFAULTS["avatar"], "musetalk-v15")
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(vc.public(e, root)["installed"])
            os.makedirs(os.path.join(root, "avatar", "wav2lip-int8"))
            open(os.path.join(root, "avatar", "wav2lip-int8", "wav2lip_int8.onnx"), "wb").close()
            self.assertTrue(vc.public(e, root)["installed"])
            self.assertEqual((vc.public(e, root)["selectable"], vc.public(e, root)["sha256"]), (False, e["sha256"]))

    def test_an_entry_the_code_does_not_complete_is_marked_and_not_downloadable(self):
        for model, why in (("piper", "no_source"), ("chatterbox", "no_source"), ("silero-vad-v4", "no_source"), ("kokoro", "no_sha256"),
                           ("sherpa-diarization", "no_sha256")):
            e = vc.find(model)
            self.assertTrue(e["verify"], model)
            self.assertEqual(vc.downloadable(e), (False, why), model)
        self.assertIn("sha256", vc.find("kokoro")["verify"])

    def test_a_url_source_without_a_sha256_or_with_an_archive_is_refused(self):
        e = {"sources": [vc._url("https://x.test/a.bin", "a.bin")]}
        self.assertEqual(vc.downloadable(e), (False, "no_sha256"))
        e = {"sources": [vc._url("https://x.test/a.tgz", "a.tgz", "a" * 64, archive="tar.bz2")]}
        self.assertEqual(vc.downloadable(e), (False, "archive"))
        e = {"sources": [vc._url("https://x.test/a.bin", "a.bin", "a" * 64)]}
        self.assertEqual(vc.downloadable(e), (True, ""))

    def test_the_download_entry_of_a_voice_model_and_of_a_stranger(self):
        d = vc.download_entry("xtts-v2")
        self.assertEqual((d["folder"], d["noncommercial"], d["kind"], d["marker"]), ("tts/xtts-v2", True, "voice", True))
        self.assertIsNone(d["why_not"])
        self.assertEqual(vc.download_entry("kokoro")["why_not"]["code"], "no_sha256")
        self.assertIsNone(vc.download_entry("nope"))
        self.assertIsNone(vc.download_entry(None))


class InstalledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def folder(self, model):
        d = os.path.join(self.root, *vc.folder_of(vc.find(model)).split("/"))
        os.makedirs(d, exist_ok=True)
        return d

    def test_nothing_there_is_not_installed(self):
        self.assertFalse(vc.installed(vc.find("kokoro"), self.root))
        self.assertFalse(vc.installed(vc.find("xtts-v2"), self.root))

    def test_a_model_put_there_by_hand_is_recognised_by_its_probe_files(self):
        d = self.folder("kokoro")
        Path(d, "kokoro-v1.0.fp16.onnx").write_bytes(b"x")
        self.assertFalse(vc.installed(vc.find("kokoro"), self.root))
        Path(d, "voices-v1.0.bin").write_bytes(b"x")
        self.assertTrue(vc.installed(vc.find("kokoro"), self.root))

    def test_the_marker_of_a_finished_download_must_match_the_files(self):
        d = self.folder("xtts-v2")
        Path(d, "model.pth").write_bytes(b"abcd")
        Path(d, vc.MARKER).write_text('{"files": [{"name": "model.pth", "size": 4}, {"name": "sub/config.json", "size": 2}]}', encoding="utf-8")
        self.assertFalse(vc.installed(vc.find("xtts-v2"), self.root))               # a file is missing
        os.makedirs(os.path.join(d, "sub"))
        Path(d, "sub", "config.json").write_bytes(b"{}")
        self.assertTrue(vc.installed(vc.find("xtts-v2"), self.root))
        Path(d, "model.pth").write_bytes(b"abc")                                      # truncated
        self.assertFalse(vc.installed(vc.find("xtts-v2"), self.root))

    def test_a_broken_marker_is_not_installed(self):
        d = self.folder("xtts-v2")
        Path(d, vc.MARKER).write_text("{not json", encoding="utf-8")
        self.assertFalse(vc.installed(vc.find("xtts-v2"), self.root))

    def test_the_public_form_has_no_source_address(self):
        text = repr(vc.public(vc.find("whisper-small"), self.root))
        self.assertNotIn("http", text)
        self.assertNotIn("sources", text)


if __name__ == "__main__":
    unittest.main()
