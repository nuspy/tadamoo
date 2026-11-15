"""The Required actions tab and the dialog for a new entry in a real headless Chrome or Edge on a mock server, at three screen sizes in English and in Italian: the number on the tab, the list with
its buttons (44 px on a touch screen) and the cause of a security authorization, an answer sent to its source, an entry answered elsewhere, a dialog for a new entry shown once.
Skipped where there is no browser or no node."""
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
SCREENS = ("desktop 1280x800", "phone 360x640", "phone 390x844")
PLAN = {"plan_id": "actions-check", "title": "Actions check", "target_type": "occasional", "nodes": {
    "ok": {"id": "ok", "type": "input", "title": "Approve", "assignee": "user", "dependencies": [], "outputs": ["go"],
           "spec": {"ask": "approve", "prompt": "Il testo va bene?", "prompt_en": "Is the text fine?", "content": "The text", "into": "good"}},
    "go": {"id": "go", "type": "action", "title": "Go", "assignee": "agent", "dependencies": ["ok"], "outputs": []}}}


class ActionsInARealBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidates = (os.environ.get("TADAMOO_TEST_BROWSER", ""), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      shutil.which("google-chrome") or "", shutil.which("chromium") or "")
        cls.browser = next((p for p in candidates if p and os.path.exists(p)), None)
        cls.node = shutil.which("node")
        if not cls.browser or not cls.node:
            raise unittest.SkipTest("no headless browser or no node")

    def server(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        tmp = tempfile.mkdtemp(prefix="acd-data-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {**os.environ, "TADAMOO_DATA": tmp + "/data", "TADAMOO_CACHE": tmp + "/cache"}
        proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--mock-engine", "--port", str(port)], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=20)))
        url = f"http://127.0.0.1:{port}/"
        for _ in range(80):
            try:
                urllib.request.urlopen(url, timeout=1).read(1)
                break
            except OSError:
                time.sleep(0.25)
        else:
            self.skipTest("the mock server did not start")
        return url

    @staticmethod
    def post(url, path, body):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())

    def test_the_tab_and_the_dialog_on_a_phone_and_on_a_desktop_in_both_languages(self):
        self.maxDiff = None
        results, log = [], ""
        for screen in SCREENS:
            for lang in ("en", "it"):
                if os.environ.get("ACT_ONLY") and os.environ["ACT_ONLY"] != f"{screen}:{lang}":
                    continue                                                       # a debugging aid: one combination
                url = self.server()                                              # a world of its own for each run: the number on the tab is counted
                self.post(url, "api/plans", {"plan": PLAN})
                self.post(url, "api/agent/approvals", {"run": "r1", "tool": "shell", "arguments": {"cmd": "ls"}, "effect": "reads"})
                out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "actions_dom_check.js"), url, self.browser, screen, lang], capture_output=True, text=True, timeout=400)
                results += json.loads(out.stdout.strip().splitlines()[-1])
                log += out.stderr[-800:]
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], log)
        if not os.environ.get("ACT_ONLY"):
            self.assertEqual(len(results), 5 * 3 * 2, results)


if __name__ == "__main__":
    unittest.main()
