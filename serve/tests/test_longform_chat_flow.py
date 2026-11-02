"""The Chat's `long_answer` goes through the flow of serve/longflow.py: a scripted engine answers each kind of call (estimate, plan, part,
check, rewrite of a passage) by what its prompt says, and the tests read the calls made and the events and text that come out."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import re
import sys
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve.engine.mock import MockEngine  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TOKENS = ByteTokenizer()
WORDS = ["".join(p) for p in itertools.product("abcdefghijklmnop", repeat=3)]


def varied(n: int, start: int = 0) -> str:
    return " ".join(WORDS[start + i] for i in range(n))


def body(k: int) -> str:
    return f"Body of part {k}. " + varied(900, 600 * (k - 1)) + "."


REVISED = "Revised passage of part 2. " + varied(900, 2600) + "."
PLAN = json.dumps({"title": "Rivers", "chunks": [
    {"id": k, "heading": f"S{k}", "goal": f"goal {k}", "target_words": 1200, "key_points": ["a"], "instruction": f"Write section {k} of the text."} for k in (1, 2, 3)]})
FIX = json.dumps({"fixes": [{"op": "replace", "chunk": 2, "anchor": " ".join(body(2).split()[:8]), "instruction": "write it again, tighter"}]})
KINDS = [("You estimate the length", "estimate"), ("You plan a long text", "plan"), ("You check a long text", "check"), ("You edit one passage", "fix")]
SHORT_PART = "A short text. " + varied(400, 100) + "."
SCRIPTS = {"short_part": SHORT_PART, "translated": "Il fiume corre verso il mare.", "estimate_long": '{"words": 3600, "reason": "a long text"}', "estimate_short": '{"words": 500, "reason": "short"}',
           "plan": PLAN, "check_none": '{"fixes": []}', "check_fix": FIX, "fix": REVISED}


class Rig:
    """A server on the scripted engine; `calls` lists the kind of each call it received, `check` picks what the check answers."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.estimate, self.check = "estimate_long", "check_none"
        order = [*[body(k) for k in (1, 2, 3)], *SCRIPTS.values()]
        self.names = ["part1", "part2", "part3", *SCRIPTS]
        self.service = Service(MockEngine(TOKENS, order, max_context=65536, route=self.route), TOKENS, ChatTemplate(ROOT / "serve/chat_template.jinja"))

    def route(self, ids) -> int:
        text = TOKENS.decode(ids)
        for marker, kind in KINDS:
            if marker in text:
                self.calls.append(kind)
                return self.names.index({"estimate": self.estimate, "check": self.check}.get(kind, kind))
        asked = re.findall(r"Part (\d+) of (\d+)", text)
        self.calls.append("part")
        if asked and asked[-1][1] == "1":                                   # a text of one part: the short script, or the translation
            return self.names.index("translated" if "Translate the source" in text else "short_part")
        return int(asked[-1][0]) - 1 if asked else 0


class ChatLongFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rig = Rig()
        cls.httpd = serve(cls.rig.service, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.rig.calls.clear()
        self.rig.estimate, self.rig.check = "estimate_long", "check_none"

    def post(self, long_answer, stream=True):
        req = urllib.request.Request(self.base + "/v1/chat/completions", headers={"Content-Type": "application/json"}, data=json.dumps(
            {"messages": [{"role": "user", "content": "Write about rivers"}], "long_answer": long_answer, "stream": stream}).encode())
        with contextlib.redirect_stdout(io.StringIO()), urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
        if not stream:
            return json.loads(raw)
        chunks = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:") and "[DONE]" not in line]
        events = [c["long_answer"] for c in chunks if c.get("long_answer")]
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        return chunks, events, text

    def test_a_length_the_request_declares_is_not_estimated_but_planned_written_and_checked(self):
        chunks, events, text = self.post({"mode": "write", "target_words": 3600})
        self.assertNotIn("estimate", self.rig.calls)
        self.assertEqual(self.rig.calls, ["plan", "part", "part", "part", "check"])
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[:2], ["estimate", "plan"])
        self.assertEqual(events[0]["source"], "asked")
        self.assertEqual([k for k in kinds if k == "segment_done"], ["segment_done"] * 3)
        self.assertEqual(kinds[-3:], ["verify", "review", "done"])
        self.assertEqual([text.index(f"Body of part {k}.") for k in (1, 2, 3)], sorted(text.index(f"Body of part {k}.") for k in (1, 2, 3)))
        last = chunks[-1]
        self.assertEqual((last["choices"][0]["finish_reason"], last["long_answer"]["segments"], last["long_answer"]["applied"]), ("stop", 3, 0))
        self.assertEqual(last["usage"]["total_tokens"], last["usage"]["prompt_tokens"] + last["usage"]["completion_tokens"])

    def test_a_length_not_declared_is_estimated_by_a_short_call_first(self):
        _chunks, events, text = self.post(True)
        self.assertEqual(self.rig.calls[:2], ["estimate", "plan"])
        self.assertEqual((events[0]["event"], events[0]["source"], events[0]["words"], events[0]["long"]), ("estimate", "model", 3600, True))
        self.assertIn("Body of part 3.", text)

    def test_a_short_estimate_still_writes_the_text_in_one_part(self):
        self.rig.estimate = "estimate_short"
        chunks, events, text = self.post(True)
        self.assertEqual(self.rig.calls, ["estimate", "part"])
        self.assertEqual([e["event"] for e in events[:2]], ["estimate", "plan"])
        self.assertFalse(events[0]["long"])
        self.assertIn("A short text.", text)
        self.assertEqual(chunks[-1]["long_answer"]["segments"], 1)

    def test_a_plan_given_in_the_request_is_followed_and_nothing_is_asked_of_the_model_about_it(self):
        outline = [{"heading": f"S{k}", "goal": f"goal {k}", "target_words": 1200, "key_points": ["a"]} for k in (1, 2, 3)]
        _chunks, events, _text = self.post({"mode": "write", "outline": outline, "segment_words": 1200})
        self.assertEqual(self.rig.calls, ["part", "part", "part", "check"])
        self.assertEqual([e["event"] for e in events if e["event"] in ("estimate", "plan")], ["plan"])

    def test_a_correction_replaces_the_text_shown_and_the_plain_answer_has_the_corrected_whole(self):
        self.rig.check = "check_fix"
        chunks, events, shown = self.post({"mode": "write", "target_words": 3600})
        revised = [e for e in events if e["event"] == "revised"]
        self.assertEqual(len(revised), 1)
        self.assertIn("Body of part 2.", shown)                              # what was shown while the parts were written
        self.assertIn("Revised passage of part 2.", revised[0]["text"])
        self.assertNotIn("Body of part 2.", revised[0]["text"])
        self.assertEqual(chunks[-1]["long_answer"]["applied"], 1)
        self.rig.calls.clear()
        body_ = self.post({"mode": "write", "target_words": 3600}, stream=False)
        content = body_["choices"][0]["message"]["content"]
        self.assertIn("Revised passage of part 2.", content)
        self.assertNotIn("Body of part 2.", content)
        self.assertIn("Body of part 3.", content)
        self.assertEqual(body_["long_answer"]["applied"], 1)

    def test_a_translation_is_not_planned_or_checked_by_the_flow(self):
        self.rig.calls.clear()
        req = urllib.request.Request(self.base + "/v1/chat/completions", headers={"Content-Type": "application/json"}, data=json.dumps(
            {"messages": [{"role": "user", "content": "The river runs to the sea."}], "stream": False,
             "long_answer": {"mode": "translate", "target_language": "Italian"}}).encode())
        with contextlib.redirect_stdout(io.StringIO()), urllib.request.urlopen(req, timeout=60) as r:
            body_ = json.loads(r.read().decode())
        self.assertEqual(self.rig.calls, ["part"])
        self.assertIn("long_answer", body_)


if __name__ == "__main__":
    unittest.main()
