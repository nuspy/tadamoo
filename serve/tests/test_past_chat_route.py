"""POST /api/past/chat: the model-only chat page hands its turns to the index of earlier conversations, from this PC only."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from tadamoo.past import PastIndex
from serve import handler
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class PastChatRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.past = PastIndex(Path(cls.tmp.name) / "past.db")
        cls.patch = mock.patch.object(handler, "_PAST", cls.past)
        cls.patch.start()
        cls.cards = mock.patch.object(handler, "_CARDS", mock.Mock())          # no card is written into the index while the test counts its hits
        cls.cards.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.cards.stop()
        cls.patch.stop()
        cls.past.close()
        cls.tmp.cleanup()

    def post(self, payload):
        req = urllib.request.Request(self.base + "/api/past/chat", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_words_of_a_chat_become_searchable_and_a_second_post_replaces_them(self):
        turns = [{"role": "user", "content": "parliamo del vertice di Bruxelles"}, {"role": "assistant", "content": "certo"}]
        self.assertEqual(self.post({"id": "abc12345", "title": "UE", "messages": turns})[0], 200)
        hit = self.past.search("Bruxelles")[0]
        self.assertEqual((hit["session"], hit["kind"], hit["title"]), ("chat-abc12345", "chat", "UE"))
        self.post({"id": "abc12345", "title": "UE", "messages": turns + [{"role": "user", "content": "e le tulipe?"}, {"role": "assistant", "content": "fioriscono"}]})
        self.assertEqual(len(self.past.search("tulipe")), 1)
        self.assertEqual(len(self.past.sessions()), 1)

    def test_a_request_that_is_not_a_chat_is_refused(self):
        self.assertEqual(self.post({"id": "x", "messages": []})[0], 400)
        self.assertEqual(self.post({"id": "abc12345", "messages": "no"})[0], 400)

    def test_it_answers_only_on_this_pc(self):
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            self.assertEqual(self.post({"id": "abc12345", "messages": []})[0], 403)


if __name__ == "__main__":
    unittest.main()
