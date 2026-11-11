"""The archive picker and the consent field in the archive settings: the logic runs in node, the wiring and the words are read from the files."""
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
PICK = (WEB / "ragpick.js").read_text(encoding="utf-8")
RAG = (WEB / "rag.js").read_text(encoding="utf-8")
CSS = (WEB / "rag.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class RagPickTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "node is needed to run the picker")
    def test_the_picker_works_on_a_stand_in_dom(self):
        out = subprocess.run([NODE, str(HERE / "ragpick_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 6)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])

    def test_it_is_loaded_after_the_knowledge_page_it_takes_the_archives_from(self):
        self.assertLess(HTML.index("web/rag.js"), HTML.index("web/ragpick.js"))
        self.assertIn("window.ragUi.catalog", PICK.replace("root.ragUi", "window.ragUi"))
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, PICK)

    def test_the_rows_are_touch_sized_and_the_phone_layout_is_defined(self):
        self.assertRegex(CSS, r"\.rp-row \{[^}]*min-height: 48px")
        self.assertRegex(CSS, r"\.rp-find \{[^}]*min-height: 44px")
        self.assertIn("@media (max-width: 520px) { .rp-badges", CSS)

    def test_every_word_of_the_picker_and_of_the_consent_field_exists_in_both_languages(self):
        keys = set(re.findall(r'"(rp\.[\w.]+)"', PICK))
        self.assertIn("rp.cloud.on", keys)
        for key in sorted(keys | {"rg.f.others", "rg.f.others.help"} | {f"rg.others.{v}" for v in ("ask_dialog", "allow", "deny", "ask_voice")}):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_archive_settings_have_the_consent_select_next_to_the_cloud_tick(self):
        self.assertIn('OTHERS = ["ask_dialog", "allow", "deny", "ask_voice"]', RAG)
        self.assertIn("others_ok: others.value", RAG)
        self.assertLess(RAG.index('check, field("rg-others"'), RAG.index("const bar = el(\"div\", \"rg-bar\"), go = btn(T(\"rg.save\")"))
