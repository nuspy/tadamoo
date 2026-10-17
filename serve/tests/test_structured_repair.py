"""The retry message of a rejected structured answer (serve/api/repair.py), and structured streams that show their
reasoning. Mock engine only."""
import json
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from serve.api.openai import repair_note, retry_messages
from serve.api.repair import build_repair
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

try:
    import jsonschema  # noqa: F401
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False

REPEATED = "Gestione del tempo e delle priorità nei progetti complessi"
LONG_ERROR = "model did not return valid JSON: Unterminated string starting at: line 117 column 5 (char 5071)"


def incident(repeats=43):
    """A list of 48 strings, 43 of them the same, the last one cut inside a string."""
    entries = [f"Introduzione {i}" for i in range(5)] + [REPEATED] * repeats
    return ('{\n  "title": "Corso",\n  "summary": "' + "abc " * 75 + '",\n  "implicit_syllabus": [\n'
            + ",\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + ',\n    "' + REPEATED[:20])


class BuildRepair(unittest.TestCase):
    def test_the_real_incident(self):
        bad = incident()
        r = build_repair(LONG_ERROR, bad)
        self.assertTrue(r.message.startswith("Your previous answer was rejected: " + LONG_ERROR), r.message)
        self.assertIn("/implicit_syllabus", r.message)
        self.assertEqual(r.message.count(REPEATED), 1)
        self.assertIn("43 times", r.message)
        self.assertIn("entries 6-48", r.message)
        self.assertNotIn(bad, r.message)
        self.assertNotIn("Introduzione 3", r.message)
        self.assertLess(len(r.message), 1500)
        self.assertEqual((r.cut, r.valid_json, r.repeat["count"], r.repeat["positions"][:2]), (True, False, 43, [6, 7]))
        self.assertIn("Do not repeat", r.message)
        self.assertEqual(r.keys, ["title", "summary", "implicit_syllabus"])

    def test_a_huge_text_stays_a_short_message(self):
        bad = '{"a": ["' + '", "'.join(["zz yy"] * 600000) + '", "cut'
        self.assertGreater(len(bad), 4_000_000)
        r = build_repair("model did not return valid JSON: Unterminated string", bad)
        self.assertLess(len(r.message), 1500)
        self.assertIn("600000 times", r.message)
        self.assertEqual(r.message.count("zz yy"), 1)

    def test_a_schema_error_on_valid_json_has_no_repetition(self):
        good = json.dumps({"title": "a", "items": ["x", "y"]})
        r = build_repair("model output failed the JSON Schema at /items: too short", good)
        self.assertTrue(r.message.startswith("Your previous answer was rejected: model output failed the JSON Schema "
                                             "at /items: too short."))
        self.assertTrue(r.valid_json)
        self.assertIsNone(r.repeat)
        self.assertNotIn("times", r.message)
        self.assertNotIn("Do not repeat", r.message)
        self.assertIn("title (text, 1 chars), items (list of 2, 2 different)", r.message)
        self.assertEqual(r.path, "/")

    def test_a_text_that_stops_being_json(self):
        r = build_repair("model did not return valid JSON: Expecting ',' delimiter", '{"a": [1,2, {"b": nope}]}')
        self.assertEqual(r.path, "/a/2/b")
        self.assertFalse(r.cut)
        self.assertIn("stopped being valid JSON at /a/2/b", r.message)

    def test_a_cut_text_says_where_it_ended(self):
        r = build_repair("e", '{"a": {"b": [1, 2, 3')
        self.assertTrue(r.cut)
        self.assertEqual(r.path, "/a/b/2")
        self.assertIn("ended unfinished at entry 3 of the list /a/b", r.message)

    def test_a_line_repeated_inside_one_string(self):
        bad = '{"text": "' + "Need infer from previous truncated answer.\n" * 30 + "abc"
        r = build_repair("model did not return valid JSON: Invalid control character", bad)
        self.assertEqual(r.repeat["kind"], "line")
        count = r.repeat["count"]
        self.assertGreaterEqual(count, 29)                       # the first line starts with the JSON opening
        self.assertEqual(r.message.count("Need infer from previous truncated answer."), 1)
        self.assertIn(f"written {count} times", r.message)
        self.assertEqual(repair_note(r), f"at /text, line written {count} times")

    def test_empty_text_prose_and_fences(self):
        r = build_repair("model did not return valid JSON: Expecting value", "")
        self.assertTrue(r.cut)
        self.assertIn("The answer was empty.", r.message)
        r = build_repair("e", "Sure! Here is the result:\n```json\n{\"a\": tr")
        self.assertEqual(r.path, "/a")
        self.assertTrue(r.cut)
        r = build_repair("e", "Just prose, no JSON at all.")
        self.assertTrue(r.message.startswith("Your previous answer was rejected: e."))
        self.assertNotIn("Just prose", r.message)

    def test_a_long_error_is_cut(self):
        r = build_repair("word " * 500, "{}")
        self.assertLess(len(r.message), 900)

    def test_retry_messages_adds_one_user_message_without_the_bad_text(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        out = retry_messages(messages, incident(), LONG_ERROR)
        self.assertEqual(out[:2], messages)
        self.assertEqual([m["role"] for m in out], ["system", "user", "user"])
        self.assertNotIn("Introduzione", out[2]["content"])
        self.assertEqual(out[2]["content"], build_repair(LONG_ERROR, incident()).message)
        self.assertEqual(len(messages), 2)                      # the request's own list is not changed


SCHEMA = {"type": "object", "properties": {"title": {"type": "string"},
                                           "implicit_syllabus": {"type": "array", "items": {"type": "string"}}},
          "required": ["title", "implicit_syllabus"], "additionalProperties": False}
FORMAT = {"type": "json_schema", "json_schema": {"name": "syllabus", "strict": True, "schema": SCHEMA}}
GOOD = json.dumps({"title": "Corso", "implicit_syllabus": ["Una voce", "Un'altra voce"]})


class Server(unittest.TestCase):
    """A mock-engine server on a free port."""

    def request(self, path, body=None, headers=None, method=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read().decode()
            return response.status, data if "event-stream" in response.headers.get("Content-Type", "") else json.loads(data)

    def start(self, script, **kw):
        self.tok = ByteTokenizer()
        self.engine = MockEngine(self.tok, script, max_context=32768)
        self.svc = Service(self.engine, self.tok, ChatTemplate(Path(__file__).resolve().parents[1] / "chat_template.jinja"))
        self.svc.telemetry = _NoTelemetry()
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def chat(self, **kwargs):
        req = {"messages": [{"role": "system", "content": "Write a syllabus."},
                            {"role": "user", "content": "Project management."}],
               "response_format": FORMAT, "reasoning_effort": "none", **kwargs}
        return self.request("/v1/chat/completions", req)

    def prompt(self, n):
        return self.tok.decode(self.engine.requests[n][0])


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema is not installed")
class RetryOverHttp(Server):
    def test_the_incident_is_not_sent_back(self):
        bad = incident()
        self.start([bad, GOOD])
        code, reply = self.chat()
        self.assertEqual(code, 200, reply)
        self.assertEqual(json.loads(reply["choices"][0]["message"]["content"]), json.loads(GOOD))
        self.assertEqual(len(self.engine.requests), 2)
        second = self.prompt(1)
        marker = "Your previous answer was rejected: "
        self.assertIn(marker, second)
        repair = second[second.index(marker):]
        self.assertTrue(repair.startswith(marker + "model did not return valid JSON: Unterminated string"), repair[:200])
        self.assertIn("/implicit_syllabus", repair)
        self.assertEqual(second.count(REPEATED), 1)
        self.assertIn("43 times", repair)
        self.assertIn("entries 6-48", repair)
        self.assertNotIn(bad, second)
        self.assertNotIn("Introduzione 3", second)
        self.assertEqual(second.count(marker), 1)
        self.assertEqual(self.prompt(0).count(marker), 0)
        # the first prompt is the second's prefix, so the engine's cache keeps it
        first_ids, second_ids = self.engine.requests[0][0], self.engine.requests[1][0]
        shared = next((i for i, (x, y) in enumerate(zip(first_ids, second_ids)) if x != y), len(first_ids))
        self.assertGreater(shared, len(first_ids) - 40)

    def test_a_schema_only_failure_is_repaired_without_repetition(self):
        self.start([json.dumps({"title": "Corso"}), GOOD])
        code, reply = self.chat()
        self.assertEqual(code, 200, reply)
        second = self.prompt(1)
        repair = second[second.index("Your previous answer was rejected: "):]
        self.assertIn("model output failed the JSON Schema at /: 'implicit_syllabus' is a required property", repair)
        self.assertNotIn("times", repair)
        self.assertNotIn("Do not repeat", repair)
        self.assertIn("Already produced, at the top level: title (text, 5 chars)", repair)
        self.assertNotIn('"title"', repair)
        self.assertEqual(reply["structured_output"]["attempts"], 2)

    def test_a_cut_answer_without_closing_text_is_repaired_too(self):
        self.start(['{"title": "Corso", "implicit_syllabus": ["a", "b", "c', GOOD])
        code, reply = self.chat()
        self.assertEqual(code, 200, reply)
        self.assertIn("ended unfinished at entry 3 of the list /implicit_syllabus", self.prompt(1))


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema is not installed")
class ReasoningStreams(Server):
    def stream(self, **kwargs):
        code, raw = self.chat(stream=True, reasoning_effort="low", **kwargs)
        self.assertEqual(code, 200)
        return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")], raw

    def test_reasoning_arrives_before_the_end_and_a_rejected_try_is_marked(self):
        bad = incident()
        self.start(["First thoughts.</think>\n" + bad, "Second thoughts.</think>\n" + GOOD])
        chunks, raw = self.stream()
        self.assertEqual(len({c["id"] for c in chunks}), 1)
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant", "content": ""})
        kinds = []
        for c in chunks:
            delta = c["choices"][0]["delta"]
            marker = c.get("structured_output")
            kinds.append("marker" if marker and marker.get("discarded") is True else
                         "reasoning" if delta.get("reasoning_content") else
                         "content" if delta.get("content") else "other")
        # first try's reasoning, then its marker, then the second try's reasoning, then the validated answer
        compact = [k for i, k in enumerate(kinds) if k != "other" and (i == 0 or k != kinds[i - 1])]
        self.assertEqual(compact, ["reasoning", "marker", "reasoning", "content"])
        thoughts = "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks)
        self.assertEqual(thoughts, "First thoughts.Second thoughts.")
        marker = next(c for c in chunks if (c.get("structured_output") or {}).get("discarded") is True)
        self.assertEqual(marker["choices"][0]["delta"], {})
        self.assertEqual(marker["structured_output"]["attempt"], 1)
        self.assertTrue(marker["structured_output"]["retrying"])
        self.assertIn("Unterminated string", marker["structured_output"]["reason"])
        content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        self.assertEqual(json.loads(content), json.loads(GOOD))               # nothing of the rejected try
        self.assertNotIn("Introduzione", raw)
        # the re-emission at the end holds the answer only: the reasoning is not repeated
        self.assertNotIn("reasoning_content", chunks[-1]["choices"][0]["delta"])
        self.assertNotIn("reasoning_content", chunks[-2]["choices"][0]["delta"])
        final = chunks[-1]
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")
        self.assertEqual(final["structured_output"]["attempts"], 2)
        self.assertTrue(final["structured_output"]["validated"])
        self.assertFalse(final["structured_output"]["constrained"])
        self.assertEqual(len(final["structured_output"]["discarded"]), 1)
        self.assertEqual(final["structured_output"]["discarded"][0]["attempt"], 1)
        self.assertIn("usage", final)
        self.assertIn("data: [DONE]", raw)

    def test_reasoning_of_a_first_good_try_is_streamed_once(self):
        self.start(["Only thoughts.</think>\n" + GOOD])
        chunks, _ = self.stream()
        self.assertEqual("".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks), "Only thoughts.")
        self.assertFalse(any((c.get("structured_output") or {}).get("discarded") for c in chunks))
        self.assertNotIn("discarded", chunks[-1]["structured_output"])
        self.assertEqual(chunks[-1]["structured_output"]["attempts"], 1)

    def test_a_last_failure_ends_with_a_marker_and_the_error(self):
        self.start(["Thinking.</think>\n" + incident()])
        chunks, raw = self.stream(structured_retries=1)
        markers = [c["structured_output"] for c in chunks if (c.get("structured_output") or {}).get("discarded") is True]
        self.assertEqual([(m["attempt"], m["retrying"]) for m in markers], [(1, True), (2, False)])
        self.assertIn('"structured_output_failed"', raw)
        self.assertFalse("".join(ch["delta"].get("content", "") for c in chunks for ch in c.get("choices", [])))

    def test_a_json_object_request_streams_reasoning_too(self):
        self.start(["Pondering.</think>\n[1]", "More pondering.</think>\n{\"ok\": true}"])
        chunks, _ = self.stream(response_format={"type": "json_object"})
        self.assertEqual("".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks),
                         "Pondering.More pondering.")
        self.assertEqual("".join(c["choices"][0]["delta"].get("content", "") for c in chunks), '{"ok":true}')

    def test_without_streaming_only_the_last_tries_text_is_returned(self):
        self.start(["First thoughts.</think>\n" + incident(), "Second thoughts.</think>\n" + GOOD])
        code, reply = self.chat(reasoning_effort="low")
        self.assertEqual(code, 200, reply)
        message = reply["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "Second thoughts.")
        self.assertEqual(json.loads(message["content"]), json.loads(GOOD))
        self.assertEqual(reply["choices"][0]["finish_reason"], "stop")
        so = reply["structured_output"]
        self.assertEqual((so["attempts"], so["validated"], so["constrained"]), (2, True, False))
        self.assertEqual([d["attempt"] for d in so["discarded"]], [1])
        self.assertIn("Unterminated string", so["discarded"][0]["reason"])

    def test_status_says_what_is_held_and_what_streams(self):
        self.start(GOOD)
        code, status = self.request("/v1/status")
        self.assertEqual(code, 200)
        so = status["structured_output"]
        self.assertTrue(so["stream_buffered"])                  # older clients read this one
        self.assertTrue(so["content_buffered"])
        self.assertTrue(so["reasoning_streamed"])
        self.assertEqual(so["formats"], ["json_object", "json_schema"])
        self.assertEqual(status["request_log"]["enabled"], True)
        self.assertGreater(status["request_log"]["capacity_chars"], 0)


class ResponsesStreamsReasoning(Server):
    def test_a_json_text_format_streams_reasoning_and_holds_the_answer(self):
        self.start(["Planning the object.</think>\n{\"ok\": true}"])
        code, raw = self.request("/v1/responses", {"input": "Say it.", "stream": True, "reasoning": {"effort": "low"},
                                                   "text": {"format": {"type": "json_object"}}})
        self.assertEqual(code, 200)
        names = [line[7:] for line in raw.splitlines() if line.startswith("event: ")]
        self.assertIn("response.reasoning_text.delta", names)
        self.assertLess(names.index("response.reasoning_text.delta"), names.index("response.completed"))
        self.assertEqual(names.count("response.output_text.delta"), 1)
        self.assertLess(names.index("response.reasoning_text.delta"), names.index("response.output_text.delta"))
        self.assertIn("Planning the object.", raw)


if __name__ == "__main__":
    unittest.main()
