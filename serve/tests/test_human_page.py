"""The window of a plan's request to the person, in a real headless Chrome or Edge on a mock server: it opens over a tab that is not Plans, at three screen sizes, takes
an approval and a value through the real buttons with a confirmation, and Back leaves a request for later. Skipped where there is no browser or no node."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
CSS = (WEB / "human.css").read_text(encoding="utf-8")


@unittest.skipUnless(shutil.which("node"), "node is needed to run the decisions")
class HumanLogicTest(unittest.TestCase):
    def test_every_case_passes(self):
        out = subprocess.run([shutil.which("node"), str(HERE / "human_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 8)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class HumanWiringTest(unittest.TestCase):
    def test_the_page_loads_the_window_and_every_string_is_in_both_languages(self):
        self.assertIn('<script defer src="web/human.js"></script>', HTML)
        self.assertIn('<link rel="stylesheet" href="web/human.css">', HTML)
        keys = set(re.findall(r'T\("(hr\.[a-z.]+)"', (WEB / "human.js").read_text(encoding="utf-8"))) | {"hr.need", "hr.need.number", "hr.need.choice", "hr.need.yesno"}
        en, it = I18N.split("\n  it: {", 1)
        for key in keys:
            self.assertIn(f'"{key}"', en, key)
            self.assertIn(f'"{key}"', it, key)

    def test_the_targets_are_big_and_the_window_follows_the_safe_area(self):
        self.assertIn("min-height: 48px", CSS)
        self.assertIn("env(safe-area-inset-bottom)", CSS)
        self.assertIn("prefers-reduced-motion", CSS)


class HumanRequestInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="hrc-data-")
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

    def test_the_window_over_another_tab_on_a_phone_and_on_a_desktop(self):
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "human_dom_check.js"), self.url, self.browser], capture_output=True, text=True, timeout=600)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(len(results), 22, results)
        self.assertEqual([r for r in results if not r["ok"]], [])


if __name__ == "__main__":
    unittest.main()
