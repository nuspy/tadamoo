"""The Connectors tab in a real headless Chrome or Edge on a mock server with a temporary home folder (no model, nothing installed): the skills found with their state, the one
filter, the switch on and off checked against the server, the description and the scope switch, at three screen sizes. Skipped where there is no browser or no node."""
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

WEB = Path(__file__).resolve().parent.parent / "web"
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
JS = (WEB / "connectors.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")


class ConnectorsPageStaticTest(unittest.TestCase):
    def test_the_tab_is_wired_and_every_text_exists_in_both_languages(self):
        self.assertIn('id="tab-connectors" data-view="connectors"', HTML)
        self.assertIn('id="view-connectors"', HTML)
        self.assertIn("web/connectors.js", HTML)
        self.assertIn("web/connectors.css", HTML)
        self.assertIn('"connectors"', re.search(r"const VIEWS = \[([^\]]*)\]", (WEB / "app.js").read_text(encoding="utf-8")).group(1))
        keys = {k for k in re.findall(r'T\("(cx\.[a-z_.]+?)"', JS) if not k.endswith(".")} | set(re.findall(r'data-t(?:-ph|-aria)?="(cx\.[a-z_.]+)"', HTML)) | {"tab.connectors"}
        keys |= {f"cx.state.{s}" for s in ("not_installed", "error", "needs_config", "disabled", "inactive", "active")} | {f"cx.tri.{s}" for s in ("on", "off", "partial")}
        keys |= {f"cx.sub.{k}" for k in ("skill", "mcp_server", "mcp_app", "plugin")} | {f"cx.scope.{s}" for s in ("all", "personal", "external")}
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_script_writes_no_markup_and_sends_the_key(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')
        self.assertIn("authHeaders()", JS)


class ConnectorsTabInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="cxc-data-")
        home = Path(cls.tmp) / "home"
        for name in ("alpha-skill", "beta-skill"):
            folder = home / ".claude" / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Does the {name} thing.\n---\n\nDo it.\n", encoding="utf-8")
        (Path(cls.tmp) / "data" / "connectors").mkdir(parents=True)
        (Path(cls.tmp) / "data" / "connectors" / "providers.json").write_text("[]", encoding="utf-8")              # no provider: the test does not need the network
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, "TADAMOO_DATA": cls.tmp + "/data", "TADAMOO_CACHE": cls.tmp + "/cache", "USERPROFILE": str(home), "HOME": str(home), "APPDATA": cls.tmp + "/appdata"}
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

    def test_the_tab_on_a_phone_and_on_a_desktop(self):
        self.maxDiff = None
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "connectors_dom_check.js"), self.url, self.browser, str(Path(self.tmp) / "home" / ".claude" / "skills")], capture_output=True, text=True, timeout=900)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 15, results)


if __name__ == "__main__":
    unittest.main()
