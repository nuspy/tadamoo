"""The grammar check in a real headless Chrome or Edge on a mock-engine server, at three screen sizes: the list in Settings > Inference, the button under a real answer
in the Chat, the offer after a translation request and none after a plain one, what the page sends, the corrected text, errors and Italian. Skipped where there is no
browser or no node."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


class LanguageCheckInARealBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = None
        cls.tmp = ""
        candidates = (os.environ.get("TADAMOO_TEST_BROWSER", ""), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      shutil.which("google-chrome") or "", shutil.which("chromium") or "")
        cls.browser = next((p for p in candidates if p and os.path.exists(p)), None)
        cls.node = shutil.which("node")
        if not cls.browser or not cls.node:
            raise unittest.SkipTest("no headless browser or no node")
        cls.tmp = tempfile.mkdtemp(prefix="lcc-data-")
        os.environ["TADAMOO_DATA"] = cls.tmp + "/data"
        os.environ["TADAMOO_CACHE"] = cls.tmp + "/cache"
        root = Path(__file__).resolve().parents[2]
        tok = ByteTokenizer()
        answers = ["</think>\n\nThe sea is wide and quiet.", "</think>\n\nJo reggelt kivanok."]
        svc = Service(MockEngine(tok, answers * 30, max_context=1000000), tok, ChatTemplate(root / "serve" / "chat_template.jinja"))
        svc.telemetry = _NoTelemetry()
        cls.httpd = serve(svc, port=0)
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/"
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
        if cls.httpd:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_check_on_a_phone_and_on_a_desktop(self):
        self.maxDiff = None
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "langcheck_dom_check.js"), self.url, self.browser], capture_output=True, text=True, timeout=600)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 13, results)


if __name__ == "__main__":
    unittest.main()
