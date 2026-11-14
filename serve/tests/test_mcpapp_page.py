"""MCP Apps in a real headless Chrome or Edge on a mock server (no model): the demo app (tools/mcp-app-demo) of a saved conversation is shown as a placeholder, opened in a
panel or a dialog, run in a sandbox of another origin and bridged to the page; at three screen sizes, in English and in Italian. Skipped where there is no browser or no node."""
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tadamoo.mcp_apps import AppCache, app_key  # noqa: E402


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


class McpAppsInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="mad-data-")
        (Path(cls.tmp) / "project").mkdir()
        env = {**os.environ, "TADAMOO_DATA": cls.tmp + "/data", "TADAMOO_CACHE": cls.tmp + "/cache"}
        cls.server = subprocess.Popen([sys.executable, "-m", "serve.server", "--mock-engine", "--port", str(port)], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    def test_the_demo_app_on_a_phone_and_on_a_desktop_in_both_languages(self):
        self.maxDiff = None
        sid = post(self.url + "api/agent/sessions", {"title": "Apps", "mode": "default"})["session"]["id"]
        server_id, uri = "mcp:app-demo", "ui://demo/survey"
        html = (ROOT / "tools" / "mcp-app-demo" / "view.html").read_text(encoding="utf-8")
        meta = {"csp": {}, "prefersBorder": True}
        digest = AppCache(Path(self.tmp) / "data" / "mcp_apps" / "templates").put(server_id, uri, html, meta, "Survey")
        where = next(Path(self.tmp).rglob("agent_sessions.json")).parent / sid                 # the conversation has no process yet: its folder is made here
        where.mkdir(parents=True, exist_ok=True)
        now = time.time()
        args = {"id": server_id, "tool": "survey", "arguments": {"title": "Lunch", "options": ["Yes", "No"]}}
        messages = [
            {"role": "user", "content": "Open the survey", "ts": now - 60},
            {"role": "assistant", "content": "", "ts": now - 50, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "call_capability", "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "call_capability", "content": "Survey 'Lunch' is open with the options Yes, No."},
        ]
        (where / "history.json").write_text(json.dumps({"session": sid, "turns": 1, "saved": now, "messages": messages, "pending": []}), encoding="utf-8")
        record = {"app": "appsaved1", "call_id": "c1", "server": server_id, "server_name": "app-demo", "key": app_key(server_id, uri), "uri": uri, "name": "Survey",
                  "tool": {"name": "survey", "title": "Survey"}, "input": {"title": "Lunch", "options": ["Yes", "No"]}, "state": "done", "template": digest, "ui": meta, "on": False,
                  "result": {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"title": "Lunch", "options": ["Yes", "No"], "series": [10, 12, 9, 15, 14, 18, 17, 20]}}}
        (where / "apps.json").write_text(json.dumps({"c1": record}), encoding="utf-8")
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "mcpapp_dom_check.js"), self.url, self.browser, str(Path(self.tmp) / "project"),
                              json.dumps({"sid": sid, "template": digest, "server": server_id, "key": app_key(server_id, uri)})], capture_output=True, text=True, timeout=1500)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], out.stderr[-3000:])
        self.assertEqual(len(results), 6 * 8, results)


if __name__ == "__main__":
    unittest.main()
