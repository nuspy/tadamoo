"""The `long_answer` request field, max_answer_tokens and the passage-loop guard, against the mock engine."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.ngram import NgramLoop  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TOKENS = ByteTokenizer()


WORDS = ["".join(p) for p in itertools.product("abcdefghijklmnop", repeat=3)]       # distinct three-letter words


def varied(n: int, start: int = 0) -> str:
    return " ".join(WORDS[start + i] for i in range(n))


def part_body(k: int) -> str:
    return f"Body of part {k}. " + varied(520, 600 * k) + "."


def route(ids) -> int:
    asked = re.findall(r"Part (\d+) of", TOKENS.decode(ids))
    return int(asked[-1]) if asked else 0


def make_service(ctx: int = 65536) -> Service:
    scripts = ['{"fixes": []}', *[part_body(k) for k in range(1, 4)]]       # the check of the flow finds nothing to fix
    return Service(MockEngine(TOKENS, scripts, max_context=ctx, route=route), TOKENS, ChatTemplate(ROOT / "serve/chat_template.jinja"))


OUTLINE = [{"heading": f"S{i}", "goal": f"goal {i}", "target_words": 700, "key_points": ["a"]} for i in (1, 2, 3)]


class LongAnswerApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = make_service()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, body):
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def long(self, **extra):
        return {"messages": [{"role": "user", "content": "Write about rivers"}],
                "long_answer": {"mode": "write", "target_words": 2100, "segment_words": 700, "outline": OUTLINE, **extra}, **{k: v for k, v in extra.items() if k == "stream"}}

    def test_a_streamed_long_answer_has_progress_content_and_totals(self):
        with contextlib.redirect_stdout(io.StringIO()):
            status, raw = self.post({**self.long(), "stream": True})
        self.assertEqual(status, 200)
        chunks = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:") and "[DONE]" not in line]
        events = [c["long_answer"] for c in chunks if c.get("long_answer")]
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "plan")
        self.assertEqual([e["index"] for e in events if e["event"] == "segment_start"], [1, 2, 3])
        self.assertEqual(kinds[-3:], ["verify", "review", "done"])          # a new text is checked after its parts are written
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        for k in (1, 2, 3):
            self.assertEqual(text.count(f"Body of part {k}."), 1)
        self.assertLess(text.index("Body of part 1."), text.index("Body of part 2."))
        last = chunks[-1]
        self.assertEqual(last["choices"][0]["finish_reason"], "stop")
        self.assertEqual(last["long_answer"]["segments"], 3)
        self.assertGreater(last["usage"]["completion_tokens"], 0)

    def test_a_plain_request_returns_the_text_and_the_report(self):
        with contextlib.redirect_stdout(io.StringIO()):
            status, raw = self.post(self.long())
        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["long_answer"]["of"], 3)
        self.assertIn("Body of part 3.", body["choices"][0]["message"]["content"])

    def test_bad_requests_are_400_before_anything_is_written(self):
        for bad in ({"messages": [{"role": "user", "content": "x"}], "long_answer": {"mode": "poem"}},
                    {"messages": [{"role": "user", "content": "x"}], "long_answer": {"mode": "translate"}},
                    {"messages": [{"role": "user", "content": "x"}], "long_answer": True, "tools": [{"type": "function", "function": {"name": "f"}}]},
                    {"messages": [{"role": "assistant", "content": "x"}], "long_answer": True}):
            status, raw = self.post({**bad, "stream": True})
            self.assertEqual(status, 400, raw)
            self.assertIn("long_answer", raw)

    def test_a_bad_answer_cap_is_400(self):
        status, raw = self.post({"messages": [{"role": "user", "content": "hi"}], "max_answer_tokens": "ten"})
        self.assertEqual(status, 400)
        self.assertIn("max_answer_tokens", raw)


class Guards(unittest.TestCase):
    def reply(self, text, limit=None, thinking=False, **req):
        svc = Service(MockEngine(TOKENS, text, max_context=8192), TOKENS, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        if limit is not None:
            svc.repeat_ngram = limit or None
        ids = TOKENS.encode("hi")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            done = [x for kind, x in svc.run(ids, thinking, None, 4000, req, threading.Event()) if kind == "done"][0]
        return done, out.getvalue()

    def test_the_answer_cap_ends_the_reply_as_length_and_says_so(self):
        done, log = self.reply(varied(400), max_answer_tokens=50)
        self.assertEqual((done["finish"], done["completion_tokens"], done.get("answer_capped")), ("length", 50, True))
        self.assertIn("answer cap of 50 tokens", log)
        done, _ = self.reply(varied(400))
        self.assertEqual((done["finish"], done["completion_tokens"]), ("stop", 1600))

    def test_the_cap_counts_the_answer_not_the_thinking(self):
        done, _ = self.reply(varied(60) + "\n</think>\n\n" + varied(100), thinking=True, max_answer_tokens=40)
        self.assertEqual((done["finish"], done.get("answer_capped")), ("length", True))
        self.assertGreaterEqual(done["reasoning_tokens"], 200)
        self.assertEqual(done["completion_tokens"] - done["reasoning_tokens"], 40)

    def test_a_passage_that_comes_back_ends_the_reply_as_a_loop(self):
        sentence = "the river runs to the sea and then it runs back again. "
        text = "An opening that is different. " + sentence * 60
        done, log = self.reply(text)
        self.assertEqual((done["finish"], done.get("repeated")), ("length", True))
        self.assertIn("repeated a passage of 12 tokens 4 times", log)
        self.assertLess(done["completion_tokens"], len(text) // 2)

    def test_normal_text_code_and_rules_are_left_alone(self):
        prose = varied(600)
        self.assertEqual(self.reply(prose)[0]["finish"], "stop")
        code = "```\n" + "self.assertEqual(response.status_code, 200)\n" * 20 + "```\nDone."
        self.assertEqual(self.reply(code)[0]["finish"], "stop")
        rule = "| " + " | ".join(["---"] * 30) + " |\n" + "ok."
        self.assertEqual(self.reply(rule)[0]["finish"], "stop")
        done, _ = self.reply(("loop passage that repeats here again. " * 30), limit=0)
        self.assertEqual(done["finish"], "stop")                           # off

    def test_json_output_is_not_watched_for_passages(self):
        rows = "[" + ",".join('{"name": "item", "value": %d, "ok": true}' % i for i in range(40)) + "]"
        self.assertEqual(self.reply(rows, response_format={"type": "json_object"})[0]["finish"], "stop")
        self.assertEqual(self.reply(rows)[0]["finish"], "length")           # the same text as plain prose is a loop

    def test_ngram_loop_counts_apart_and_expires(self):
        guard = NgramLoop(4, 3, 50)
        stream = [1, 2, 3, 4, 5] * 3
        self.assertEqual([guard.push(t) for t in stream].count(True) > 0, True)
        guard = NgramLoop(4, 3, 12)
        flags = [guard.push(t) for t in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] * 3]
        self.assertFalse(any(flags))                                       # the repeats are further apart than the window
        guard = NgramLoop(4, 2, 100)
        self.assertFalse(any(guard.push(t) for t in [1, 1, 2, 2] * 10))     # at most 3 different tokens: a pattern, not a passage


if __name__ == "__main__":
    unittest.main()
