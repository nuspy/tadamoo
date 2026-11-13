"""The voice service's door to the knowledge archives on a real server: its token opens the `/api/voice/rag/*` routes and no other `/api/rag` route, the page's
consent routes need the page's own key, and a question travels from the service to the page and back. The embedding side is not started."""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import handler, voice_service
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class VoiceRagServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.stores = []
        base = Path(cls.tmp.name)
        cls.env = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(base / "data"), "TADAMOO_CACHE": str(base / "cache")})
        cls.env.start()
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner")), mock.patch.object(handler, "_RAG", None), mock.patch.object(handler, "_RAG_EMBED", None)]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.port = cls.httpd.server_address[1]
        cls.token = voice_service.ensure_token().read_text(encoding="utf-8").strip()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        held = handler._RAG
        for p in cls.patches:
            p.stop()
        cls.env.stop()
        for store in (held, *cls.stores):
            if store is not None:
                store.router.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.svc.api_key = "page-key"

    def call(self, method, path, body=None, key=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Authorization": "Bearer " + (key or self.token)}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            conn.close()

    def test_the_voice_token_opens_the_voice_routes_and_not_the_archives_themselves(self):
        status, data = self.call("POST", "/api/voice/rag/passages", {"function": "nope"})
        self.assertEqual((status, data["error"]["type"]), (400, "voice_rag"))                      # reached the route: the answer is the route's own
        self.assertEqual(self.call("POST", "/api/voice/rag/passages", {"function": "copilot", "archives": [], "query": "x"})[0], 200)
        for method, path in (("POST", "/api/rag/search"), ("GET", "/api/rag/catalog"), ("POST", "/api/rag/recall"), ("GET", "/api/voice/consent"),
                             ("POST", "/api/voice/consent/answer")):
            self.assertEqual(self.call(method, path, {} if method == "POST" else None)[0], 401, path)

    def test_a_question_goes_from_the_service_to_the_page_and_the_answer_comes_back(self):
        from tadamoo.rag import RagStore
        store = handler._RAG or RagStore(Path(self.tmp.name) / "data" / "rag")
        self.stores.append(store)
        aid = store.create("Boats", project="a" * 12)["id"]
        status, out = self.call("POST", "/api/voice/rag/ask", {"session": "m1", "function": "meeting", "archive_id": aid, "preview": "the keel", "how": "spoken"})
        self.assertEqual(status, 200)
        qid = out["question"]["id"]
        status, page = self.call("GET", "/api/voice/consent", key="page-key")
        self.assertEqual((status, [q["id"] for q in page["questions"]]), (200, [qid]))
        self.assertEqual(page["questions"][0]["archive"], "Boats")
        status, got = self.call("POST", "/api/voice/consent/answer", {"id": qid, "answer": "meeting"}, key="page-key")
        self.assertEqual((status, got["question"]["answer"], got["question"]["by"]), (200, "meeting", "dialog"))
        status, seen = self.call("GET", f"/api/voice/rag/ask/{qid}")
        self.assertEqual((status, seen["question"]["state"], seen["question"]["answer"]), (200, "answered", "meeting"))
        self.assertEqual(self.call("GET", "/api/voice/consent", key="page-key")[1]["questions"], [])


if __name__ == "__main__":
    unittest.main()
