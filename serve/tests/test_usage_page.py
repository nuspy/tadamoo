"""Models > Usage in a real headless Chrome or Edge at 360x640, 390x844 and 1280x800: the fixed periods, a period by date and time, the totals and the tables, with calls written
30 minutes, 5 hours and 3 days ago. Skipped where there is no browser or no node."""
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

WEB = Path(__file__).resolve().parents[1] / "web"


class UsageTabInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="usage-data-")
        data = Path(cls.tmp) / "data"
        data.mkdir()
        now = time.time()
        rows = [(now - 1800, "p1", "Cloud", "glm-x", "chat", 100, 20, 10), (now - 5 * 3600, "local", "Local engine", "qwen", "voice", 40, 10, 5), (now - 3 * 86400, "p1", "Cloud", "glm-x", "executor", 70, 5, 0)]
        with (data / "llm_usage_log.jsonl").open("w", encoding="utf-8") as f:
            for t, provider, name, model, function, prompt, completion, cached in rows:
                f.write(json.dumps({"t": t, "provider": provider, "name": name, "model": model, "function": function, "prompt": prompt, "completion": completion, "cached": cached}) + "\n")
        env = {**os.environ, "TADAMOO_DATA": str(data), "TADAMOO_CACHE": cls.tmp + "/cache"}
        cls.server = subprocess.Popen([sys.executable, "-m", "serve.server", "--mock-engine", "--port", str(port)], cwd=str(WEB.parents[1]), env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    def test_the_tab_at_three_widths(self):
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "usage_dom_check.js"), self.url, self.browser], capture_output=True, text=True, timeout=400)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 9, results)


if __name__ == "__main__":
    unittest.main()
