"""Model discovery, on-demand loading and safe unloading over the actual HTTP API."""
import json
import io
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve.frontend import ChatTemplate
from serve.engine.errors import EngineDied, EngineStuck
from serve.engine.mock import MockEngine
from serve.engine.process import ProcessEngine
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer


class ResidentEngine(ProcessEngine):
    def __init__(self, tok):
        self.reply = MockEngine(tok, "Hello.", max_context=4096)
        self.max_context, self.info, self.last = 4096, {}, {}
        self.loaded, self.starts, self.closes = False, 0, 0
        self.unloaded = True
        self.progress = None

    def alive(self):
        return self.loaded

    def exit_code(self):
        return None

    def restart(self, tries=3):
        self.loaded = True
        self.starts += 1
        self.unloaded = False

    def close(self):
        self.loaded = False
        self.closes += 1

    def unload(self):
        self.close()
        self.unloaded = True

    def generate(self, *args, **kwargs):
        yield from self.reply.generate(*args, **kwargs)


class Lifecycle(unittest.TestCase):
    def test_close_sends_eof_before_waiting_for_windows_reader(self):
        engine = ProcessEngine("missing-executable", [], lazy=True)
        proc = mock.Mock()
        proc.stdin, proc.stdout = io.StringIO(), io.StringIO()
        proc.poll.side_effect = [None, 0]
        proc.wait.side_effect = lambda **kwargs: self.assertTrue(proc.stdin.closed)
        engine.proc = proc
        engine.close()
        self.assertIsNone(engine.proc)
        proc.kill.assert_not_called()

    def test_close_does_not_forget_a_process_still_exiting(self):
        engine = ProcessEngine("missing-executable", [], lazy=True)
        proc = mock.Mock()
        proc.stdin, proc.stdout = io.StringIO(), io.StringIO()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired("engine", 2)
        engine.proc = proc
        with self.assertRaisesRegex(EngineStuck, "still releasing"):
            engine.close()
        self.assertIs(engine.proc, proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()

    def test_close_terminates_before_it_kills(self):
        # an engine that ignores QUIT is terminated first; kill is the last resort
        engine = ProcessEngine("missing-executable", [], lazy=True)
        proc = mock.Mock()
        proc.stdin, proc.stdout = io.StringIO(), io.StringIO()
        proc.poll.side_effect = [None, 0]
        proc.wait.side_effect = [subprocess.TimeoutExpired("engine", 20), 0]
        engine.proc = proc
        engine.close()
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        self.assertIsNone(engine.proc)

    def test_native_lazy_constructor_does_not_start_a_process(self):
        engine = ProcessEngine("missing-executable", ["--max-context", "16384"], lazy=True)
        self.assertFalse(engine.alive())
        self.assertEqual(engine.max_context, 16384)
        self.assertIsNone(engine.exit_code())
        engine.close()

    def setUp(self):
        tok = ByteTokenizer()
        self.engine = ResidentEngine(tok)
        self.svc = Service(self.engine, tok, ChatTemplate(Path(__file__).resolve().parents[1] / "chat_template.jinja"))
        self.svc.control_token = "control-secret"
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, path, body=None, headers=None):
        secret = {"X-Basalt-Control": self.svc.control_token} if path.startswith("/api/") else {}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **secret, **(headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read().decode()
            return response.status, data if "event-stream" in response.headers.get("Content-Type", "") else json.loads(data)

    def test_listen_first_shows_loading_and_requests_wait_for_the_load(self):
        # --listen-first: the HTTP server answers while the engine starts; a chat waits for the load, no second start
        import threading
        gate, began = threading.Event(), threading.Event()
        plain = self.engine.restart

        def slow_restart(tries=3):
            began.set()
            gate.wait(10)
            plain(tries)
        self.engine.restart = slow_restart
        loader = threading.Thread(target=self.svc.load_at_start)
        loader.start()
        self.assertTrue(began.wait(5))
        self.assertTrue(self.request("/v1/status")[1]["loading"])
        self.assertEqual(self.request("/metrics")[1]["live"]["state"], "loading")
        chat = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 32, "reasoning_effort": "none"}
        out = {}
        asker = threading.Thread(target=lambda: out.update(answer=self.request("/v1/chat/completions", chat)))
        asker.start()
        time.sleep(0.3)
        self.assertNotIn("answer", out)                  # waiting for the load, not refused
        gate.set()
        loader.join(5)
        asker.join(10)
        self.assertEqual(out["answer"][0], 200)
        self.assertEqual(out["answer"][1]["choices"][0]["message"]["content"], "Hello.")
        self.assertEqual(self.engine.starts, 1)
        self.assertFalse(self.request("/v1/status")[1]["loading"])
        self.assertEqual(self.request("/metrics")[1]["live"]["state"], "idle")

    def test_discover_chat_unload_and_stream_reload(self):
        self.assertEqual(self.request("/api/health")[1]["service"], "basalt")
        self.assertFalse(self.request("/v1/status")[1]["loaded"])
        self.assertEqual(self.request("/v1/models")[1]["data"][0]["status"]["value"], "unloaded")
        self.assertTrue(self.request("/props")[1]["models_autoload"])
        self.assertEqual(self.engine.starts, 0)
        chat = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 32, "reasoning_effort": "none"}
        for _ in range(2):
            code, answer = self.request("/v1/chat/completions", chat)
            self.assertEqual(code, 200)
            self.assertEqual(answer["choices"][0]["message"]["content"], "Hello.")
        self.assertEqual(self.engine.starts, 1)
        self.assertEqual(self.request("/api/control/unload", {})[1], {"status": "unloaded"})
        code, stream = self.request("/v1/chat/completions", {**chat, "stream": True})
        self.assertEqual(code, 200)
        self.assertIn("data: [DONE]", stream)
        self.assertEqual(self.engine.starts, 2)

    def test_busy_auth_and_foreign_origin_cannot_unload(self):
        self.engine.loaded = True
        with self.svc.fifo:
            self.assertEqual(self.request("/api/control/unload", {})[0], 409)
        self.svc.api_key = "local-secret"
        self.assertEqual(self.request("/api/control/unload", {})[0], 401)
        self.assertEqual(self.request("/api/control/unload", {}, {"Authorization": "Bearer local-secret",
                                                        "Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.engine.closes, 0)
        self.assertTrue(self.engine.loaded)
        self.assertEqual(self.request("/api/control/unload", {}, {"Authorization": "Bearer local-secret"})[0], 200)

    def test_an_engine_that_died_on_load_is_reported_as_such(self):
        # EngineDied is a RuntimeError: it keeps its own answer ("the next request restarts it")
        def died():
            raise EngineDied("the engine ended while it started")
        self.engine.restart = died
        code, body = self.request("/v1/chat/completions", {"messages": [{"role": "user", "content": "Hello"}]})
        self.assertEqual(code, 503)
        self.assertIn("the next request restarts it", body["error"]["message"])

    def test_explicit_load_and_the_secret_of_the_control_routes(self):
        self.assertEqual(self.engine.starts, 0)
        code, status = self.request("/api/control/load", {})
        self.assertEqual((code, status), (200, {"status": "loaded"}))
        self.assertEqual(self.engine.starts, 1)
        self.assertEqual(self.request("/api/control/load", {}, {"Content-Type": "text/plain"})[0], 415)
        for headers in ({"X-Basalt-Control": "wrong"}, {"X-Basalt-Control": ""}):
            self.assertEqual(self.request("/api/control/unload", {}, headers)[0], 403, headers)
        self.svc.control_token = ""                                  # a server that wrote no secret has the routes off
        self.assertEqual(self.request("/api/control/unload", {}, {"X-Basalt-Control": "anything"})[0], 403)
        self.assertEqual(self.engine.closes, 0)

    def test_the_old_routes_are_gone_for_clients(self):
        for path in ("/v1/load", "/v1/unload", "/load", "/unload"):
            self.assertEqual(self.request(path, {})[0], 404, path)
        self.assertEqual(self.request("/v1/status")[0], 200)


if __name__ == "__main__":
    unittest.main()
