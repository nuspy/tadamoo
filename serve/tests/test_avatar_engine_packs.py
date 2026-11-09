"""The catalog entries of the PC avatar engines (Wav2Lip v2, SadTalker, ER-NeRF): their code is vendored, so there are only weights here; every file has a pinned
source (a Hugging Face commit, or a GitHub commit for the AD-NeRF 3DMM files) with its SHA-256 and size, the folders are the ones the voice service reads, and the
download planner puts the files where the code looks for them. No network: Hugging Face is replaced by a fake tree."""
import importlib.util
import re
import unittest
from pathlib import Path
from unittest import mock

from serve import downloads, voice_catalog as vc

LINLY_REV = "de2ee105d8fbe279e973b79db24009e803845b2f"                    # Kedreamix/Linly-Talker on Hugging Face
RAW = "https://raw.githubusercontent.com/YudongGuo/AD-NeRF/9fdcd7e1352aa74e26baa8e60c862f9fbd7933bf/data_util/face_tracking/3DMM/"
LINLY = ("wav2lipv2-weights", "wav2lipv2-yolo-face", "wav2lipv2-landmarks", "sadtalker-weights", "sadtalker-gfpgan", "ernerf-audio-encoder", "ernerf-face-parsing")
MINE = LINLY + ("ernerf-bfm",)


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

    def test_the_weights_sit_where_the_code_looks_for_them(self):
        self.assertEqual(vc.BY_ID["sadtalker-gfpgan"]["folder"], "avatar-engines/sadtalker/code/gfpgan/weights")          # the code reads ./gfpgan/weights
        self.assertEqual(vc.BY_ID["ernerf-audio-encoder"]["folder"], "avatar-engines/ernerf/code/checkpoints")          # and ./checkpoints/audio_visual_encoder.pth
        self.assertEqual(vc.BY_ID["ernerf-bfm"]["folder"], "avatar-engines/ernerf/code/NeRF/data_utils/face_tracking/3DMM")
        self.assertEqual(vc.BY_ID["wav2lipv2-yolo-face"]["folder"], "avatar-engines/wav2lipv2/models/yolov8n-face")
        self.assertEqual(vc.BY_ID["wav2lipv2-landmarks"]["folder"], "avatar-engines/wav2lipv2/models/wflw/hrnet18_256x256_p1")

    def test_the_code_is_vendored_so_no_entry_installs_it(self):
        ids = {e["id"] for e in vc.CATALOG}
        self.assertFalse({"wav2lipv2-code", "sadtalker-code", "ernerf-code"} & ids)
        self.assertIsNone(importlib.util.find_spec("serve.voice_linly_packs"))
        from callbridge import models_store
        self.assertFalse([k for k in list(models_store.MODELS) + list(models_store.FOLDERS) if k.endswith("-code")])
        for name in ("wav2lipv2", "sadtalker", "ernerf"):
            self.assertTrue((Path(__file__).resolve().parents[2] / "voice" / "avatar_engines" / "vendor" / name / "VENDORED.md").is_file(), name)


