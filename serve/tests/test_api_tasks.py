"""Tests for /api/tasks (the programs' way into the register of open work) and /api/changes HTTP endpoints."""
import json
import os
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
from serve.changes import ChangeJournal
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
        cls.svc = Service(MockEngine(tok, "Hello.", max_context=4096), tok, ChatTemplate.from_file(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = NoTelemetry()
        cls.svc.api_key = cls.api_key
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": cls.tmp.name})                     # the register of work reported from outside lives in the data folder
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
        # 1. GET /api/tasks: every open task of every folder, flat
        code, data = self.call("GET", "/api/tasks")
        self.assertEqual(code, 200)
        self.assertIn("tasks", data)

        # 2. POST /api/tasks (a report): the task is opened, running, with what is done so far
        code, res = self.call("POST", "/api/tasks", {"id": "http_test_task", "status": "running", "result": "step 1", "metadata": {"goal": "Rebuild the site", "origin": "test"}})
        self.assertEqual(code, 200)
        self.assertEqual(res["status"], "ok")
        self.assertEqual((res["task"]["id"], res["task"]["state"], res["task"]["title"], res["task"]["done"]), ("http_test_task", "running", "Rebuild the site", "step 1"))
        self.assertEqual(res["task"]["meta"]["origin"], "test")

        # 3. GET verifies it, with the fields a reader needs
        code, data2 = self.call("GET", "/api/tasks")
        row = next(t for t in data2["tasks"] if t["id"] == "http_test_task")
        self.assertEqual((row["status"], row["state"], row["result"], row["metadata"]["origin"]), ("running", "running", "step 1", "test"))

        # 4. a failed one stays, blocked, with the reason; a finished one is deleted
        self.call("POST", "/api/tasks", {"id": "http_test_task", "status": "failed", "result": "the model was not reachable"})
        row = next(t for t in self.call("GET", "/api/tasks")[1]["tasks"] if t["id"] == "http_test_task")
        self.assertEqual((row["state"], row["missing"], row["result"]), ("blocked", "the model was not reachable", "the model was not reachable"))
        code, ended = self.call("POST", "/api/tasks", {"id": "http_test_task", "status": "done"})
        self.assertEqual((code, ended["status"]), (200, "ended"))
        self.assertNotIn("http_test_task", [t["id"] for t in self.call("GET", "/api/tasks")[1]["tasks"]])

    def test_a_parent_names_its_subtasks_whichever_is_reported_first_and_closing_deletes(self):
        self.call("POST", "/api/tasks", {"id": "kid.1", "status": "pending", "metadata": {"title": "Part one", "depends_on": []}})
        self.call("POST", "/api/tasks", {"id": "parent", "status": "running", "children": ["kid.1", "kid.2"], "metadata": {"title": "The request"}})
        self.call("POST", "/api/tasks", {"id": "kid.2", "status": "pending", "metadata": {"title": "Part two"}})
        rows = {t["id"]: t for t in self.call("GET", "/api/tasks")[1]["tasks"]}
        self.assertEqual((rows["kid.1"]["parent"], rows["kid.2"]["parent"], rows["parent"]["children"]), ("parent", "parent", ["kid.1", "kid.2"]))
        self.assertEqual(rows["kid.1"]["state"], "queued")
        self.call("POST", "/api/tasks", {"id": "parent", "action": "close"})
        self.assertEqual([t["id"] for t in self.call("GET", "/api/tasks")[1]["tasks"] if t["id"] in ("parent", "kid.1", "kid.2")], [])

    def test_the_folder_a_report_names_decides_the_register(self):
        folder = Path(self.tmp.name) / "unknown-folder"
        folder.mkdir()
        self.call("POST", "/api/tasks", {"id": "elsewhere", "status": "running", "project": str(folder), "metadata": {"title": "Run elsewhere"}})
        row = next(t for t in self.call("GET", "/api/tasks")[1]["tasks"] if t["id"] == "elsewhere")
        self.assertEqual(Path(row["project"]), Path(self.tmp.name) / "journal")                 # a folder nobody added has no register of its own: the data folder's
        self.assertFalse((folder / ".tadamoo").exists())
        self.call("POST", "/api/tasks", {"id": "elsewhere", "action": "close"})

    def test_changes_api(self):
        code, data = self.call("GET", "/api/changes")
        self.assertEqual(code, 200)
        self.assertIn("schema", data)

    def test_tasks_post_rejects_bad_requests(self):
        code, _ = self.call("POST", "/api/tasks", raw=b"[1, 2]")
        self.assertEqual(code, 400)
        code, _ = self.call("POST", "/api/tasks", {"status": "running"})
        self.assertEqual(code, 400)
        code, _ = self.call("POST", "/api/tasks", {"id": "x", "status": "nonsense"})
        self.assertEqual(code, 400)
        code, _ = self.call("POST", "/api/tasks", {"id": "bad id!", "status": "running"})
        self.assertEqual(code, 400)
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
        req = urllib.request.Request(self.base + "/web/changes.js")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
