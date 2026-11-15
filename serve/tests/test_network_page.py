"""Settings > Network in a real headless Chrome or Edge on a server in standby with a config file, at three screen sizes in English and in Italian: the form, Save (the values reach the config file
and the page says they wait for a restart), the test, and the guided set-up. Skipped where there is no browser or no node."""
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


class NetworkPageInARealBrowserTest(unittest.TestCase):
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
        tmp = tempfile.mkdtemp(prefix="nwd-data-")
        self.addCleanup(shutil.rmtree, tmp, True)
        config = Path(tmp) / "tadamoo_config.json"
        config.write_text(json.dumps({"server": {"parallel": 2}}), encoding="utf-8")
        env = {**os.environ, "TADAMOO_DATA": tmp + "/data", "TADAMOO_CACHE": tmp + "/cache"}
        proc = subprocess.Popen([sys.executable, "-m", "serve.server", "--standby", "--config", str(config), "--port", str(port)], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        return url, config

    def test_the_network_settings_on_a_phone_and_on_a_desktop_in_both_languages(self):
        self.maxDiff = None
        results, log = [], ""
        for screen in SCREENS:
            for lang in ("en", "it"):
                if os.environ.get("NW_ONLY") and os.environ["NW_ONLY"] != f"{screen}:{lang}":
                    continue                                                       # a debugging aid: one combination
                url, config = self.server()
                out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "network_dom_check.js"), url, self.browser, screen, lang], capture_output=True, text=True, timeout=400)
                results += json.loads(out.stdout.strip().splitlines()[-1])
                log += out.stderr[-800:]
                saved = json.loads(config.read_text(encoding="utf-8"))["server"]
                if all(r["ok"] for r in results):
                    self.assertEqual((saved["host"], saved["public-name"], saved["allowed-hosts"], saved["parallel"]), ("0.0.0.0", "tadamoo.invalid", ["mypc.example.ts.net", "other.example.org"], 2))
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], log)
        if not os.environ.get("NW_ONLY"):
            self.assertEqual(len(results), 3 * 3 * 2, results)


if __name__ == "__main__":
    unittest.main()
