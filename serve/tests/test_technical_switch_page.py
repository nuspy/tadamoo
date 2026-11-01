"""The technical session's switch in the chat header: it is in the page, loads its state on start, and its texts exist in both languages."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "technical.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class TechnicalSwitchPage(unittest.TestCase):
    def test_the_button_and_the_script_are_in_the_page_with_the_status_line_it_uses(self):
        for ident in set(re.findall(r'getElementById\("([\w-]+)"\)', JS)):
            self.assertIn(f'id="{ident}"', HTML, ident)
        self.assertIn('src="web/technical.js"', HTML)

    def test_every_text_is_in_both_languages(self):
        keys = set(re.findall(r'\bT\(on \? "([\w.]+)"', JS)) | set(re.findall(r'\bT\("([\w.]+)"', JS)) | set(re.findall(r'T\(on \? "[\w.]+" : "([\w.]+)"', JS))
        keys |= {"tech.on", "tech.off", "tech.tip", "tech.now.on", "tech.now.off", "tech.fail", "su.state.master_off"}
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_it_reads_the_state_at_start_and_writes_only_a_boolean(self):
        self.assertIn('fetch("api/technical-session", { cache: "no-store"', JS)
        self.assertIn("JSON.stringify({ on: !on })", JS)
        self.assertNotIn("innerHTML", JS)

    def test_the_dialog_says_when_the_switch_is_off(self):
        self.assertIn("state.master === false", (WEB / "sideuses.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
