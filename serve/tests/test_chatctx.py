"""The chat page's context policies (serve/chatctx.py and POST /api/chat/context), against the mock engine."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve import chatctx  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = "Facts: the user builds a boat called Nimbus-7."


def chat(turns: int, size: int = 300) -> list[dict]:
    """A system prompt, then `turns` user/assistant pairs of about `size` bytes each, then a new user message."""
    out = [{"role": "system", "content": "SYSTEM PROMPT"}, {"role": "user", "content": "FIRST " + "a" * size}]
    out.append({"role": "assistant", "content": "reply 0 " + "b" * size})
    for i in range(1, turns):
        out += [{"role": "user", "content": f"user {i} " + "c" * size},
                {"role": "assistant", "content": f"reply {i} " + "d" * size}]
    out.append({"role": "user", "content": "NEWEST question"})
    return out


class Policies(unittest.TestCase):
    def setUp(self):
        tok = ByteTokenizer()
        self.engine = MockEngine(tok, SUMMARY, max_context=4096)
        self.svc = Service(self.engine, tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))

    def fit(self, messages, policy, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return chatctx.fit(self.svc, {"messages": messages, "policy": policy, **kw})

    def test_small_conversations_are_left_alone(self):
        for policy in chatctx.POLICIES:
            r = self.fit(chat(1, 50), policy)
            self.assertEqual(r["action"], "none", policy)
            self.assertEqual(r["limit"], 4096)
            self.assertGreater(r["used"], 100)
        self.assertEqual(self.engine.requests, [])

    def test_stop_reports_over(self):
        r = self.fit(chat(9), "stop")
        self.assertEqual(r["action"], "over")
        self.assertEqual(r["keep"], list(range(len(chat(9)))))

    def test_rolling_keeps_head_and_newest_turns(self):
        msgs = chat(9)
        r = self.fit(msgs, "rolling", max_tokens=256)
        self.assertEqual(r["action"], "dropped")
        keep = r["keep"]
        self.assertEqual(keep[:2], [0, 1])                       # system prompt and first user message
        self.assertEqual(keep[-1], len(msgs) - 1)                # the new question
        self.assertEqual(keep, sorted(keep))
        tail = keep[2:]
        self.assertEqual(tail, list(range(tail[0], len(msgs))))  # the newest turns, contiguous and verbatim
        self.assertEqual(msgs[tail[0]]["role"], "user")          # whole turns only
        self.assertLessEqual(r["used"], r["limit"] - 8 - 256)
        self.assertTrue(r["dropped"])
        self.assertEqual(self.engine.requests, [])               # no model call

    def test_rolling_cannot_cut_the_head(self):
        msgs = [{"role": "system", "content": "s" * 3000}, {"role": "user", "content": "u" * 1200}]
        self.assertEqual(self.fit(msgs, "rolling")["action"], "over")

    def test_compact_calls_the_model_once_and_keeps_head_and_tail(self):
        msgs = chat(5, 300)
        r = self.fit(msgs, "compact", max_tokens=128, keep_turns=2, threshold=0.5)
        self.assertEqual(r["action"], "compacted", r)
        self.assertEqual(len(self.engine.requests), 1)           # one summary call
        prompt_bytes = self.svc.tok.decode(self.engine.requests[0][0])
        self.assertIn("compact the middle of a conversation", prompt_bytes)
        self.assertIn("user 1", prompt_bytes)
        self.assertNotIn("SYSTEM PROMPT", prompt_bytes)          # the head is not summarized
        self.assertEqual(r["summary"]["text"], SUMMARY)
        self.assertTrue(r["summary"]["message"]["content"].startswith(chatctx.SUMMARY_PREFIX))
        n = len(msgs)
        self.assertEqual(r["keep"], [0, 1, n - 3, n - 2, n - 1])   # head + the last full turn + the new question
        self.assertEqual(r["summary"]["replaces"], list(range(2, n - 3)))
        self.assertLess(r["used"], r["used_before"])

    def test_compact_waits_for_the_threshold(self):
        msgs = chat(2, 200)
        r = self.fit(msgs, "compact", threshold=0.95, max_tokens=128)
        self.assertEqual(r["action"], "none")
        self.assertEqual(self.engine.requests, [])

    def test_bad_requests(self):
        for req in ({"messages": [], "policy": "stop"}, {"messages": chat(1), "policy": "trim"},
                    {"messages": chat(1), "threshold": 2}, {"messages": "hi"}):
            with self.assertRaises(ValueError, msg=req):
                chatctx.fit(self.svc, req)

    def test_turns(self):
        msgs = chat(3)
        self.assertEqual(chatctx.head_of(msgs), [0, 1])
        self.assertEqual(chatctx.head_of(msgs[1:]), [0])
        self.assertEqual(chatctx.turns_after(msgs, 2), [[2], [3, 4], [5, 6], [7]])


class OverHttp(unittest.TestCase):
    def setUp(self):
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, SUMMARY, max_context=2048), tok,
                           ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, body, **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                c.request("POST", "/api/chat/context", body=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", **headers})
                r = c.getresponse()
                return r.status, json.loads(r.read())
        finally:
            c.close()

    def test_meter_and_guards(self):
        code, body = self.post({"messages": chat(1, 20)})
        self.assertEqual(code, 200)
        self.assertEqual((body["action"], body["limit"]), ("none", 2048))
        self.assertEqual(self.post({"messages": chat(1)}, Origin="http://evil.example.com")[0], 403)
        self.assertEqual(self.post({"messages": []})[0], 400)
        self.svc.api_key = "k"
        self.assertEqual(self.post({"messages": chat(1)})[0], 401)
        self.assertEqual(self.post({"messages": chat(1)}, Authorization="Bearer k")[0], 200)


if __name__ == "__main__":
    unittest.main()
