"""The Tasks tab in a real headless Chrome or Edge on a mock server (no model): the open tasks of two projects as a tree, filters, add, edit, subtask, move, delete, a plan as one line,
and Resume in chat, at three screen sizes. Skipped where there is no browser or no node."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


class TasksTabInARealBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = None
        cls.tmp = ""
        candidates = (os.environ.get("TADAMOO_TEST_BROWSER", ""), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      shutil.which("google-chrome") or "", shutil.which("chromium") or "")
        cls.browser = next((p for p in candidates if p and os.path.exists(p)), None)
        cls.node = shutil.which("node")
        if not cls.browser or not cls.node:
            raise unittest.SkipTest("no headless browser or no node")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cls.tmp = tempfile.mkdtemp(prefix="tkc-data-")
        for name in ("site", "app"):
            (Path(cls.tmp) / name).mkdir()
        plans = Path(cls.tmp) / "data" / "planner" / "plans"
        plans.mkdir(parents=True)
        nodes = {"start": {"type": "start", "status": "completed"}, "a": {"type": "action", "status": "completed", "assignee": "agent", "title": "Research"},
                 "b": {"type": "action", "status": "in_progress", "assignee": "agent", "title": "Write the script"}, "c": {"type": "action", "status": "in_progress", "assignee": "user", "title": "Approve"}}
        (plans / "briefing.json").write_text(json.dumps({"plan": {"plan_id": "briefing", "title": "Briefing", "nodes": nodes}}), encoding="utf-8")
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, "TADAMOO_DATA": cls.tmp + "/data", "TADAMOO_CACHE": cls.tmp + "/cache"}
        cls.server = subprocess.Popen([sys.executable, "-m", "serve.server", "--mock-engine", "--port", str(port)], cwd=str(root), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{port}/"
        for _ in range(80):
            try:
                urllib.request.urlopen(cls.url, timeout=1).read(1)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise unittest.SkipTest("the mock server did not start")

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.terminate()
            cls.server.wait(timeout=20)
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_tab_on_a_phone_and_on_a_desktop(self):
        self.maxDiff = None
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "tasks_dom_check.js"), self.url, self.browser, str(Path(self.tmp) / "site"), str(Path(self.tmp) / "app")],
                             capture_output=True, text=True, timeout=900)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 12, results)


if __name__ == "__main__":
    unittest.main()
