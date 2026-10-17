"""The request log: the store (serve/util/reqlog.py), what the server writes into it for each API, and its endpoints.
Mock engine only."""
import contextlib
import http.client
import io
import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from serve.config import build_parser, load_config_file, parse_settings
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.logbuf import LogBuffer, TeeStream
from serve.util.reqlog import GATHER_CHARS, RequestLog, export_jsonl, export_text
from serve.util.reqtrace import request_params
from serve.util.text import ByteTokenizer

try:
    import jsonschema  # noqa: F401
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Store(unittest.TestCase):
    def test_ids_rise_and_each_event_has_its_fields(self):
        log = RequestLog()
        rid = log.new_request()
        a = log.add(rid, "request", {"endpoint": "/v1/x"}, "body")
        b = log.add(rid, "finish", {"finish_reason": "stop"})
        c = log.add(None, "server", {"level": "info"}, "a line")
        self.assertEqual((a, b, c), (1, 2, 3))
        out = log.since(0)
        self.assertEqual([e["id"] for e in out["events"]], [1, 2, 3])
        first = out["events"][0]
        self.assertEqual((first["req"], first["kind"], first["text"], first["data"]), (rid, "request", "body", {"endpoint": "/v1/x"}))
        self.assertNotIn("text", out["events"][1])
        self.assertIsNone(out["events"][2]["req"])
        self.assertEqual((out["enabled"], out["epoch"], out["last"], out["skipped"]), (True, 0, 3, 0))
        self.assertEqual(out["dropped"], {"events": 0, "chars": 0})
        self.assertEqual(log.new_request(), "r2")

    def test_pieces_are_gathered_into_one_event(self):
        clock = Clock()
        log = RequestLog(clock=clock)
        for text in ("one ", "two ", "three"):
            log.piece("r1", "reasoning", text, attempt=1)
        self.assertEqual(log.since(0)["events"], [])             # still gathering
        clock.t += 1
        events = log.since(0)["events"]                          # older than the gathering time: stored
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["kind"], events[0]["text"], events[0]["data"]), ("reasoning", "one two three", {"attempt": 1}))

    def test_a_different_kind_or_event_closes_the_gathering_in_order(self):
        log = RequestLog(clock=Clock())
        log.piece("r1", "reasoning", "think")
        log.piece("r1", "content", "answer")
        log.piece("r2", "content", "other request")
        log.add("r1", "finish")
        kinds = [(e["req"], e["kind"]) for e in log.since(0)["events"]]
        self.assertEqual(kinds, [("r1", "reasoning"), ("r1", "content"), ("r1", "finish")])    # r2 is still gathering
        self.assertEqual(log.snapshot()[-1]["text"], "other request")

    def test_a_big_piece_is_stored_at_once(self):
        log = RequestLog(clock=Clock())
        log.piece("r1", "reasoning", "x" * GATHER_CHARS)
        self.assertEqual(len(log.since(0)["events"]), 1)

    def test_since_after_and_the_size_limit(self):
        log = RequestLog()
        for i in range(10):
            log.add("r1", "server", None, f"line {i} " + "x" * 1000)
        self.assertEqual([e["id"] for e in log.since(7)["events"]], [8, 9, 10])
        self.assertEqual(log.since(10)["events"], [])
        small = log.since(0, max_chars=3500)
        self.assertEqual([e["id"] for e in small["events"]], [8, 9, 10])    # the newest ones fit
        self.assertEqual(small["skipped"], 7)
        self.assertEqual(log.since(0)["skipped"], 0)

    def test_the_budget_drops_the_oldest_and_counts_them(self):
        log = RequestLog()
        log.max_chars = 5000
        for i in range(30):
            log.add(None, "server", None, f"{i:02d} " + "y" * 400)
        out = log.since(0)
        self.assertGreater(out["dropped"]["events"], 0)
        ids = [e["id"] for e in out["events"]]
        self.assertEqual(ids[-1], 30)
        self.assertEqual(len(ids) + out["dropped"]["events"], 30)
        self.assertLessEqual(out["held_chars"], 5000)
        self.assertEqual(out["capacity_chars"], 5000)
        self.assertGreater(out["dropped"]["chars"], 0)
        self.assertIn("were discarded", "".join(export_text(log)))
        self.assertEqual(next(iter(export_jsonl(log))).count("dropped_events"), 1)

    def test_clear_empties_and_starts_a_new_epoch_but_ids_keep_rising(self):
        log = RequestLog()
        log.add(None, "server", None, "a")
        log.piece("r1", "content", "pending")
        log.clear()
        out = log.since(0)
        self.assertEqual((out["events"], out["epoch"], out["held_chars"], out["dropped"]["events"]), ([], 1, 0, 0))
        self.assertEqual(log.add(None, "server", None, "b"), 2)
        self.assertEqual([e["text"] for e in log.since(0)["events"]], ["b"])

    def test_export_text_and_jsonl(self):
        log = RequestLog()
        log.add("r1", "request", {"endpoint": "/v1/chat/completions", "params": {"model": "m"}}, '{\n  "a": "è"\n}')
        log.piece("r1", "reasoning", "thinking è")
        log.add("r1", "finish", {"finish_reason": "stop"})
        text = "".join(export_text(log))
        self.assertTrue(text.startswith("# Request log, saved "))
        self.assertRegex(text, r"\[\d\d:\d\d:\d\d\.\d{3}\] r1\s+REQUEST\n")
        self.assertIn('  "endpoint": "/v1/chat/completions"', text)          # data as indented JSON
        self.assertIn('{\n  "a": "è"\n}', text)                              # the raw text as it is
        self.assertRegex(text, r"REASONING\nthinking è")
        self.assertIn("FINISH", text)
        lines = "".join(export_jsonl(log)).splitlines()
        head = json.loads(lines[0])
        self.assertEqual((head["kind"], head["epoch"], head["dropped_events"]), ("log", 0, 0))
        self.assertEqual([json.loads(x)["kind"] for x in lines[1:]], ["request", "reasoning", "finish"])
        self.assertIn("è", lines[1])                                          # UTF-8, not escaped

    def test_a_disabled_log_keeps_nothing(self):
        log = RequestLog(enabled=False)
        self.assertIsNone(log.add("r1", "request"))
        log.piece("r1", "content", "x")
        out = log.since(0)
        self.assertEqual((out["enabled"], out["events"], out["last"]), (False, [], 0))
        self.assertEqual("".join(export_text(log)).count("REQUEST"), 0)

    def test_threads_get_distinct_ids(self):
        log = RequestLog()
        threads = [threading.Thread(target=lambda: [log.add(None, "server", None, "x") for _ in range(200)]) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([e["id"] for e in log.since(0)["events"]], list(range(1, 801)))

    def test_request_params_keep_the_plain_fields_only(self):
        params = request_params({"model": "m", "temperature": 0.5, "top_p": 0.9, "max_tokens": 10, "stop": ["a", "b"],
                                 "tools": [{}, {}], "seed": 4, "stream": True, "api_key": "secret", "metadata": {"k": 1},
                                 "response_format": {"type": "json_schema", "json_schema": {"name": "n", "schema": {}}},
                                 "reasoning_effort": "low", "structured_retries": 1, "messages": [1, 2, 3]})
        self.assertEqual(params, {"model": "m", "temperature": 0.5, "top_p": 0.9, "max_tokens": 10, "stop": ["a", "b"],
                                  "tools_count": 2, "seed": 4, "stream": True, "reasoning_effort": "low",
                                  "structured_retries": 1, "response_format": {"type": "json_schema", "name": "n"},
                                  "messages_count": 3})
        self.assertEqual(request_params({"reasoning": {"effort": "high"}, "text": {"format": {"type": "json_object"}},
                                         "max_output_tokens": 5})["response_format"], {"type": "json_object"})
        self.assertEqual(request_params({"thinking": {"type": "enabled", "budget_tokens": 9}})["thinking"],
                         {"type": "enabled", "budget_tokens": 9})
        self.assertFalse(request_params({})["stream"])


class FakeTelemetry:
    """The sampler's snapshot with one card; nothing is read from the machine."""

    def snapshot(self):
        return {"now": {"gpus": [{"index": 0, "util": 55, "mem_used": 20 << 30, "mem_total": 32 << 30, "temp": 61,
                                  "power": 300.0}], "cpu": 12.0, "ram_used": 40 << 30, "ram_total": 64 << 30},
                "history": {}, "static": {"gpus": [{"index": 0, "name": "Test GPU"}]}}


class NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


class Http(unittest.TestCase):
    def start(self, script="Thinking.</think>\n\nHello there.", telemetry=True, delay=0.0, key=""):
        self.tok = ByteTokenizer()
        self.engine = MockEngine(self.tok, script, max_context=32768, delay_s=delay)
        self.svc = Service(self.engine, self.tok, ChatTemplate(Path(__file__).resolve().parents[1] / "chat_template.jinja"))
        self.svc.telemetry = FakeTelemetry() if telemetry else NoTelemetry()
        self.svc.api_key = key
        self.httpd = serve(self.svc, port=0)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, path, body=None, headers=None, method=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            text = response.read().decode()
            ctype = response.headers.get("Content-Type", "")
            return response.status, (text if "json" not in ctype or "ndjson" in ctype else json.loads(text)), response.headers

    def log(self, after=0):
        code, out, _ = self.call(f"/api/reqlog?after={after}")
        self.assertEqual(code, 200)
        return out

    def kinds(self, rid=None):
        return [e["kind"] for e in self.log()["events"] if rid is None or e["req"] == rid]

    def events(self, kind):
        return [e for e in self.log()["events"] if e["kind"] == kind]

    CHAT = {"messages": [{"role": "user", "content": "Say hello, è."}], "reasoning_effort": "low", "temperature": 0.2,
            "max_tokens": 500, "stop": ["zzz"]}


class ChatRequests(Http):
    def test_a_plain_request_leaves_its_whole_story_in_order(self):
        self.start()
        code, reply, _ = self.call("/v1/chat/completions", self.CHAT)
        self.assertEqual(code, 200, reply)
        out = self.log()
        events = out["events"]
        rid = events[0]["req"]
        self.assertEqual([e["kind"] for e in events],
                         ["request", "gpu", "timing", "reasoning", "content", "timing", "gpu", "finish"])
        self.assertEqual({e["req"] for e in events}, {rid})
        request = events[0]
        self.assertEqual(request["data"]["endpoint"], "/v1/chat/completions")
        self.assertEqual(request["data"]["method"], "POST")
        self.assertRegex(request["data"]["client"], r"^127\.0\.0\.1:\d+$")
        self.assertFalse(request["data"]["stream"])
        self.assertEqual(request["data"]["params"], {"temperature": 0.2, "max_tokens": 500, "stop": ["zzz"],
                                                     "reasoning_effort": "low", "stream": False, "messages_count": 1})
        self.assertEqual(request["text"], json.dumps(self.CHAT, indent=2, ensure_ascii=False))     # the body, whole
        self.assertIn("è", request["text"])
        self.assertEqual(events[3]["text"], "Thinking.")
        self.assertEqual(events[4]["text"].strip(), "Hello there.")
        first = events[2]["data"]
        self.assertEqual(first["prompt_tokens"], len(self.engine.requests[0][0]))
        self.assertIn("first_token_s", first)
        done = events[5]["data"]
        self.assertEqual((done["prompt_tokens"], done["cached_tokens"]), (first["prompt_tokens"], 0))
        self.assertGreater(done["completion_tokens"], 0)
        self.assertEqual(events[1]["data"]["when"], "start")
        self.assertEqual(events[6]["data"]["when"], "end")
        self.assertEqual(events[1]["data"]["gpus"], [{"index": 0, "name": "Test GPU", "used_mib": 20480, "total_mib": 32768,
                                                      "util_pct": 55, "temp_c": 61, "power_w": 300.0}])
        self.assertEqual(events[1]["data"]["ram"], {"used_mib": 40960, "total_mib": 65536})
        finish = events[7]["data"]
        self.assertEqual(finish["finish_reason"], "stop")
        self.assertEqual(finish["usage"]["prompt_tokens"], first["prompt_tokens"])
        self.assertGreaterEqual(finish["duration_s"], 0)
        self.assertNotIn("attempts", finish)
        self.assertNotIn("Authorization", json.dumps(events))

    def test_a_streamed_request_logs_the_same_pieces(self):
        self.start()
        code, raw, _ = self.call("/v1/chat/completions", {**self.CHAT, "stream": True})
        self.assertEqual(code, 200)
        self.assertIn("data: [DONE]", raw)
        events = self.log()["events"]
        self.assertEqual([e["kind"] for e in events],
                         ["request", "gpu", "timing", "reasoning", "content", "timing", "gpu", "finish"])
        self.assertTrue(events[0]["data"]["stream"])
        self.assertEqual(events[3]["text"], "Thinking.")

    def test_the_cursor_returns_only_what_is_new(self):
        self.start()
        self.call("/v1/chat/completions", self.CHAT)
        first = self.log()
        self.call("/v1/chat/completions", self.CHAT)
        later = self.log(first["last"])
        self.assertTrue(all(e["id"] > first["last"] for e in later["events"]))
        self.assertEqual([e["kind"] for e in later["events"]][0], "request")
        self.assertNotEqual(later["events"][0]["req"], first["events"][0]["req"])

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema is not installed")
    def test_a_structured_retry_logs_every_try_and_the_repair_message(self):
        entries = ",".join(json.dumps(x) for x in ["one"] + ["same entry here"] * 5)
        bad = '{"items": [' + entries + ', "cut'
        good = '{"items": ["a", "b"]}'
        self.start(["Try one thoughts.</think>\n" + bad, "Try two thoughts.</think>\n" + good], telemetry=False)
        schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                  "required": ["items"]}
        body = {**self.CHAT, "response_format": {"type": "json_schema", "json_schema": {"name": "s", "schema": schema}}}
        code, reply, _ = self.call("/v1/chat/completions", body)
        self.assertEqual(code, 200, reply)
        events = self.log()["events"]
        self.assertEqual([e["kind"] for e in events],
                         ["request", "timing", "reasoning", "content", "timing", "attempt", "repair",
                          "timing", "reasoning", "content", "timing", "attempt", "finish"])
        by = {}
        for e in events:
            by.setdefault(e["kind"], []).append(e)
        self.assertEqual([e["data"]["attempt"] for e in by["reasoning"]], [1, 2])
        self.assertEqual(by["content"][0]["text"], bad)                       # the discarded try's text is in the log
        self.assertEqual(by["content"][1]["text"], good)
        first, second = by["attempt"]
        self.assertEqual((first["data"]["ok"], first["data"]["discarded"], first["data"]["retrying"]), (False, True, True))
        self.assertIn("Unterminated string", first["data"]["error"])
        self.assertEqual(first["data"]["finish_reason"], "stop")
        self.assertEqual((second["data"]["ok"], second["data"]["discarded"]), (True, False))
        repair = by["repair"][0]
        self.assertEqual(repair["data"]["attempt"], 1)
        self.assertEqual(repair["data"]["path"], "/items/6")
        self.assertEqual(repair["data"]["repeated"]["count"], 5)
        self.assertEqual((repair["data"]["repeated"]["first"], repair["data"]["repeated"]["last"]), (2, 6))
        self.assertTrue(repair["text"].startswith("Your previous answer was rejected: model did not return valid JSON"))
        self.assertIn(repair["text"], self.tok.decode(self.engine.requests[1][0]))     # exactly what was sent
        self.assertNotIn(bad, repair["text"])
        self.assertEqual(events[-1]["data"]["attempts"], 2)
        self.assertEqual(events[0]["data"]["params"]["response_format"], {"type": "json_schema", "name": "s"})

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema is not installed")
    def test_a_structured_failure_ends_with_the_error(self):
        self.start('{"not": "a list"}', telemetry=False)
        fmt = {"type": "json_schema", "json_schema": {"name": "s", "schema": {
            "type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}}}
        code, reply, _ = self.call("/v1/chat/completions", {**self.CHAT, "response_format": fmt, "structured_retries": 0})
        self.assertEqual(code, 502)
        kinds = self.kinds()
        self.assertEqual(kinds[-3:], ["attempt", "error", "finish"])
        error = self.events("error")[0]["data"]
        self.assertEqual(error["type"], "structured_output_failed")
        self.assertTrue(error["message"].startswith("502: "))
        self.assertFalse(self.events("attempt")[0]["data"]["retrying"])

    def test_a_refused_request_is_logged_with_its_error(self):
        self.start(telemetry=False)
        code, reply, _ = self.call("/v1/chat/completions", {**self.CHAT, "response_format": {"type": "json_object"},
                                                           "structured_retries": 9})
        self.assertEqual(code, 400)
        self.assertEqual(self.kinds(), ["request", "error", "finish"])
        self.assertEqual(self.events("request")[0]["data"]["params"]["structured_retries"], 9)
        self.assertIn("structured_retries", self.events("error")[0]["data"]["message"])
        self.assertIsNone(self.events("finish")[0]["data"]["finish_reason"])

    def test_a_body_that_is_not_json_is_kept_as_it_came(self):
        self.start(telemetry=False)
        code, _, _ = self.call("/v1/chat/completions", raw=b'{"messages": [oops')
        self.assertEqual(code, 400)
        request = self.events("request")[0]
        self.assertTrue(request["data"]["unparsed"])
        self.assertEqual(request["text"], '{"messages": [oops')
        self.assertEqual(self.kinds(), ["request", "error", "finish"])

    def test_requests_that_are_not_generation_are_not_logged(self):
        self.start(telemetry=False)
        self.call("/v1/messages/count_tokens", {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.call("/v1/status")
        self.call("/health")
        self.assertEqual(self.log()["events"], [])

    def test_a_client_that_leaves_is_logged_as_a_cancel(self):
        self.start("x " * 400, telemetry=False, delay=0.02)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", json.dumps({**self.CHAT, "stream": True, "reasoning_effort": "none"}),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.fp.readline()
        response.fp.readline()
        response.close()
        conn.close()
        deadline = time.time() + 10
        while "finish" not in self.kinds() and time.time() < deadline:
            time.sleep(0.1)
        kinds = self.kinds()
        self.assertIn("cancel", kinds)
        self.assertEqual(kinds[-1], "finish")
        self.assertEqual(kinds.count("cancel"), 1)
        self.assertEqual(self.events("finish")[0]["data"]["finish_reason"], "cancel")


class OtherApis(Http):
    def test_responses_reasoning_and_answer(self):
        self.start(telemetry=False)
        body = {"input": "Say hello.", "reasoning": {"effort": "low"}, "max_output_tokens": 300, "temperature": 0.4}
        code, reply, _ = self.call("/v1/responses", body)
        self.assertEqual(code, 200, reply)
        code, raw, _ = self.call("/v1/responses", {**body, "stream": True})
        self.assertEqual(code, 200)
        events = self.log()["events"]
        requests = [e for e in events if e["kind"] == "request"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["data"]["endpoint"], "/v1/responses")
        self.assertEqual(requests[0]["data"]["params"]["reasoning"], {"effort": "low"})
        self.assertEqual(requests[0]["data"]["params"]["max_output_tokens"], 300)
        self.assertTrue(requests[1]["data"]["stream"])
        for rid in (requests[0]["req"], requests[1]["req"]):
            mine = [e for e in events if e["req"] == rid]
            self.assertEqual([e["kind"] for e in mine], ["request", "timing", "reasoning", "content", "timing", "finish"])
            self.assertEqual(mine[2]["text"], "Thinking.")
            self.assertEqual(mine[3]["text"].strip(), "Hello there.")

    def test_messages_thinking_and_text(self):
        self.start(telemetry=False)
        body = {"model": "m", "max_tokens": 300, "messages": [{"role": "user", "content": "hello"}],
                "thinking": {"type": "enabled", "budget_tokens": 2000}, "system": "Be brief."}
        for stream in (False, True):
            code, reply, _ = self.call("/v1/messages", {**body, "stream": stream})
            self.assertEqual(code, 200, reply)
        events = self.log()["events"]
        requests = [e for e in events if e["kind"] == "request"]
        self.assertEqual([r["data"]["endpoint"] for r in requests], ["/v1/messages"] * 2)
        self.assertEqual(requests[0]["data"]["params"]["thinking"], {"type": "enabled", "budget_tokens": 2000})
        self.assertEqual(requests[0]["data"]["params"]["max_tokens"], 300)
        for r in requests:
            mine = [e for e in events if e["req"] == r["req"]]
            self.assertEqual([e["kind"] for e in mine], ["request", "timing", "reasoning", "content", "timing", "finish"])
            self.assertEqual(mine[2]["text"], "Thinking.")
            self.assertEqual(mine[3]["text"].strip(), "Hello there.")
        self.assertEqual(json.loads(requests[0]["text"])["system"], "Be brief.")


class Hardware(Http):
    def test_figures_every_so_often_while_a_request_runs(self):
        self.start("word " * 60, delay=0.01)
        self.svc.gpu_ticker.every = 0.05
        self.call("/v1/chat/completions", {**self.CHAT, "reasoning_effort": "none"})
        gpu = self.events("gpu")
        whens = [e["data"]["when"] for e in gpu]
        self.assertEqual(whens[0], "start")
        self.assertEqual(whens[-1], "end")
        ticks = [e for e in gpu if e["data"]["when"] == "tick"]
        self.assertGreaterEqual(len(ticks), 2)
        rid = gpu[0]["req"]
        self.assertTrue(all(e["req"] is None and e["data"]["requests"] == [rid] for e in ticks))
        self.assertEqual(self.log()["events"][-1]["kind"], "finish")
        time.sleep(0.3)
        after = len(self.events("gpu"))
        time.sleep(0.3)
        self.assertEqual(len(self.events("gpu")), after)                    # nothing while idle

    def test_no_readings_no_events(self):
        self.start(telemetry=False)
        self.call("/v1/chat/completions", self.CHAT)
        self.assertEqual(self.events("gpu"), [])
        self.assertIn("finish", self.kinds())

    def test_the_servers_own_lines_and_the_engines(self):
        self.start(telemetry=False)
        self.svc.logs.add("srv", "model unloaded after 60 s idle, the next request loads it")
        self.svc.logs.add("eng", "CUDA error: out of memory")
        buffer = LogBuffer()
        self.svc.logs = buffer                                            # a new buffer is wired too
        tee = TeeStream(io.StringIO(), buffer, "srv")
        tee.write("[server] warning: the reply repeated one token\nhalf a li")
        tee.write("ne\n")
        events = self.log()["events"]
        self.assertEqual([(e["kind"], e["data"]["level"], e["text"], e["req"]) for e in events], [
            ("server", "info", "model unloaded after 60 s idle, the next request loads it", None),
            ("engine", "err", "CUDA error: out of memory", None),
            ("server", "warn", "warning: the reply repeated one token", None),
            ("server", "info", "half a line", None)])
        self.assertEqual([x["msg"] for x in buffer.since(0)[0]][0], "warning: the reply repeated one token")   # /api/logs as before


class Endpoints(Http):
    def test_authorization_follows_the_api_key(self):
        self.start(key="s3cret", telemetry=False)
        auth = {"Authorization": "Bearer s3cret"}
        for path in ("/api/reqlog", "/api/reqlog/export"):
            self.assertEqual(self.call(path)[0], 401, path)
            self.assertEqual(self.call(path, headers={"Authorization": "Bearer wrong"})[0], 401, path)
            self.assertEqual(self.call(path, headers=auth)[0], 200, path)
            self.assertEqual(self.call(path, headers={"x-api-key": "s3cret"})[0], 200, path)
        self.assertEqual(self.call("/api/reqlog/clear", {}, method="POST")[0], 401)
        self.call("/v1/chat/completions", self.CHAT, headers=auth)
        code, out, _ = self.call("/api/reqlog", headers=auth)
        self.assertEqual(code, 200)
        self.assertIn("request", [e["kind"] for e in out["events"]])
        self.assertNotIn("s3cret", json.dumps(out))                         # the key is never logged
        self.assertNotIn("s3cret", self.call("/api/reqlog/export", headers=auth)[1])
        self.assertEqual(self.call("/api/reqlog/clear", {}, headers=auth, method="POST")[0], 200)

    def test_the_host_check_applies_without_a_key(self):
        self.start(telemetry=False)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/reqlog", headers={"Host": "evil.example"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_export_is_a_download_in_text_or_jsonl(self):
        self.start(telemetry=False)
        self.call("/v1/chat/completions", self.CHAT)
        code, text, headers = self.call("/api/reqlog/export")
        self.assertEqual(code, 200)
        self.assertRegex(headers["Content-Disposition"], r'^attachment; filename="basalt-requests-\d{8}-\d{6}\.log"$')
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("REQUEST", text)
        self.assertIn("Hello there.", text)
        self.assertIn("è", text)
        code, text, headers = self.call("/api/reqlog/export?format=jsonl")
        self.assertRegex(headers["Content-Disposition"], r'^attachment; filename="basalt-requests-\d{8}-\d{6}\.jsonl"$')
        self.assertTrue(headers["Content-Type"].startswith("application/x-ndjson"))
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(rows[0]["kind"], "log")
        self.assertEqual([r["kind"] for r in rows[1:]], self.kinds())
        self.assertEqual(self.call("/api/reqlog/export?format=xml")[0], 400)

    def test_clear_needs_the_own_page_and_empties_the_log(self):
        self.start(telemetry=False)
        self.call("/v1/chat/completions", self.CHAT)
        before = self.log()
        self.assertTrue(before["events"])
        self.assertEqual(self.call("/api/reqlog/clear", {}, headers={"Content-Type": "text/plain"}, method="POST")[0], 415)
        self.assertEqual(self.call("/api/reqlog/clear", {}, headers={"Origin": "http://evil.example"}, method="POST")[0], 403)
        self.assertTrue(self.log()["events"])                                 # refused: nothing cleared
        code, body, _ = self.call("/api/reqlog/clear", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"}, method="POST")
        self.assertEqual((code, body), (200, {"cleared": True}))
        after = self.log()
        self.assertEqual((after["events"], after["epoch"], after["held_chars"]), ([], before["epoch"] + 1, 0))
        self.assertEqual(self.call("/api/reqlog/clear", method="GET")[0], 404)

    def test_the_status_says_what_the_log_is(self):
        self.start(telemetry=False)
        _, status, _ = self.call("/v1/status")
        self.assertEqual(status["request_log"], {"enabled": True, "capacity_chars": 32 << 20})

    def test_a_disabled_log_answers_and_keeps_nothing(self):
        self.start(telemetry=False)
        from serve.util.reqlog import RequestLog as Log
        self.svc.reqlog = Log(enabled=False, max_chars=1 << 20)
        code, reply, _ = self.call("/v1/chat/completions", self.CHAT)
        self.assertEqual(code, 200)
        self.assertEqual(self.call("/v1/chat/completions", {**self.CHAT, "stream": True})[0], 200)
        out = self.log()
        self.assertEqual((out["enabled"], out["events"], out["last"]), (False, [], 0))
        self.assertEqual(self.call("/api/reqlog/export")[0], 200)
        self.assertEqual(self.call("/api/reqlog/clear", {}, method="POST")[0], 200)
        self.assertEqual(self.call("/v1/status")[1]["request_log"], {"enabled": False, "capacity_chars": 1 << 20})


class Settings(unittest.TestCase):
    def parse(self, cli=(), server=None):
        ap = build_parser()
        argv = list(cli)
        if server is not None:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "c.json"
                path.write_text(json.dumps({"server": server}), encoding="utf-8")
                file_argv, _ = load_config_file(str(path), ap)
            argv = file_argv + argv
        return ap.parse_known_args(argv)[0]

    def test_on_and_32_mib_by_default(self):
        a = self.parse()
        self.assertEqual((a.request_log, a.request_log_mib), ("on", 32.0))

    def test_the_command_line(self):
        a = self.parse(["--request-log", "off", "--request-log-mib", "64"])
        self.assertEqual((a.request_log, a.request_log_mib), ("off", 64.0))
        a, *_ = parse_settings(build_parser(), ["--mock-engine", "--request-log", "off"])
        self.assertEqual(a.request_log, "off")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse(["--request-log", "maybe"])

    def test_the_config_file_takes_true_or_false(self):
        self.assertEqual(self.parse(server={"request_log": False}).request_log, "off")
        self.assertEqual(self.parse(server={"request_log": True}).request_log, "on")
        a = self.parse(server={"request_log": False, "request_log_mib": 8})
        self.assertEqual((a.request_log, a.request_log_mib), ("off", 8.0))
        self.assertEqual(self.parse(["--request-log", "on"], server={"request_log": False}).request_log, "on")


if __name__ == "__main__":
    unittest.main()
