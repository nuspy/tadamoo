"""The window that asks what to save of a Markdown file (only the Markdown, only the converted file, or both) in a real headless Chrome or Edge on a mock server, at three screen
sizes: the three ways, the formats this PC offers, what each choice asks of the page, the remembered choice, and a server without conversion. Skipped where there is no browser or no node."""
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


class MarkdownSaveWindowInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="mxc-data-")
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

    def test_the_window_on_a_phone_and_on_a_desktop(self):
        self.maxDiff = None
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "mdexport_dom_check.js"), self.url, self.browser], capture_output=True, text=True, timeout=600)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 15, results)


if __name__ == "__main__":
    unittest.main()
