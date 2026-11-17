"""Settings > Skills and the /install-skill command in a real headless Chrome or Edge on a mock server (no model, no download): the list with states and sizes, an address read
and one skill of it installed, turn off and on, remove, and the command with and without a conversation, at three screen sizes. Skipped where there is no browser or no node."""
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


class SkillsTabInARealBrowserTest(unittest.TestCase):
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
        cls.tmp = tempfile.mkdtemp(prefix="skc-data-")
        for name in ("alpha-skill", "beta-skill"):
            folder = Path(cls.tmp) / "clone" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Does the {name} thing.\n---\n\nDo it.\n", encoding="utf-8")
        # a built-in-looking skill with no models, for the /install-skill name command (every shipped one now needs models)
        gamma = Path(cls.tmp) / "extra" / "gamma-skill"
        gamma.mkdir(parents=True)
        (gamma / "SKILL.md").write_text("---\nname: gamma-skill\ndescription: Does the gamma thing.\n---\n\nDo it.\n", encoding="utf-8")
        extra_catalogue = Path(cls.tmp) / "extra_skills.json"
        extra_catalogue.write_text(json.dumps({"skills": [{"name": "gamma-skill", "path": str(gamma), "kind": "skill", "group": "test", "default": False, "requires": [],
                                                           "title": {"en": "Gamma skill", "it": "Skill gamma"},
                                                           "summary": {"en": "A skill with no models, for tests.", "it": "Una skill senza modelli, per i test."}}]}), encoding="utf-8")
        root = Path(__file__).resolve().parents[2]
        env = {**os.environ, "TADAMOO_DATA": cls.tmp + "/data", "TADAMOO_CACHE": cls.tmp + "/cache", "TADAMOO_EXTRA_SKILLS": str(extra_catalogue)}
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

    def test_the_tab_and_the_command_on_a_phone_and_on_a_desktop(self):
        self.maxDiff = None
        out = subprocess.run([self.node, str(Path(__file__).resolve().parent / "skills_dom_check.js"), self.url, self.browser, str(Path(self.tmp) / "clone")], capture_output=True, text=True, timeout=900)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 18, results)


if __name__ == "__main__":
    unittest.main()
