"""Settings > Voice/Avatar's own six sub-tabs (Common, Voice, Phone, Avatar, Meetings, Chat): the bar, which of the four sections and four
panels each one shows, and that every section stays reachable only when the outer Settings sub-tab "voice" is the one open. The page is
static here; the layout is checked in a browser by hand."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voicesubtabs.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")

KEYS = ("common", "voice", "phone", "avatar", "meetings", "chat")


class VoiceSubtabsPageTest(unittest.TestCase):
    def test_the_bar_has_one_button_per_key_with_a_translated_label(self):
        bar = HTML[HTML.index('id="vbs-tabs"'):HTML.index("</div>", HTML.index('id="vbs-tabs"'))]
        for key in KEYS:
            self.assertIn(f'data-vbs="{key}"', bar, key)
            self.assertIn(f'data-t="vbs.{key}"', bar, key)
        self.assertEqual(bar.count('role="tab"'), len(KEYS))
        self.assertIn('data-sub="voice"', HTML[HTML.index('id="vbs-tabs"') - 80:HTML.index('id="vbs-tabs"') + 40])

    def test_the_bar_and_the_four_sections_share_the_outer_voice_sub_tab(self):
        for anchor in ('id="vbs-tabs"', 'id="vb-section"', 'id="vo-section"', 'id="vph-section"', 'id="tgm-section"'):
            chunk = HTML[max(0, HTML.index(anchor) - 80):HTML.index(anchor)]
            self.assertIn('data-sub="voice"', chunk, anchor)
        self.assertLess(HTML.index('id="vbs-tabs"'), HTML.index('id="vb-section"'))

    def test_the_script_knows_every_key_and_which_sections_or_panel_it_shows(self):
        for key in KEYS:
            self.assertIn(f"{key}:", JS, key)
        self.assertIn('"vb-section"', JS)
        self.assertIn('"vo-section"', JS)
        self.assertIn('"vph-section"', JS)
        self.assertIn('"tgm-section"', JS)
        self.assertIn('"vbs-panel-common"', JS)
        self.assertIn('"vbs-panel-voice"', JS)
        self.assertIn('"vbs-panel-avatar"', JS)
        self.assertIn('"vbs-panel-meetings"', JS)

    def test_a_click_tells_the_page_and_the_outer_sub_tab_re_applies_it(self):
        self.assertIn('new CustomEvent("tadamoo:voicesub"', JS)
        self.assertIn('document.addEventListener("tadamoo:sub"', JS)
        self.assertIn("b.onclick = ()", JS)
        self.assertIn("document.body.dataset.vbsSub", JS)

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)

    def test_every_label_exists_in_english_and_italian(self):
        for key in (f"vbs.{k}" for k in KEYS):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)


if __name__ == "__main__":
    unittest.main()