class PinnedTest(unittest.TestCase):
    def test_every_linly_weight_is_pinned_to_one_commit_with_a_sha256_and_a_size(self):
        for id_ in LINLY:
            e = vc.BY_ID[id_]
            (s,) = e["sources"]
            self.assertEqual((s["type"], s["repo"], s["revision"]), ("hf", "Kedreamix/Linly-Talker", LINLY_REV), id_)
            self.assertEqual(sorted(s["pins"]), sorted(s["files"]), id_)
            self.assertEqual(sorted(s["sizes"]), sorted(s["files"]), id_)
            for sha in s["pins"].values():
                self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertEqual(e["size"], sum(s["sizes"].values()), id_)
            self.assertTrue(s["strip"] and all(f.startswith(s["strip"]) for f in s["files"]), id_)
            self.assertEqual(sorted(f[len(s["strip"]):] for f in s["files"]), sorted(e["probe"]), id_)
            self.assertEqual(vc.downloadable(e), (True, ""), id_)

    def test_the_3dmm_files_are_pinned_to_a_github_commit_with_a_sha256_and_a_size(self):
        e = vc.BY_ID["ernerf-bfm"]
        self.assertEqual([s["name"] for s in e["sources"]], ["exp_info.npy", "keys_info.npy", "sub_mesh.obj", "topology_info.npy"])
        self.assertEqual(sorted(e["probe"]), sorted(s["name"] for s in e["sources"]))
        for s in e["sources"]:
            self.assertEqual((s["type"], s["url"]), ("url", RAW + s["name"]))
            self.assertRegex(s["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(s["size"], 0)
        self.assertEqual(e["size"], sum(s["size"] for s in e["sources"]))
        self.assertEqual((e["license"], vc.downloadable(e)), ("mit", (True, "")))
        self.assertIn("Basel", e["notes"]["en"])                                                       # the Basel file stays by hand

    def test_nothing_has_to_be_placed_by_hand_but_the_basel_face_model(self):
        for e in entries():
            self.assertNotEqual(e["sources"], [], e["id"])

    def test_the_wav2lip_v2_weights_and_the_face_parsing_ones_are_non_commercial_and_the_others_are_not(self):
        self.assertEqual([x["id"] for x in vc.CATALOG if x["noncommercial"] and x["id"] in MINE], ["wav2lipv2-weights", "ernerf-face-parsing"])
        self.assertEqual(vc.BY_ID["wav2lipv2-yolo-face"]["license"], "ultralytics-face")
        self.assertIn("AGPL", vc.BY_ID["wav2lipv2-yolo-face"]["notes"]["en"])

    def test_the_licence_text_shown_before_an_install_keeps_the_restrictions_that_protect_the_person(self):
        lic = vc.LICENSES["linly-talker"]
        self.assertIn("non-commercial", lic["en"])
        self.assertIn("states no licence", lic["en"])
        face = vc.LICENSES["ultralytics-face"]
        self.assertIn("AGPL-3.0", face["en"])
        self.assertTrue(face["it"] and lic["it"])

    def test_the_face_parsing_entry_says_how_it_was_matched_and_what_else_the_code_fetches(self):
        notes = vc.BY_ID["ernerf-face-parsing"]["notes"]["en"]
        self.assertIn("53,289,463", notes)
        self.assertIn("not compared byte for byte", notes)
        self.assertIn("resnet18-5c106cde.pth", notes)


class PlanTest(unittest.TestCase):
    def test_the_plan_of_the_3dmm_files_names_every_file_with_its_hash_and_needs_no_network(self):
        problems = []
        files = downloads._source_files(vc.download_entry("ernerf-bfm"), problems)
        self.assertEqual(problems, [])
        self.assertEqual([f["name"] for f in files], ["exp_info.npy", "keys_info.npy", "sub_mesh.obj", "topology_info.npy"])
        self.assertTrue(all(f["url"].startswith(RAW) and re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) and f["size"] for f in files))

    def test_a_weights_file_lands_without_the_repository_folder_and_a_missing_one_is_a_problem(self):
        pin = vc.BY_ID["sadtalker-weights"]["sources"][0]["pins"]["checkpoints/SadTalker_V0.0.2_256.safetensors"]
        tree = {"checkpoints/SadTalker_V0.0.2_256.safetensors": {"size": 5, "sha256": pin}}
        with mock.patch.object(downloads, "resolve_revision", return_value=LINLY_REV), mock.patch.object(downloads, "repo_tree", return_value=tree):
            problems = []
            files = downloads._source_files(vc.download_entry("sadtalker-weights"), problems)
        self.assertEqual([f["name"] for f in files], ["SadTalker_V0.0.2_256.safetensors"])
        self.assertEqual([p["code"] for p in problems], ["missing_file"])                         # mapping_00229-model.pth.tar is not in the fake tree
        self.assertIn("mapping_00229-model.pth.tar", problems[0]["text"])

    def test_a_file_that_is_not_the_pinned_one_is_refused(self):
        tree = {"checkpoints/wav2lipv2.pth": {"size": 5, "sha256": "a" * 64}}
        with mock.patch.object(downloads, "resolve_revision", return_value=LINLY_REV), mock.patch.object(downloads, "repo_tree", return_value=tree):
            problems = []
            files = downloads._source_files(vc.download_entry("wav2lipv2-weights"), problems)
        self.assertEqual(files, [])
        self.assertEqual([p["code"] for p in problems], ["pin"])

    def test_the_landmark_config_which_is_not_in_lfs_is_pinned_by_the_catalog_and_lands_with_its_model(self):
        pins = vc.BY_ID["wav2lipv2-landmarks"]["sources"][0]["pins"]
        tree = {p: {"size": 1, "sha256": None, "git_oid": "b" * 40} if p.endswith("config.yaml") else {"size": 1, "sha256": h} for p, h in pins.items()}
        with mock.patch.object(downloads, "resolve_revision", return_value=LINLY_REV), mock.patch.object(downloads, "repo_tree", return_value=tree):
            problems = []
            files = downloads._source_files(vc.download_entry("wav2lipv2-landmarks"), problems)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(f["name"] for f in files), ["config.yaml", "model.pth"])
        self.assertTrue(all(f["sha256"] in pins.values() for f in files))

    def test_the_planner_accepts_underscore_names_and_still_refuses_hidden_and_reserved_ones(self):
        self.assertEqual(downloads.safe_relpath("src/__init__.py"), "src/__init__.py")
        for bad in (".hidden", "a/../b", "con.txt", "a//b", "name."):
            with self.assertRaises(downloads.DownloadError, msg=bad):
                downloads.safe_relpath(bad)


if __name__ == "__main__":
    unittest.main()
