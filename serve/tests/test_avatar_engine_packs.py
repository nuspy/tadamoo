"""The catalog entries of the PC avatar engines (Wav2Lip v2, SadTalker, ER-NeRF): every file of a pack has its pinned address and SHA-256, every other entry says
what is not known (verify) and is not downloadable without a source, the licence notes are there, the folders are the ones the voice service reads, and the
download planner puts the files where the code looks for them. No network: Hugging Face is replaced by a fake tree. If the scratch clone of Linly-Talker is
on this machine, every hash is checked against its blobs too."""
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from serve import downloads, voice_catalog as vc, voice_linly_packs as linly

CLONE = Path(r"C:\TEMP\smoke\upstream\Linly-Talker")
RAW = "https://raw.githubusercontent.com/Kedreamix/Linly-Talker/dc831b3eea1202728c45042dd92de73ade8c3abd/"
CODE = {"wav2lipv2-code": linly.WAV2LIPV2_FILES, "sadtalker-code": linly.SADTALKER_FILES, "ernerf-code": linly.ERNERF_FILES}
WEIGHTS = ("wav2lipv2-weights", "sadtalker-weights", "sadtalker-gfpgan", "ernerf-audio-encoder")
BY_HAND = ("wav2lipv2-yolo-face", "wav2lipv2-landmarks", "ernerf-bfm", "ernerf-face-parsing")
MINE = tuple(CODE) + WEIGHTS + BY_HAND


def entries():
    return [vc.BY_ID[i] for i in MINE]


class ShapeTest(unittest.TestCase):
    def test_every_entry_is_an_engine_pack_of_the_avatar_service_that_a_function_does_not_load(self):
        for e in entries():
            self.assertEqual((e["service"], e["kind"], e["selectable"]), ("avatar", "engine-pack", False), e["id"])
            self.assertTrue(e["folder"].startswith("avatar-engines/"), e["id"])
            self.assertTrue(e["notes"]["en"] and e["notes"]["it"] and e["venv"]["en"] and e["venv"]["it"], e["id"])
            self.assertIn(e["license"], vc.LICENSES)

    def test_the_folders_are_the_ones_the_voice_service_reads(self):
        ms = (Path(__file__).resolve().parents[2] / "voice" / "callbridge" / "models_store.py").read_text(encoding="utf-8")
        for e in entries():
            self.assertIn(f'"{e["id"]}": "{e["folder"]}"', ms, e["id"])
            self.assertEqual(vc.folder_of(e), e["folder"])

    def test_a_code_pack_sits_in_a_code_folder_and_the_weights_next_to_it_or_in_it_where_the_code_looks(self):
        self.assertEqual([vc.BY_ID[i]["folder"] for i in CODE], ["avatar-engines/wav2lipv2/code", "avatar-engines/sadtalker/code", "avatar-engines/ernerf/code"])
        self.assertEqual(vc.BY_ID["sadtalker-gfpgan"]["folder"], "avatar-engines/sadtalker/code/gfpgan/weights")          # the code reads ./gfpgan/weights
        self.assertEqual(vc.BY_ID["ernerf-audio-encoder"]["folder"], "avatar-engines/ernerf/code/checkpoints")          # and ./checkpoints/audio_visual_encoder.pth


class CodePackTest(unittest.TestCase):
    def test_every_file_has_a_pinned_address_a_sha256_and_a_size(self):
        for id_, files in CODE.items():
            e = vc.BY_ID[id_]
            self.assertEqual(len(e["sources"]), len(files), id_)
            self.assertEqual(e["size"], sum(f[2] for f in files))
            for s, (path, sha, size) in zip(e["sources"], files):
                self.assertEqual((s["type"], s["url"], s["name"], s["sha256"], s["size"]), ("url", RAW + path, path, sha, size), (id_, path))
                self.assertRegex(sha, r"^[0-9a-f]{64}$")
                self.assertGreater(size, 0)
                downloads.safe_relpath(path)                                                  # `__init__.py` and every other name is accepted
            self.assertEqual(vc.downloadable(e), (True, ""), id_)

    def test_the_probe_files_are_in_the_pack_and_the_license_file_comes_with_it(self):
        for id_, files in CODE.items():
            names = {f[0] for f in files}
            self.assertTrue(set(vc.BY_ID[id_]["probe"]) <= names, id_)
            self.assertIn("LICENSE", names)

    def test_the_scripts_each_engine_runs_are_in_its_pack(self):
        self.assertIn("TFG/Wav2Lipv2.py", {f[0] for f in linly.WAV2LIPV2_FILES})
        self.assertIn("src/modelsv2/wav2lip_v2.py", {f[0] for f in linly.WAV2LIPV2_FILES})
        self.assertIn("TFG/SadTalker.py", {f[0] for f in linly.SADTALKER_FILES})
        self.assertIn("TFG/NeRFTalk.py", {f[0] for f in linly.ERNERF_FILES})
        self.assertIn("NeRF/data_utils/process.py", {f[0] for f in linly.ERNERF_FILES})
        self.assertNotIn("TFG/__init__.py", {f[0] for f in linly.WAV2LIPV2_FILES + linly.SADTALKER_FILES})            # it imports every other engine

    def test_nothing_unneeded_is_listed(self):
        for files in CODE.values():
            for path, _sha, _size in files:
                self.assertFalse(path.lower().endswith((".mp4", ".jpg", ".png", ".wav", ".gif", ".ipynb")), path)
                self.assertFalse(path.startswith(("docs/", "webui", "app")), path)

    def test_the_licence_text_shown_before_an_install_keeps_only_the_real_restrictions(self):
        lic = vc.LICENSES["linly-talker"]
        self.assertIn("MIT", lic["en"])
        self.assertIn("non-commercial", lic["en"])
        self.assertNotIn("not verified", lic["en"] + vc.LICENSES["ultralytics-face"]["en"].replace("not stated", ""))
        face = vc.LICENSES["ultralytics-face"]
        self.assertIn("AGPL-3.0", face["en"])
        self.assertTrue(face["it"] and lic["it"])
        for id_ in ("sadtalker-code", "ernerf-code", "sadtalker-weights"):
            self.assertNotIn("license", vc.BY_ID[id_]["verify"], id_)                               # the licence notes live in the code and docs, not on the page


