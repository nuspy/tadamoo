"""The flashing tabs: the decision runs in node, the wiring, the styles and the strings are read from the files."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "attention.js").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the decision")
class AttentionLogicTest(unittest.TestCase):
    def test_every_case_passes(self):
        out = subprocess.run([NODE, str(HERE / "attention_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 5)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class AttentionWiringTest(unittest.TestCase):
    def test_the_script_watches_the_two_tabs_and_asks_the_server(self):
        self.assertIn('{ cron: "tab-cron", plans: "tab-plans" }', JS)
        self.assertIn('fetch("api/attention"', JS)
        self.assertIn("if (document.hidden) return;", JS)                     # nothing is asked while the page is not looked at
        for ident in ("tab-cron", "tab-plans", "attn-live"):
            self.assertIn(f'id="{ident}"', HTML)
        self.assertLess(HTML.index("web/i18n.js"), HTML.index("web/attention.js"))

    def test_it_is_announced_and_the_blinking_stops_for_those_who_ask(self):
        self.assertRegex(HTML, r'id="attn-live" class="sr" role="status" aria-live="polite"')
        self.assertRegex(CSS, r"\.tab\.attn::after \{[^}]*border-radius: 50%")                     # the dot stays when the blinking is off
        self.assertRegex(CSS, r"@media \(prefers-reduced-motion: reduce\) \{ \.tab\.attn \{ animation: none; \} \}")

    def test_the_announcement_exists_in_both_languages(self):
        self.assertEqual(I18N.count('"attn.new":'), 2)
        self.assertIn('T("attn.new"', JS)
