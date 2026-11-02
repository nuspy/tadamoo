"""GET and POST /api/technical-session: the master switch of the technical session, changed from this PC only."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from harness import side_uses
from serve import handler, userdata
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class TechnicalSwitchRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = mock.patch.object(userdata, "path", lambda *parts: Path(cls.tmp.name).joinpath(*parts))
        cls.patch.start()
        cls.env = mock.patch.dict("os.environ", {"BASALT_DATA": cls.tmp.name})
        cls.env.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.patch.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def call(self, method, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + "/api/technical-session", data=data, headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_it_is_on_by_default_and_the_choice_is_kept(self):
        side_uses.set_session(True)
        self.assertEqual(self.call("GET"), (200, {"on": True}))
        self.assertEqual(self.call("POST", {"on": False}), (200, {"on": False}))
        self.assertEqual(self.call("GET"), (200, {"on": False}))
        self.assertEqual(self.call("POST", {"on": True}), (200, {"on": True}))

    def test_anything_but_a_true_or_false_is_refused(self):
        for bad in ({"on": "yes"}, {}, {"on": 1}):
            self.assertEqual(self.call("POST", bad)[0], 400)


if __name__ == "__main__":
    unittest.main()
