"""The browser selector of Settings > Wallet in a real headless Chrome or Edge, at three screen sizes in English and in Italian: what drives the browser tools (agent-browser or TaDaMoo's own), for
everything including the vault, sign-ins and payments. It shows its default, it is saved by the server, it is still there after the page is reloaded, and the file the server wrote is what the next
conversation reads: it starts with the chosen driver (the agent stack is built from the flags the choice gives). Skipped where there is no browser or no node."""
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


class BrowserSelectorsInARealBrowserTest(unittest.TestCase):
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
        tmp = tempfile.mkdtemp(prefix="bbd-data-")
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
        return url, Path(tmp) / "data"

    def test_the_selector_on_a_phone_and_on_a_desktop_in_both_languages(self):
        results, log = [], ""
        for screen in SCREENS:
            for lang in ("en", "it"):
                url, data = self.server()
                out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "browser_backend_dom_check.js"), url, self.browser, screen, lang], capture_output=True, text=True, timeout=400)
                results += json.loads(out.stdout.strip().splitlines()[-1])
                log += out.stderr[-800:]
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [], log)
        self.assertEqual(len(results), 2 * 3 * 2, results)

    def test_what_the_page_saved_is_what_the_next_conversation_starts_with(self):
        """The server writes browser.json; a conversation started from it gets the flag and builds the driver the choice names."""
        from tadamoo import vendored
        from tadamoo.browser import BrowserPrefs
        from tadamoo.headless import config_from_args, parse_args
        from tadamoo.stack import build_agent
        url, data = self.server()
        for choice in ("tadamoo", "agent-browser"):
            body = {"enabled": True, "headless": True, "backend": choice}
            req = urllib.request.Request(url + "api/wallet/browser", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
            self.assertEqual(BrowserPrefs(data / "browser.json").read()["backend"], choice)
        tmp = tempfile.mkdtemp(prefix="bbd-run-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env_data = Path(tmp) / "data"
        (env_data / "tools" / "agent-browser").mkdir(parents=True)
        (env_data / "tools" / "agent-browser" / vendored.exe_names("agent-browser")[0]).write_bytes(b"x")
        (Path(tmp) / "work").mkdir()
        old = os.environ.get("TADAMOO_DATA")
        os.environ["TADAMOO_DATA"] = str(env_data)
        self.addCleanup(lambda: os.environ.__setitem__("TADAMOO_DATA", old) if old is not None else os.environ.pop("TADAMOO_DATA", None))
        names = []
        for choice in ("tadamoo", "agent-browser"):
            argv = ["-p", "task", "--workspace", str(Path(tmp) / "work"), "--no-memory", "--browser", "--browser-cdp-url", "http://127.0.0.1:9", "--browser-backend", choice]
            names.append(type(build_agent(config_from_args(parse_args(argv)), client=object()).browser).__name__)
        self.assertEqual(names, ["Browser", "AgentBrowser"])


if __name__ == "__main__":
    unittest.main()
