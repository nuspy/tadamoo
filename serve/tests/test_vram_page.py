"""The Settings line that shows the VRAM lent to other functions: on the page, in both languages, read only."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "serve" / "web"
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "vrambroker.js").read_text(encoding="utf-8")


class PageTests(unittest.TestCase):
    def test_the_words_are_in_both_languages_with_the_same_placeholders(self):
        en, it = I18N.split("\n  it: {")
        keys = sorted(set(re.findall(r'"(vb\.[\w.]+)":', I18N)))
        self.assertEqual(keys, ["vb.e.offline", "vb.gb", "vb.idle", "vb.lent", "vb.off", "vb.title", "vb.unknown"])
        for key in keys:
            e = re.search(rf'"{re.escape(key)}": "((?:[^"\]|\.)*)"', en)
            i = re.search(rf'"{re.escape(key)}": "((?:[^"\]|\.)*)"', it)
            self.assertTrue(e and i, key)
            self.assertEqual(sorted(re.findall(r"\{\d\}", e.group(1))), sorted(re.findall(r"\{\d\}", i.group(1))), key)
        used = set(re.findall(r'T\("(vb\.[\w.]+)"', JS)) | set(re.findall(r'data-t="(vb\.[\w.]+)"', HTML))
        self.assertEqual(sorted(used), keys)

    def test_the_section_and_the_script_are_on_the_page_and_the_line_only_reads(self):
        self.assertIn('id="vb-root"', HTML)
        self.assertIn('<script defer src="web/vrambroker.js"></script>', HTML)
        self.assertIn('"api/vram/leases"', JS)
        self.assertNotIn('"POST"', JS)
        self.assertNotIn("innerHTML", JS)


if __name__ == "__main__":
    unittest.main()
