"""The weights of the personalised avatar engines in the catalog (their code is vendored in voice/avatar_engines/vendor, so there is no entry for it): FeatherHuBERT, the
landmark weights both engines load, and the wenet encoder that has no source. Every file has a SHA-256 and a size or the entry is marked to verify, the licence text is there,
an entry without a source cannot be downloaded, the plan is the downloader's with no network, and the training routes of the voice tab are on the proxy's list with their
bodies checked. No download is made."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import downloads, voice_catalog as vc, voice_engine_weights as packs, voice_proxy

ENTRIES = ("feathertalk-weights", "engine-landmarks", "ultralight-wenet-encoder")
HEX = r"^[0-9a-f]{64}$"
VENDOR = Path(__file__).resolve().parents[2] / "voice" / "avatar_engines" / "vendor"


class CatalogEntriesTest(unittest.TestCase):
    def test_the_entries_are_engine_packs_of_the_avatar_service_that_no_function_loads_and_the_code_has_none(self):
        for model in ENTRIES:
            e = vc.find(model)
            self.assertEqual((e["service"], e["kind"], e["selectable"]), ("avatar", "engine-pack", False), model)
        self.assertEqual(vc.DEFAULTS["avatar"], "musetalk-v15")
        self.assertIsNone(vc.find("ultralight-code"))
        self.assertIsNone(vc.find("feathertalk-code"))

    def test_each_one_installs_under_avatar_engines_where_the_engine_looks(self):
        self.assertEqual({m: vc.folder_of(vc.find(m)) for m in ENTRIES}, {
            "feathertalk-weights": "avatar-engines/feathertalk/weights", "engine-landmarks": "avatar-engines/landmarks",
            "ultralight-wenet-encoder": "avatar-engines/ultralight/weights"})

    def test_every_source_is_a_raw_address_at_the_pinned_commit_with_its_sha256_and_size(self):
        self.assertRegex(packs.FEATHERTALK_COMMIT, r"^[0-9a-f]{40}$")
        for model in ("feathertalk-weights", "engine-landmarks"):
            e = vc.find(model)
            self.assertTrue(e["sources"])
            for s in e["sources"]:
                self.assertEqual(s["type"], "url")
                self.assertEqual(s["url"], f"https://raw.githubusercontent.com/{packs.FEATHERTALK_REPO}/{packs.FEATHERTALK_COMMIT}/{'data_utils/' if model == 'engine-landmarks' else ''}{s['name']}")
                self.assertRegex(s["sha256"], HEX, s["name"])
                self.assertIsInstance(s["size"], int)
                self.assertGreater(s["size"], 0)
            self.assertEqual(e["size"], sum(s["size"] for s in e["sources"]))
            self.assertEqual(vc.downloadable(e), (True, ""))

    def test_an_entry_without_a_source_is_to_verify_and_not_downloadable(self):
        e = vc.find("ultralight-wenet-encoder")
        self.assertEqual(vc.downloadable(e), (False, "no_source"))
        self.assertTrue(e["verify"])

    def test_the_landmark_files_are_the_same_in_the_vendored_readmes_world_and_carry_a_licence_to_verify(self):
        e = vc.find("engine-landmarks")
        self.assertIn("license", e["verify"])
        self.assertEqual(sorted(s["name"] for s in e["sources"]), ["checkpoint_epoch_335.pth.tar", "scrfd_2.5g_kps.onnx"])
        self.assertIn("not stated", e["notes"]["en"])


class LicenceTest(unittest.TestCase):
    def test_feathertalk_has_its_licence_text_in_the_entry_and_the_same_file_vendored(self):
        e = vc.find("feathertalk-weights")
        lic = vc.license_of(e)
        self.assertIn("Version 2.0, January 2004", lic["text"])
        self.assertEqual(hashlib.sha256(lic["text"].encode("utf-8")).hexdigest(), hashlib.sha256((VENDOR / "feathertalk" / "LICENSE").read_bytes()).hexdigest())
        self.assertIn("unfilled template", lic["en"])
        self.assertFalse(e["noncommercial"])

    def test_the_weights_are_pinned_with_the_sha256_the_upstream_docs_state_and_the_chinese_only_note(self):
        e = vc.find("feathertalk-weights")
        (s,) = e["sources"]
        self.assertEqual((s["name"], s["sha256"], s["size"]), ("feather_hubert.pth", "3b147e66502132bac3ff76b4730c2a83c341c75251d22a28182a5a1b689cfc46", 13480913))
        self.assertEqual(e["sha256"], s["sha256"])
        self.assertIn("3b147e66502132bac3ff76b4730c2a83c341c75251d22a28182a5a1b689cfc46", (VENDOR / "feathertalk" / "docs" / "WEIGHTS.md").read_text(encoding="utf-8"))
        self.assertIn("Chinese speech only", e["notes"]["en"])
        self.assertIn("unverified", e["notes"]["en"])
        self.assertIn("solo", e["notes"]["it"])

    def test_the_wenet_encoder_has_no_source_and_the_note_names_what_the_readme_says(self):
        e = vc.find("ultralight-wenet-encoder")
        self.assertEqual((e["sources"], e["size"], e["sha256"]), ([], None, None))
        self.assertTrue({"source", "sha256"} <= set(e["verify"]))
        self.assertIn("Google Drive", e["notes"]["en"])
        self.assertNotIn("http", e["notes"]["en"])                                          # no address is invented or copied as a source
        self.assertEqual(e["probe"], ["encoder.onnx"])


class PlanTest(unittest.TestCase):
    """The downloader's own plan for an entry, with the network blocked: sizes and hashes come from the catalog."""

    def plan(self, model, root):
        with mock.patch.object(downloads, "url_size", side_effect=AssertionError("network")),                 mock.patch.object(downloads, "access_check", side_effect=AssertionError("network")):
            return downloads.plan(model, root, folders=[])

    def test_the_landmark_weights_plan_at_their_folder_with_every_file_hashed(self):
        with tempfile.TemporaryDirectory() as root:
            p = self.plan("engine-landmarks", root)
            self.assertTrue(p["ok"], p["problems"])
            self.assertEqual(p["folder"], os.path.join(root, "avatar-engines", "landmarks"))
            self.assertEqual([f["name"] for f in p["files"]], ["scrfd_2.5g_kps.onnx", "checkpoint_epoch_335.pth.tar"])
            self.assertTrue(all(f["sha256"] and f["size"] and f["url"].startswith("https://raw.githubusercontent.com/") for f in p["files"]))
            self.assertTrue(p["marker"])

    def test_the_encoder_without_a_source_cannot_be_planned_into_a_download(self):
        with tempfile.TemporaryDirectory() as root:
            p = self.plan("ultralight-wenet-encoder", root)
            self.assertFalse(p["ok"])
            self.assertEqual(p["problems"][0]["code"], "verify")


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, route, body=None, timeout=None):
        self.calls.append((method, route, body))
        return 200, {"ok": True}