class WeightsTest(unittest.TestCase):
    def test_the_weights_of_the_repository_the_readme_names_are_to_verify_and_say_so(self):
        for id_ in WEIGHTS:
            e = vc.BY_ID[id_]
            (s,) = e["sources"]
            self.assertEqual((s["type"], s["repo"], s["revision"]), ("hf", "Kedreamix/Linly-Talker", "main"), id_)
            self.assertTrue({"source", "size"} <= set(e["verify"]), id_)
            self.assertTrue(s["strip"] and all(f.startswith(s["strip"]) for f in s["files"]), id_)
            self.assertEqual(sorted(Path(f).name for f in s["files"]), sorted(e["probe"]), id_)
            self.assertEqual(vc.downloadable(e), (True, ""), id_)

    def test_wav2lipv2_weights_say_the_readme_does_not_list_the_file_and_are_treated_as_non_commercial(self):
        e = vc.BY_ID["wav2lipv2-weights"]
        self.assertIn("does not list this file", e["notes"]["en"])
        self.assertTrue(e["noncommercial"] and vc.BY_ID["wav2lipv2-code"]["noncommercial"])
        self.assertEqual([x["id"] for x in vc.CATALOG if x["noncommercial"] and x["id"].startswith("wav2lipv2")], ["wav2lipv2-code", "wav2lipv2-weights"])

    def test_what_has_no_stated_source_has_no_source_says_verify_and_cannot_be_downloaded(self):
        for id_ in BY_HAND:
            e = vc.BY_ID[id_]
            self.assertEqual(e["sources"], [], id_)
            self.assertEqual(vc.downloadable(e), (False, "no_source"), id_)
            self.assertTrue({"source", "sha256"} <= set(e["verify"]), id_)
            self.assertTrue(e["probe"], id_)
            self.assertEqual(vc.download_entry(id_)["why_not"]["code"], "no_source")
        self.assertEqual(vc.BY_ID["wav2lipv2-yolo-face"]["license"], "ultralytics-face")
        self.assertIn("AGPL", vc.BY_ID["wav2lipv2-yolo-face"]["notes"]["en"])
        self.assertIn("Basel", vc.BY_ID["ernerf-bfm"]["notes"]["en"])

    def test_every_pack_file_has_a_sha256_or_its_entry_is_marked_verify(self):
        for e in entries():
            for s in e["sources"]:
                if s["type"] == "url":
                    self.assertRegex(s["sha256"], r"^[0-9a-f]{64}$")
                else:
                    self.assertTrue(e["verify"], e["id"])                                       # Hugging Face gives the hash at plan time, the entry still says verify


class PlanTest(unittest.TestCase):
    def test_the_plan_of_a_code_pack_names_every_file_with_its_hash_and_needs_no_network(self):
        e = vc.download_entry("wav2lipv2-code")
        problems = []
        files = downloads._source_files(e, problems)
        self.assertEqual(problems, [])
        self.assertEqual([(f["name"], f["sha256"], f["size"]) for f in files], [(p, h, s) for p, h, s in linly.WAV2LIPV2_FILES])
        self.assertTrue(all(f["url"].startswith(RAW) for f in files))

    def test_a_weights_file_lands_without_the_repository_folder_and_a_missing_one_is_a_problem(self):
        tree = {"checkpoints/SadTalker_V0.0.2_256.safetensors": {"size": 5, "sha256": "a" * 64}}
        with mock.patch.object(downloads, "resolve_revision", return_value="r" * 40), mock.patch.object(downloads, "repo_tree", return_value=tree):
            problems = []
            files = downloads._source_files(vc.download_entry("sadtalker-weights"), problems)
        self.assertEqual([f["name"] for f in files], ["SadTalker_V0.0.2_256.safetensors"])
        self.assertEqual([p["code"] for p in problems], ["missing_file"])                         # mapping_00229-model.pth.tar is not in the fake tree
        self.assertIn("mapping_00229-model.pth.tar", problems[0]["text"])

    def test_the_planner_accepts_underscore_names_and_still_refuses_hidden_and_reserved_ones(self):
        self.assertEqual(downloads.safe_relpath("src/__init__.py"), "src/__init__.py")
        for bad in (".hidden", "a/../b", "con.txt", "a//b", "name."):
            with self.assertRaises(downloads.DownloadError, msg=bad):
                downloads.safe_relpath(bad)


@unittest.skipUnless((CLONE / ".git").exists() or (CLONE / "HEAD").exists(), "the scratch clone of Linly-Talker is not on this machine")
class AgainstTheCloneTest(unittest.TestCase):
    def test_every_hash_and_size_is_the_one_of_the_blob_at_the_pinned_commit(self):
        import hashlib
        self.assertEqual(linly.COMMIT, "dc831b3eea1202728c45042dd92de73ade8c3abd")
        for files in CODE.values():
            for path, sha, size in files:
                blob = subprocess.run(["git", "-C", str(CLONE), "show", f"{linly.COMMIT}:{path}"], capture_output=True, check=True).stdout
                self.assertEqual((hashlib.sha256(blob).hexdigest(), len(blob)), (sha, size), path)


if __name__ == "__main__":
    unittest.main()
