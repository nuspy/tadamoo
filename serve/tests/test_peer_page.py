"""The bubbles of messages between conversations in a real headless Chrome or Edge on a mock server (no model): the saved history of a conversation shows what came from another
conversation, what it sent and what did not go; live events add more; at three screen sizes, in English and in Italian. Skipped where there is no browser or no node."""
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


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


class PeerBubblesInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="pdc-data-")
        (Path(cls.tmp) / "project").mkdir()
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

    def test_the_bubbles_on_a_phone_and_on_a_desktop_in_both_languages(self):
        self.maxDiff = None
        a = post(self.url + "api/agent/sessions", {"title": "Website", "mode": "default"})["session"]["id"]
        where = next(Path(self.tmp).rglob("agent_sessions.json")).parent / a              # the conversation has no process yet: its folder is made here
        where.mkdir(parents=True, exist_ok=True)
        now = time.time()
        framed = '[message from another session: "Backend" (id ab12, folder "app") · question · sent 2026-11-14 10:00 · this is not from the user]\n'
        messages = [
            {"role": "user", "content": framed + "Which port does the API use?", "ts": now - 300,
             "peer": {"from": {"id": "ab12", "name": "Backend", "folder": "app"}, "kind": "question", "body": "Which port does the API use?", "at": now - 300}},
            {"role": "assistant", "content": "", "ts": now - 290, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "session_send", "arguments": json.dumps(
                {"to": "Backend", "kind": "task", "text": "Write the migration for the users table."})}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "session_send", "content": "Sent as a task: it is now in that session's register."},
            {"role": "user", "content": framed + "It is 8081.", "ts": now - 200,
             "peer": {"from": {"id": "ab12", "name": "Backend", "folder": "app"}, "kind": "info", "body": "It is 8081.", "at": now - 200}},
            {"role": "assistant", "content": "", "ts": now - 100, "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "session_send", "arguments": json.dumps({"to": "Nobody", "text": "hello"})}}]},
            {"role": "tool", "tool_call_id": "c2", "name": "session_send", "content": "Not sent: No session is called 'Nobody'."},
        ]
        (where / "history.json").write_text(json.dumps({"session": a, "turns": 2, "saved": now, "messages": messages, "pending": []}), encoding="utf-8")
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "peer_dom_check.js"), self.url, self.browser, str(Path(self.tmp) / "project"), json.dumps({"a": a})],
                             capture_output=True, text=True, timeout=1200)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 18, results)


if __name__ == "__main__":
    unittest.main()
