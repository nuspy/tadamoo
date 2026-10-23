"""Tests for /api/tasks and /api/changes HTTP endpoints."""
import json
import unittest
import urllib.request
from pathlib import Path

from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


class ApiTasksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "Hello.", max_context=4096), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = NoTelemetry()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get_json(self, path):
        req = urllib.request.Request(self.base + path, method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def post_json(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_tasks_lifecycle_api(self):
        # 1. GET /api/tasks
        code, data = self.get_json("/api/tasks")
        self.assertEqual(code, 200)
        self.assertIn("tasks", data)

        # 2. POST /api/tasks (record)
        code, res = self.post_json("/api/tasks", {"id": "http_test_task", "status": "running", "metadata": {"origin": "test"}})
        self.assertEqual(code, 200)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["task"]["id"], "http_test_task")
        self.assertEqual(res["task"]["status"], "running")

        # 3. GET verifies task exists
        code, data2 = self.get_json("/api/tasks")
        ids = [t["id"] for t in data2["tasks"]]
        self.assertIn("http_test_task", ids)

        # 4. POST /api/tasks (close)
        code, res_close = self.post_json("/api/tasks", {"id": "http_test_task", "action": "close"})
        self.assertEqual(code, 200)
        self.assertEqual(res_close["status"], "closed")

    def test_changes_api(self):
        code, data = self.get_json("/api/changes")
        self.assertEqual(code, 200)
        self.assertIn("schema", data)
