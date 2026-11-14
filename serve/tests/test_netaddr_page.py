"""Addresses for a person on another device, in a real headless Chrome or Edge on a mock server: the page's own Markdown turns an address of the server's machine in an answer into the
address the person reaches it at, a picture written as such an address becomes a reference the page loads with its key, and a page at this machine changes nothing; at three screen
sizes. Skipped where there is no browser or no node."""
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


class NetAddrInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="nad-data-")
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

    def test_answers_for_a_person_elsewhere_and_for_a_person_at_the_machine(self):
        self.maxDiff = None
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "netaddr_dom_check.js"), self.url, self.browser], capture_output=True, text=True, timeout=600)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], out.stderr[-3000:])
        self.assertEqual(len(results), 4 * 3, results)

    def test_the_page_loads_the_script_before_the_markdown_that_uses_it(self):
        html = (ROOT / "serve" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("web/netaddr.js"), html.index("web/chat.js"))
        self.assertIn("window.NetAddr.fix(s)", (ROOT / "serve" / "web" / "chat.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
