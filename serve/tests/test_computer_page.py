"""Settings > Wallet > Computer control in a real headless Chrome or Edge, at three screen sizes in English and in Italian: the section, its size, and that turning it on asks first.
Skipped where there is no browser or no node. The server runs in standby on this system's own backend (it only lists the screens; nothing is clicked or captured)."""
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


class ComputerSectionInARealBrowserTest(unittest.TestCase):
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
        tmp = tempfile.mkdtemp(prefix="ccd-data-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {**os.environ, "TADAMOO_DATA": tmp + "/data", "TADAMOO_CACHE": tmp + "/cache"}
        proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--standby", "--port", str(port)], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=20)))
        url = f"http://127.0.0.1:{port}/"
        for _ in range(100):
            try:
                urllib.request.urlopen(url, timeout=1).read(1)
                break
            except OSError:
                time.sleep(0.25)
        else:
            self.skipTest("the server did not start")
        return url

    def test_the_section_on_a_phone_and_on_a_desktop_in_both_languages(self):
        results, log = [], ""
        for screen in SCREENS:
            for lang in ("en", "it"):
                out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "computer_dom_check.js"), self.server(), self.browser, screen, lang], capture_output=True, text=True, timeout=400)
                results += json.loads(out.stdout.strip().splitlines()[-1])
                log += out.stderr[-800:]
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], log)
        self.assertEqual(len(results), 2 * 3 * 2, results)


if __name__ == "__main__":
    unittest.main()
