"""The Settings section and the question for heavy jobs: on the page, in both languages with the same placeholders, text only."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "serve" / "web"
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "heavyjobs.js").read_text(encoding="utf-8")
DYNAMIC = [f"hv.mode.{m}{tail}" for m in ("ask", "allow", "never") for tail in ("", ".help")] + ["hv.engine.no_local_engine"]


class PageTests(unittest.TestCase):
    def test_every_word_is_in_both_languages_with_the_same_placeholders_and_none_is_unused(self):
        en, it = I18N.split("\n  it: {")
        keys = sorted(set(re.findall(r'"(hv\.[\w.]+)":', I18N)))
        for key in keys:
            e = re.search(rf'"{re.escape(key)}": "((?:[^"\\]|\\.)*)"', en)
            i = re.search(rf'"{re.escape(key)}": "((?:[^"\\]|\\.)*)"', it)
            self.assertTrue(e and i, key)
            self.assertEqual(sorted(re.findall(r"\{\d\}", e.group(1))), sorted(re.findall(r"\{\d\}", i.group(1))), key)
        used = {k for k in re.findall(r'T\("(hv\.[\w.]+)"', JS) if not k.endswith(".")} | set(re.findall(r'data-t="(hv\.[\w.]+)"', HTML)) | set(DYNAMIC)
        self.assertEqual(sorted(used), keys)

    def test_the_section_the_script_and_the_style_are_on_the_page(self):
        self.assertIn('id="hv-root"', HTML)
        self.assertIn('<script defer src="web/heavyjobs.js"></script>', HTML)
        self.assertIn('<link rel="stylesheet" href="web/heavyjobs.css">', HTML)

    def test_the_page_only_puts_text_in_the_page_and_a_question_is_answered_by_a_button(self):
        self.assertNotIn("innerHTML", JS)
        self.assertIn('"api/heavy/answer"', JS)
        self.assertIn('addEventListener("cancel"', JS)


if __name__ == "__main__":
    unittest.main()
