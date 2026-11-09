"""voice.json `avatar.enhance`: the defaults, every field checked, a scale that must suit its provider, the whole object checked before anything is
written, the file replaced whole, and the camera setting left alone. The route that carries it is /api/voice/avatar (voice_avatar.py)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import voice_avatar, voice_proxy, voice_settings as vs


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

    def refused(self, req, code):
        with self.assertRaises(vs.VoiceSettingsError) as cm:
            voice_avatar.update({"enhance": req})
        self.assertEqual(cm.exception.code, code, req)
        self.assertTrue(cm.exception.en and cm.exception.it)
        return cm.exception


class Defaults(Base):
    def test_everything_is_off_when_nothing_was_chosen(self):
        self.assertEqual(voice_avatar.view(), {"virtual_camera": False, "enhance": {"upscale": "none", "scale": 2.0, "interpolate": "none", "factor": 2,
                                                                                    "worker_fps_cap": 0, "worker_height_cap": 0, "rtx_video_path": ""}})
        self.assertFalse((self.root / "data" / "voice.json").exists())

    def test_a_damaged_object_in_the_file_reads_as_the_defaults(self):
        (self.root / "data").mkdir()
        for bad in ({"upscale": "magic"}, {"scale": 9}, "on", {"factor": 5}, {"worker_fps_cap": 3}):
            (self.root / "data" / "voice.json").write_text(json.dumps({"avatar": {"enhance": bad}}), encoding="utf-8")
            self.assertEqual(voice_avatar.view()["enhance"], vs.ENHANCE_DEFAULT, bad)


class Changes(Base):
    def test_a_choice_is_kept_and_the_other_fields_keep_their_values(self):
        got = voice_avatar.update({"enhance": {"upscale": "basic", "scale": 3, "interpolate": "basic", "factor": 3}})
        self.assertEqual((got["enhance"]["upscale"], got["enhance"]["scale"], got["enhance"]["interpolate"], got["enhance"]["factor"]), ("basic", 3.0, "basic", 3))
        got = voice_avatar.update({"enhance": {"worker_fps_cap": 12}})
        self.assertEqual((got["enhance"]["upscale"], got["enhance"]["worker_fps_cap"]), ("basic", 12))
        self.assertEqual(self.stored()["avatar"]["enhance"]["factor"], 3)

    def test_the_camera_setting_and_the_acceleration_do_not_disturb_each_other(self):
        voice_avatar.update({"virtual_camera": True})
        voice_avatar.update({"enhance": {"upscale": "basic"}})
        got = voice_avatar.update({"virtual_camera": False})
        self.assertEqual((got["virtual_camera"], got["enhance"]["upscale"]), (False, "basic"))

    def test_each_field_is_checked(self):
        self.refused({"upscale": "dlss"}, "upscale")
        self.refused({"upscale": 1}, "upscale")
        self.refused({"scale": 5}, "scale")
        self.refused({"scale": "2"}, "scale")
        self.refused({"scale": True}, "scale")
        self.refused({"interpolate": "rife"}, "interpolate")
        self.refused({"factor": 4}, "factor")
        self.refused({"factor": 2.0}, "factor")
        self.refused({"worker_fps_cap": 4}, "worker_fps_cap")
        self.refused({"worker_fps_cap": 61}, "worker_fps_cap")
        self.refused({"worker_fps_cap": "10"}, "worker_fps_cap")
        self.refused({"worker_height_cap": 119}, "worker_height_cap")
        self.refused({"worker_height_cap": 2161}, "worker_height_cap")
        self.refused({"worker_height_cap": True}, "worker_height_cap")
        self.refused({"driver": "install"}, "field")
        self.refused(["upscale"], "enhance")

    def test_the_limits_take_zero_for_automatic_and_their_edges(self):
        for key, ok in (("worker_fps_cap", (0, 5, 60)), ("worker_height_cap", (0, 120, 2160))):
            for value in ok:
                self.assertEqual(voice_avatar.update({"enhance": {key: value}})["enhance"][key], value)

    def test_a_scale_must_suit_the_provider_it_is_chosen_with(self):
        voice_avatar.update({"enhance": {"upscale": "basic", "scale": 1.5}})
        exc = self.refused({"upscale": "rtx_video"}, "scale")                      # 1.5 is stored: the pair is refused as a whole
        self.assertIn("rtx_video takes the scales 2, 3, 4", exc.en)
        self.assertEqual(self.stored()["avatar"]["enhance"]["upscale"], "basic")           # nothing was written
        got = voice_avatar.update({"enhance": {"upscale": "rtx_video", "scale": 2}})
        self.assertEqual((got["enhance"]["upscale"], got["enhance"]["scale"]), ("rtx_video", 2.0))
        self.refused({"scale": 1.5}, "scale")

    def test_the_sdk_path_is_a_full_path_to_a_dll_or_nothing(self):
        path = str(self.root / "sdk" / "rtxvideo.dll")
        self.assertEqual(voice_avatar.update({"enhance": {"rtx_video_path": path}})["enhance"]["rtx_video_path"], path)
        self.refused({"rtx_video_path": "rtxvideo.dll"}, "rtx_video_path")
        self.refused({"rtx_video_path": str(self.root / "run.exe")}, "rtx_video_path")
        self.refused({"rtx_video_path": 7}, "rtx_video_path")
        self.refused({"rtx_video_path": path + "\0"}, "rtx_video_path")
        self.assertEqual(voice_avatar.update({"enhance": {"rtx_video_path": ""}})["enhance"]["rtx_video_path"], "")

    def test_a_refused_request_writes_nothing_and_a_good_one_replaces_the_file_whole(self):
        voice_avatar.update({"enhance": {"upscale": "basic"}})
        before = (self.root / "data" / "voice.json").read_bytes()
        self.refused({"upscale": "basic", "scale": 7}, "scale")
        self.assertEqual((self.root / "data" / "voice.json").read_bytes(), before)
        voice_avatar.update({"enhance": {"factor": 3}})
        self.assertFalse((self.root / "data" / "voice.json.tmp").exists())
        self.assertEqual(self.stored()["avatar"]["enhance"]["factor"], 3)

    def test_the_other_settings_of_the_file_survive(self):
        vs.set_brain({"mode": "harness"})
        voice_avatar.update({"enhance": {"upscale": "basic"}})
        self.assertEqual(self.stored()["brain"]["mode"], "harness")

    def test_the_route_that_reads_the_plan_is_on_the_proxys_list(self):
        self.assertIn(("GET", "/avatar/enhance"), voice_proxy.ROUTES)
        self.assertIsNone(voice_proxy.ROUTES[("GET", "/avatar/enhance")](None))
        with self.assertRaises(voice_proxy.Refused):
            voice_proxy.ROUTES[("GET", "/avatar/enhance")]({"x": 1})


if __name__ == "__main__":
    unittest.main()
