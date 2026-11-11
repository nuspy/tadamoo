"""The route of the picture and video tools, on a real server with a stand-in engine and a stand-in LTX Studio."""
import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from media_fake import make_ltx  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402
from tadamoo.media import client  # noqa: E402


class MediaRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(base / "data"), "TADAMOO_CACHE": str(base / "cache")})
        env.start()
        self.addCleanup(env.stop)
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.ltx = make_ltx(base / "ltx")
        client.forget_checks()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=None if body is None else json.dumps(body), headers={"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def test_the_status_says_what_can_be_made_and_which_tools_a_conversation_offers(self):
        code, out = self.req("POST", "/api/media", {"ltx_dir": str(self.ltx)})
        self.assertEqual(code, 200)
        self.assertEqual((out["ltx_dir"], out["image"]["ready"], out["video"]["ready"], out["tools"]), (str(self.ltx), True, True, ["generate_image", "generate_video"]))
        code, out = self.req("GET", "/api/media")
        self.assertEqual((code, out["settings"]["ltx_dir"], out["settings"]["enabled"]), (200, str(self.ltx), True))

    def test_off_or_missing_weights_take_the_tool_out_of_the_list(self):
        self.req("POST", "/api/media", {"ltx_dir": str(self.ltx)})
        self.assertEqual(self.req("POST", "/api/media", {"enabled": False})[1]["tools"], [])
        half = make_ltx(Path(self.tmp.name) / "half", missing=["qwen21-vae"])
        out = self.req("POST", "/api/media", {"enabled": True, "ltx_dir": str(half)})[1]
        self.assertEqual((out["tools"], out["image"]["missing"]), (["generate_video"], ["qwen21-vae"]))

    def test_a_bad_change_is_a_400_with_the_reason_and_changes_nothing(self):
        self.req("POST", "/api/media", {"ltx_dir": str(self.ltx), "wait_s": 30})
        for body in ({"wait_s": 99999}, {"ltx_dir": "/no/such/folder"}, {"nope": 1}, {"enabled": "yes"}):
            code, out = self.req("POST", "/api/media", body)
            self.assertEqual(code, 400, body)
            self.assertTrue(out["error"]["message"])
        self.assertEqual(self.req("GET", "/api/media")[1]["settings"]["wait_s"], 30)


if __name__ == "__main__":
    unittest.main()
