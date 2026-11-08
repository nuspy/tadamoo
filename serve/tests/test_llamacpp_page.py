"""The llama.cpp tab of the Loader: its place in the page, its texts in both languages, and what it asks of the server (run on a stand-in DOM)."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve import llamacpp

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "llamacpp.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


class LlamaCppPage(unittest.TestCase):
    def test_the_tab_and_its_panel_are_in_the_page_and_the_other_tabs_know_them(self):
        self.assertIn('id="sub-llamacpp"', HTML)
        self.assertIn('id="ld-llamacpp"', HTML)
        self.assertLess(HTML.index("web/providers.js"), HTML.index("web/llamacpp.js"))
        self.assertIn('"llamacpp"', (WEB / "providers.js").read_text(encoding="utf-8"))
        engines = (WEB / "engines.js").read_text(encoding="utf-8")
        self.assertIn('"llamacpp", "voicesvc"]', engines)
        self.assertIn('"ld-llamacpp"', engines)

    def test_every_text_is_in_both_languages(self):
        keys = set(re.findall(r'\bT\("(lc\.[\w.]+)"', JS)) | set(re.findall(r'\btr\("(lc\.[\w.]+)"', JS)) | set(re.findall(r'data-t="((?:lc|pv\.sub\.llamacpp)[\w.]*)"', HTML))
        keys |= {"pv.sub.llamacpp"}
        keys |= {f"lc.state.{s}" for s in ("stopped", "starting", "ready", "error", "external")}
        keys |= {f"lc.src.{s}" for s in ("saved", "env", "update", "bin", "path", "usual")}
        codes = set(re.findall(r'LlamaError\(\d+, "(\w+)"', (HERE.parent / "llamacpp.py").read_text(encoding="utf-8")))
        codes = {c for c in codes if not c.endswith("_")} | {f"bad_{k}" for k in llamacpp.LIMITS} | {"offline", "spawn_failed", "exited", "update_failed"}
        keys |= {f"lc.e.{c}" for c in codes}
        keys = {k for k in keys if not k.endswith(".")}
        self.assertGreater(len(keys), 60)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_what_the_server_says_is_put_in_the_page_as_text_and_the_download_waits_for_a_button(self):
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("confirm(", JS.replace("confirm: true", ""))
        self.assertEqual(JS.count('"api/llamacpp/update/apply"'), 1)                       # one place downloads: the button under the plan
        self.assertIn('plan.token, confirm: true', JS)

    def test_the_page_asks_the_server_what_the_person_means(self):
        if NODE is None:
            self.skipTest("node is not installed")
        out = subprocess.run([NODE, str(HERE / "llamacpp_dom_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(len(results), 5)
        for r in results:
            self.assertTrue(r["ok"], f"{r['name']}: {r.get('error')}")


if __name__ == "__main__":
    unittest.main()
