"""The server key in a real headless browser: it is kept between visits, a new visit does not ask for it again, and Forget key takes it out. Skipped where there is no browser or no node."""
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
KEY = "test-key-not-real-123"


class KeyMemoryPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidates = (os.environ.get("TADAMOO_TEST_BROWSER", ""), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      shutil.which("google-chrome") or "", shutil.which("chromium") or "")
        cls.browser = next((p for p in candidates if p and os.path.exists(p)), None)
        cls.node = shutil.which("node")
        if not cls.browser or not cls.node:
            raise unittest.SkipTest("no headless browser or no node")

    def test_the_key_is_kept_and_can_be_forgotten(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        tmp = tempfile.mkdtemp(prefix="kmp-data-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {**os.environ, "TADAMOO_DATA": tmp + "/data", "TADAMOO_CACHE": tmp + "/cache", "TADAMOO_CREDENTIALS": "file"}
        proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--standby", "--api-key", KEY, "--port", str(port)], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=20)))
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read()
                break
            except OSError:
                time.sleep(0.5)
        else:
            self.fail("the server did not start")
        res = subprocess.run([self.node, str(ROOT / "serve" / "tests" / "key_memory_check.js"), f"http://127.0.0.1:{port}/", self.browser, KEY], capture_output=True, text=True, timeout=180)
        rows = json.loads(res.stdout.strip().splitlines()[-1])
        bad = [r for r in rows if not r["ok"]]
        self.assertEqual(bad, [], res.stdout[-600:])
        self.assertGreaterEqual(len(rows), 4)


if __name__ == "__main__":
    unittest.main()
