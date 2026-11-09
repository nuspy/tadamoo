"""The avatar routes the Voice tab may ask of the voice service through the JSON proxy: each is on the list with its body checked, anything near
them is refused, and nothing is sent to the service for a refused request. The service is replaced by a recorder."""
import unittest
from unittest import mock

from serve import voice_proxy

NAME = "Ada-2"
DRAFT = "0123456789abcdef" * 2


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, route, body=None, timeout=None):
        self.calls.append((method, route, body, timeout))
        return 200, {"ok": True}


class AvatarProxy(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        patcher = mock.patch.object(voice_proxy.voice_service, "call", self.rec)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ok(self, method, path, body=None):
        status, data = voice_proxy.call({"method": method, "path": path, **({"body": body} if body is not None else {})})
        self.assertEqual((status, data), (200, {"ok": True}), path)
        return self.rec.calls[-1]

    def refused(self, method, path, body=None, status=None):
        with self.assertRaises(voice_proxy.Refused) as e:
            voice_proxy.call({"method": method, "path": path, **({"body": body} if body is not None else {})})
        if status:
            self.assertEqual(e.exception.status, status, (method, path, body))
        return e.exception

    def test_the_reads_take_no_body(self):
        for path in ("/avatar/state", "/avatar/list", "/avatar/output", "/avatar/prepare", f"/avatar/wizard/{DRAFT}"):
            self.ok("GET", path)
            self.refused("GET", path, {"x": 1}, 400)
        self.assertEqual(len(self.rec.calls), 5)

    def test_speaking_takes_a_sentence_and_maybe_a_language_and_waits_for_the_synthesis(self):
        call = self.ok("POST", "/avatar/speak", {"text": " ciao ", "lang": "it"})
        self.assertEqual(call[:3], ("POST", "/avatar/speak", {"text": "ciao", "lang": "it"}))
        self.assertGreater(call[3], 60)
        self.assertEqual(self.ok("POST", "/avatar/speak", {"text": "hi"})[2], {"text": "hi"})
        for bad in ({}, {"text": ""}, {"text": "x" * 301}, {"text": 5}, {"text": "hi", "lang": "../x"}, {"text": "hi", "voice": "evil"}, {"text": "hi", "engine": "xtts"}, "hi"):
            self.refused("POST", "/avatar/speak", bad, 400)

    def test_starting_the_personalitys_avatar_or_one_by_name(self):
        self.assertEqual(self.ok("POST", "/avatar/start")[2], {})
        self.assertEqual(self.ok("POST", "/avatar/start", {})[2], {})
        self.assertEqual(self.ok("POST", "/avatar/start", {"persona": NAME})[2], {"persona": NAME})
        for bad in ({"persona": "../x"}, {"persona": 5}, {"avatar": NAME}, {"persona": NAME, "dir": "C:/"}):
            self.refused("POST", "/avatar/start", bad, 400)

    def test_preparing_names_an_avatar_and_a_plain_clip(self):
        self.assertEqual(self.ok("POST", "/avatar/prepare", {"avatar": NAME, "clip": "walk 1.mp4"})[2], {"avatar": NAME, "clip": "walk 1.mp4"})
        self.assertEqual(self.ok("POST", "/avatar/prepare", {"avatar": NAME})[2], {"avatar": NAME})
        self.ok("POST", "/avatar/prepare/cancel")
        for bad in ({}, {"avatar": "../x"}, {"avatar": NAME, "clip": "../x.mp4"}, {"avatar": NAME, "clip": "x.mov"}, {"avatar": NAME, "path": "C:/x"}, {"avatar": 5}, {"avatar": NAME + "\n"}):
            self.refused("POST", "/avatar/prepare", bad, 400)

    def test_the_wizard_routes_that_carry_no_file(self):
        self.ok("POST", "/avatar/wizard/start")
        self.ok("DELETE", f"/avatar/wizard/{DRAFT}")
        call = self.ok("POST", f"/avatar/wizard/{DRAFT}/elabora", {"name": " Ada L. "})
        self.assertEqual(call[2], {"name": "Ada L.", "language": "it"})
        self.assertGreaterEqual(call[3], 120)
        for bad in ({}, {"name": ""}, {"name": "x" * 41}, {"name": "a\nb"}, {"name": "ok", "language": "../x"}, {"name": "ok", "folder": "C:/"}):
            self.refused("POST", f"/avatar/wizard/{DRAFT}/elabora", bad, 400)

    def test_the_files_of_the_wizard_do_not_go_through_the_json_proxy(self):
        for step in ("foto", "pose", "video", "voce"):
            self.refused("POST", f"/avatar/wizard/{DRAFT}/{step}", {"file": "AAAA"}, 403)
        self.refused("GET", "/avatar/preview.mjpg", None, 403)                                  # the stream has its own relay
        self.refused("GET", f"/avatar/{NAME}/assets.zip", None, 403)                           # the phone's zip is the phone's, not the tab's
        self.refused("POST", "/avatar/visemes", {"text": "hi", "duration_s": 1}, 403)
        self.assertEqual(self.rec.calls, [])

    def test_the_training_video_is_listed_and_removed_through_the_proxy_and_uploaded_through_the_relay(self):
        self.assertEqual(self.ok("GET", f"/avatar/{NAME}/training-video")[:3], ("GET", f"/avatar/{NAME}/training-video", None))
        self.assertEqual(self.ok("DELETE", f"/avatar/{NAME}/training-video")[:2], ("DELETE", f"/avatar/{NAME}/training-video"))
        self.refused("DELETE", f"/avatar/{NAME}/training-video", {"x": 1}, 400)
        self.refused("POST", f"/avatar/{NAME}/training-video", {"file": "AAAA"}, 403)             # a file goes through serve/voice_avatar.py, not a JSON body
        self.refused("PUT", f"/avatar/{NAME}/training-video", None, 403)
        for name in ("../x", "a/b", ".hidden", "a b", "x" * 61, ""):
            self.refused("GET", f"/avatar/{name}/training-video", None, 403)
            self.refused("DELETE", f"/avatar/{name}/training-video", None, 403)
        self.refused("GET", f"/avatar/{NAME}/training-video/x", None, 403)
        self.assertEqual(len(self.rec.calls), 2)

    def test_a_draft_id_or_an_avatar_name_that_is_not_plain_is_refused(self):
        for draft in ("../x", "A" * 32, "0" * 31, "0" * 33, "0" * 32 + "/x", ""):
            self.refused("GET", f"/avatar/wizard/{draft}", None, 403)
            self.refused("DELETE", f"/avatar/wizard/{draft}", None, 403)
        for name in ("../x", "a/b", ".hidden", "a b", "x" * 61, ""):
            self.refused("DELETE", f"/avatar/{name}", None, 403)
        self.assertEqual(self.rec.calls, [])

    def test_deleting_an_avatar_and_correcting_its_mouth(self):
        self.ok("DELETE", f"/avatar/{NAME}")
        self.refused("DELETE", f"/avatar/{NAME}", {"x": 1}, 400)
        self.assertEqual(self.ok("POST", f"/personas/{NAME}/wav2lip/bocca", {"p1": [10, 20.5], "p2": [30, 20]})[2], {"p1": [10, 20.5], "p2": [30, 20]})
        for bad in ({}, {"p1": [1, 2]}, {"p1": [1], "p2": [1, 2]}, {"p1": [True, 2], "p2": [1, 2]}, {"p1": [1, 2], "p2": [1, "x"]}, {"p1": [1, 2], "p2": [99999, 2]},
                    {"p1": [1, 2], "p2": [1, 2], "extra": 1}):
            self.refused("POST", f"/personas/{NAME}/wav2lip/bocca", bad, 400)

    def test_other_methods_and_neighbouring_routes_are_refused(self):
        for method, path in (("PUT", f"/avatar/{NAME}"), ("POST", "/avatar/stop"), ("POST", "/avatar/lipsync"), ("POST", "/avatar/stream"),
                             ("POST", "/avatar/interrupt"), ("GET", f"/personas/{NAME}/poster"), ("POST", f"/personas/{NAME}/poster"), ("GET", "/avatar/"),
                             ("POST", "/avatar/state"), ("DELETE", "/avatar/state/x"), ("GET", "/avatar/wizard/start")):
            self.refused(method, path, None, 403)
        self.assertEqual(self.rec.calls, [])


if __name__ == "__main__":
    unittest.main()
