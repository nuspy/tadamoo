"""Tests for /api/tasks and /api/changes HTTP endpoints."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import handler
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


class _Server(unittest.TestCase):
    api_key = ""

    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "Hello.", max_context=4096), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = NoTelemetry()
        cls.svc.api_key = cls.api_key
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = mock.patch.object(handler, "_TASKS", TaskJournal(Path(cls.tmp.name)))
        cls.patch.start()
        cls.patch_changes = mock.patch.object(handler, "_CHANGES", ChangeJournal(Path(cls.tmp.name)))
        cls.patch_changes.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.patch.stop()
        cls.patch_changes.stop()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None, headers=None, raw=None):
        data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode("utf-8"))
        hdrs = {"Content-Type": "application/json"} if method == "POST" else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


class ApiTasksTest(_Server):
    def test_tasks_lifecycle_api(self):
        # 1. GET /api/tasks
        code, data = self.call("GET", "/api/tasks")
        self.assertEqual(code, 200)
        self.assertIn("tasks", data)

        # 2. POST /api/tasks (record)
        code, res = self.call("POST", "/api/tasks", {"id": "http_test_task", "status": "running", "metadata": {"origin": "test"}})
        self.assertEqual(code, 200)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["task"]["id"], "http_test_task")
        self.assertEqual(res["task"]["status"], "running")

        # 3. GET verifies task exists
        code, data2 = self.call("GET", "/api/tasks")
        ids = [t["id"] for t in data2["tasks"]]
        self.assertIn("http_test_task", ids)

        # 4. POST /api/tasks (close)
        code, res_close = self.call("POST", "/api/tasks", {"id": "http_test_task", "action": "close"})
        self.assertEqual(code, 200)
        self.assertEqual(res_close["status"], "closed")

    def test_changes_api(self):
        code, data = self.call("GET", "/api/changes")
        self.assertEqual(code, 200)
        self.assertIn("schema", data)

    def test_tasks_post_rejects_bad_requests(self):
        code, _ = self.call("POST", "/api/tasks", raw=b"[1, 2]")
        self.assertEqual(code, 400)
        code, _ = self.call("POST", "/api/tasks", {"status": "running"})
        self.assertEqual(code, 400)
        code, _ = self.call("POST", "/api/tasks", {"id": "x"}, headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        code, _ = self.call("POST", "/api/tasks", {"id": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        code, data = self.call("GET", "/api/tasks")
        self.assertNotIn("x", [t["id"] for t in data["tasks"]])


class ApiTasksKeyedTest(_Server):
    api_key = "s3cret"
    auth = {"Authorization": "Bearer s3cret"}

    def test_the_records_need_the_key(self):
        for path in ("/api/tasks", "/api/changes"):
            self.assertEqual(self.call("GET", path)[0], 401, path)
            self.assertEqual(self.call("GET", path, headers=self.auth)[0], 200, path)

    def test_no_record_file_is_served_from_the_assets(self):
        # the journals live in the user's data folder; the assets hold no JSON, so none of these names is a file to fetch
        for path in ("/web/tasks.json", "/web/changes.json", "/web/logs.json"):
            self.assertEqual(self.call("GET", path, headers=self.auth)[0], 404, path)

    def test_post_needs_the_key(self):
        self.assertEqual(self.call("POST", "/api/tasks", {"id": "k"})[0], 401)
        self.assertEqual(self.call("POST", "/api/tasks", {"id": "k"}, headers=self.auth)[0], 200)

    def test_scripts_stay_public(self):
        req = urllib.request.Request(self.base + "/web/tasks.js")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
