"""Settings > General > Where the conversations are kept: the markup the script reads, the routes it calls, the strings in both languages."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "sessionsbase.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class SessionsBasePage(unittest.TestCase):
    def test_the_section_and_the_script_are_in_the_page(self):
        self.assertIn('id="sb-root"', HTML)
        self.assertIn('data-sub="general" id="sb-section"', HTML)
        self.assertIn("web/sessionsbase.js", HTML)

    def test_the_script_calls_the_three_routes(self):
        for call in ('"api/sessions-base"', '"api/sessions-base/check"', '"api/sessions-base/move"'):
            self.assertIn(call, JS)

    def test_the_move_waits_for_a_good_check_and_the_field_is_labelled(self):
        self.assertIn("go.disabled = busy || !(checked && checked.ok)", JS)
        self.assertIn('label.htmlFor = "sb-path"', JS)
        self.assertIn('input.addEventListener("input", () => { checked = null; })', JS)

    def test_every_string_exists_in_both_languages(self):
        used = set(re.findall(r'T\("(sb\.[\w.]+)"', JS)) | set(re.findall(r'data-t="(sb\.[\w.]+)"', HTML))
        used.discard("sb.e.")                                                  # the code is added to it at run time: all of them are listed next
        used |= {f"sb.e.{c}" for c in re.search(r"const CODES = \[([^\]]*)\]", JS).group(1).replace('"', "").replace(" ", "").split(",")}
        self.assertGreaterEqual(len(used), 15)
        for key in sorted(used):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
