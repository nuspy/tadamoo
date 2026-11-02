"""The technical session's dialog: its controls, its texts in both languages for every use and every number, and its place in the page."""
import re
import unittest
from pathlib import Path

from tadamoo import side_uses

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "sideuses.js").read_text(encoding="utf-8")
CSS = (WEB / "sideuses.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class SideUsesPage(unittest.TestCase):
    def test_the_controls_the_script_uses_exist_in_the_page(self):
        for ident in set(re.findall(r'getElementById\("(su-[\w-]+)"\)', JS)):
            if ident.endswith("-") or ident in ("su-stats-", "su-on-", "su-prompt-"):
                continue
            self.assertIn(f'id="{ident}"', HTML, ident)

    def test_every_text_is_in_both_languages_for_every_use_and_every_number(self):
        keys = {k for k in set(re.findall(r'data-t="(su\.[\w.]+)"', HTML)) | set(re.findall(r'T\("(su\.[\w.]+)"', JS)) if not k.endswith(".")}
        for use in side_uses.USES:
            keys |= {f"su.u.{use.id}", f"su.u.{use.id}.d"} | {f"su.p.{p.key}" for p in use.params}
        self.assertGreater(len(keys), 30)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_dialog_is_greyed_without_a_session_and_everything_the_server_says_is_text(self):
        self.assertIn("state.session.available", JS)
        self.assertIn("disabled = off", JS)
        self.assertIn('dlg.dataset.off', JS)
        self.assertNotIn("innerHTML", JS)
        self.assertIn(".sud[data-off", CSS)

    def test_the_script_and_the_styles_are_loaded_and_touch_targets_are_large_enough(self):
        self.assertIn('href="web/sideuses.css"', HTML)
        self.assertIn('src="web/sideuses.js"', HTML)
        self.assertLess(HTML.index("web/intake.js"), HTML.index("web/sideuses.js"))
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("@media (max-width", CSS)

    def test_the_numbers_the_page_offers_are_the_registrys(self):
        for use in side_uses.USES:
            self.assertEqual(len(use.params), len({p.key for p in use.params}))
            for p in use.params:
                self.assertLess(p.lo, p.hi)
                self.assertTrue(p.lo <= p.default <= p.hi, (use.id, p.key))


if __name__ == "__main__":
    unittest.main()
