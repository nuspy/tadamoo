"""The Dark web section of the inference settings: the page runs on a stand-in DOM in node, the strings and the wiring are read from the files."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
JS = (WEB / "darkwebset.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class DarkwebPageTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "node is needed to run the page on the stand-in DOM")
    def test_the_section_works_on_a_stand_in_dom(self):
        out = subprocess.run([NODE, str(HERE / "darkweb_dom_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertEqual(len(results), 4)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])

    def test_every_string_exists_in_both_languages(self):
        keys = set(re.findall(r'T\("(dw\.[a-z.]+)"', JS)) | {f"dw.tor.{a}.done" for a in ("install", "start", "stop")}
        keys = {k for k in keys if not k.endswith(".")}                 # the three done messages are built from the action
        self.assertGreater(len(keys), 18)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_it_sits_in_the_inference_settings_and_the_server_text_enters_as_text(self):
        self.assertIn('id="dw-root"', HTML)
        self.assertLess(HTML.index('id="cs-show_tooling"'), HTML.index('id="dw-root"'))
        self.assertIn("web/darkwebset.js", HTML)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)
