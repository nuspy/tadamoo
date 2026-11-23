"""Models > Providers: the line "External requests through the OpenAI and Anthropic interfaces" sits right under the table of functions, before the council,
and says in both languages that only the loaded local model answers them (never the primary model or a chosen provider). Static checks of the page's own files."""
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

WEB = Path(__file__).resolve().parents[1] / "web"
KEYS = ("pv.external", "pv.external.who", "pv.external.help")


class ExternalRequestsRowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "providers.js").read_text(encoding="utf-8")
        cls.i18n = (WEB / "i18n.js").read_text(encoding="utf-8")

    def test_the_row_comes_right_after_the_functions_and_before_the_council(self):
        call = re.search(r"primarySection\(\), functionsTable\(\), externalRequests\(\), councilSection\(\)", self.js)
        self.assertTrue(call, "the page must draw functionsTable(), externalRequests(), councilSection() in that order")

    def test_the_row_uses_its_three_texts(self):
        body = re.search(r"function externalRequests\(\) \{(.*?)\n  \}", self.js, re.S).group(1)
        for key in KEYS:
            self.assertIn(f'T("{key}")', body)

    def test_every_text_exists_in_english_and_in_italian(self):
        for key in KEYS:
            hits = re.findall(rf'"{re.escape(key)}":\s*"((?:[^"\\]|\\.)*)"', self.i18n)
            self.assertEqual(len(hits), 2, f"{key} must be in both languages")
            self.assertNotEqual(hits[0], hits[1], f"{key}: the two languages are the same text")

    def test_the_texts_say_who_answers(self):
        who = re.findall(r'"pv\.external\.who":\s*"((?:[^"\\]|\\.)*)"', self.i18n)
        self.assertIn("local engine", who[0])
        self.assertIn("Primary model", who[0])
        self.assertIn("motore locale", who[1])
        self.assertIn("Modello primario", who[1])


class ExternalRequestsRowInARealBrowserTest(unittest.TestCase):
    """The row on the real Providers page, in a headless Chrome or Edge at 360x640, 390x844 and 1280x800: it is there, between the functions and the council, with its texts,
    and the page does not scroll sideways. Skipped where there is no browser or no node."""

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
        cls.tmp = tempfile.mkdtemp(prefix="prc-data-")
        env = {**os.environ, "TADAMOO_DATA": cls.tmp + "/data", "TADAMOO_CACHE": cls.tmp + "/cache"}
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

    def test_the_row_at_three_widths(self):
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "providers_row_dom_check.js"), self.url, self.browser], capture_output=True, text=True, timeout=300)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 3, results)


if __name__ == "__main__":
    unittest.main()