class TrainProxyTest(unittest.TestCase):
    TOKEN = "ab" * 32

    def setUp(self):
        self.rec = Recorder()
        patcher = mock.patch.object(voice_proxy.voice_service, "call", self.rec)
        patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, method, path, body=None):
        with self.assertRaises(voice_proxy.Refused) as e:
            voice_proxy.call({"method": method, "path": path, **({"body": body} if body is not None else {})})
        return e.exception

    def test_the_four_training_routes_pass_with_a_checked_body(self):
        for method, path, body in (("POST", "/avatar/train/plan", {"avatar": "Ada-2", "engine": "ultralight", "epochs": 10, "asr": "wenet", "fresh": True}),
                                   ("POST", "/avatar/train/plan", {"avatar": "ada", "engine": "feathertalk"}),
                                   ("POST", "/avatar/train/start", {"token": self.TOKEN, "confirm": True}), ("GET", "/avatar/train/status", None),
                                   ("POST", "/avatar/train/cancel", None)):
            self.assertEqual(voice_proxy.call({"method": method, "path": path, **({"body": body} if body is not None else {})}), (200, {"ok": True}))
        for method, path, body in (("POST", "/avatar/train/setup", {"engine": "ultralight", "asr": "hubert", "avatar": "ada", "probe": True}), ("POST", "/avatar/train/setup", {"engine": "feathertalk"}),
                                   ("POST", "/avatar/train/python", {"path": "/opt/py/python"}), ("POST", "/avatar/train/python", {"path": ""}),
                                   ("POST", "/avatar/train/export", {"avatar": "ada", "engine": "feathertalk"})):
            self.assertEqual(voice_proxy.call({"method": method, "path": path, "body": body}), (200, {"ok": True}))
        self.assertEqual(len(self.rec.calls), 10)

    def test_a_bad_body_is_refused_before_anything_is_sent(self):
        for body in ({"avatar": "ada"}, {"avatar": "ada", "engine": "musetalk_15"}, {"avatar": "../x", "engine": "ultralight"}, {"avatar": "ada", "engine": "ultralight", "epochs": 0},
                     {"avatar": "ada", "engine": "ultralight", "epochs": True}, {"avatar": "ada", "engine": "ultralight", "asr": "mp3"},
                     {"avatar": "ada", "engine": "ultralight", "fresh": "yes"}, {"avatar": "ada", "engine": "ultralight", "extra": 1}, "x", None):
            self.assertEqual(self.refused("POST", "/avatar/train/plan", body).status, 400, body)
        for body in ({"token": "short", "confirm": True}, {"token": self.TOKEN}, {"token": self.TOKEN, "confirm": "yes"}, {"token": self.TOKEN, "confirm": True, "x": 1}, None):
            self.assertEqual(self.refused("POST", "/avatar/train/start", body).status, 400, body)
        for body in ({"engine": "x"}, {"engine": "ultralight", "probe": "yes"}, {"engine": "ultralight", "avatar": "../x"}, {"engine": "ultralight", "asr": "mp3"}):
            self.assertEqual(self.refused("POST", "/avatar/train/setup", body).status, 400, body)
        for body in ({"path": 5}, {"path": "a\nb"}, {}, {"path": "x", "y": 1}):
            self.assertEqual(self.refused("POST", "/avatar/train/python", body).status, 400, body)
        for path in ("\\\\attacker\\share\\python.exe", "//attacker/share/python", "C:\\Users\\me\\Downloads\\setup.exe", "/usr/bin/bash", "C:\\py\\python.exe.bat"):
            for route in ("/avatar/train/python", "/avatar/engines/python"):       # a network path or a file that is not a Python never reaches the service
                self.assertEqual(self.refused("POST", route, {"path": path}).status, 400, (route, path))
        for body in ({"avatar": "ada"}, {"avatar": "ada", "engine": "visemes"}, {"avatar": "a b", "engine": "ultralight"}):
            self.assertEqual(self.refused("POST", "/avatar/train/export", body).status, 400, body)
        self.assertEqual(self.refused("GET", "/avatar/train/status", {"x": 1}).status, 400)
        self.assertEqual(self.refused("DELETE", "/avatar/train/status").status, 403)
        self.assertEqual(self.rec.calls, [])


if __name__ == "__main__":
    unittest.main()
