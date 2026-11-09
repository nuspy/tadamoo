"""Chat and Code on a narrow screen: the conversations are a panel, the header controls sit behind More."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "narrow.css").read_text(encoding="utf-8")
HEAD_JS = (WEB / "chathead.js").read_text(encoding="utf-8")
CHAT_JS = (WEB / "agentchat.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HEAD = HTML[HTML.index('<div class="sh chathead">'):HTML.index('<div class="acopy"')]
PANEL = HEAD[HEAD.index('id="ch-morepanel"'):]
KEPT = ("new-chat", "branches-open", "export-open", "tgm-move", "tech-toggle", "wud-toggle", "priv-toggle", "forget-open", "pskills-open", "chg-open",
        "folder-chip", "branch-chat", "mode-chip", "learn-badge", "chat-model", "ps-chat", "po-chat", "chatmode", "coderole", "persona", "effort",
        "cs-open", "vs-group", "vs-font-down", "vs-font-up", "vs-width-down", "vs-width-up", "ps-chat-panel")


class MoreMenuTest(unittest.TestCase):
    def test_more_is_a_button_that_controls_the_panel_holding_the_other_controls(self):
        button = re.search(r'<button[^>]*id="ch-more"[^>]*>', HEAD).group(0)
        for needle in ('type="button"', 'aria-haspopup="true"', 'aria-expanded="false"', 'aria-controls="ch-morepanel"', 'data-t="ch.more"'):
            self.assertIn(needle, button)
        self.assertLess(HEAD.index('id="new-chat"'), HEAD.index('id="ch-more"'))
        self.assertLess(HEAD.index('id="ch-more"'), HEAD.index('id="ch-morepanel"'))

    def test_every_control_of_the_header_is_still_there_inside_the_panel_or_next_to_the_buttons(self):
        for ident in KEPT:
            self.assertIn(f'id="{ident}"', HEAD, ident)
        for ident in KEPT[1:]:
            self.assertIn(f'id="{ident}"', PANEL, ident)
        self.assertEqual(HEAD.count('<div class="ch-panel"'), 1)

    def test_the_status_chips_stay_in_the_row_when_the_panel_is_closed(self):
        for ident in ("folder-chip", "mode-chip", "learn-badge"):
            self.assertRegex(HEAD, rf'class="[^"]*ch-keep[^"]*" id="{ident}"')
        self.assertIn(".chathead:not(.more-open) .ch-panel > :not(.ch-keep) { display: none; }", CSS)

    def test_the_script_toggles_the_panel_and_escape_returns_focus_to_more(self):
        self.assertIn('classList.toggle("more-open", open)', HEAD_JS)
        self.assertIn('setAttribute("aria-expanded", String(open))', HEAD_JS)
        self.assertIn('e.key !== "Escape"', HEAD_JS)
        self.assertIn("more.focus()", HEAD_JS)
        self.assertIn('window.matchMedia("(max-width: 860px)")', HEAD_JS)
        self.assertIn('src="web/chathead.js"', HTML)
        self.assertLess(HTML.index("web/chatsize.js"), HTML.index("web/chathead.js"))

    def test_the_label_exists_in_both_languages(self):
        self.assertEqual(I18N.count('"ch.more":'), 2)


class ConversationPanelTest(unittest.TestCase):
    def test_the_list_is_a_panel_above_the_chat_not_a_column_beside_it(self):
        self.assertIn(".chatrow { flex-direction: column;", CSS)
        self.assertIn(".convs, .convs.shut { flex: none; }", CSS)
        self.assertIn(".convs:not(.shut) { max-height: min(50dvh, 420px); }", CSS)

    def test_the_toggle_is_a_44px_button_with_aria_expanded(self):
        self.assertIn('id="cv-toggle" aria-expanded="true" aria-controls="cv-body"', HTML)
        self.assertRegex(CSS, r"\.cv-toggle \{[^}]*width: 44px; min-height: 44px;")
        self.assertIn('.cv-toggle[aria-expanded="true"]::after', CSS)

    def test_the_list_starts_shut_is_not_remembered_and_closes_when_a_conversation_is_chosen(self):
        self.assertIn('window.matchMedia("(max-width: 860px)")', CHAT_JS)
        self.assertIn('const wanted = () => !narrow.matches && load(CV_KEY, "1") !== "0";', CHAT_JS)
        self.assertIn("if (!narrow.matches) keep(CV_KEY", CHAT_JS)
        self.assertIn("open.onclick = () => { dismiss();", CHAT_JS)
        self.assertIn('e.key !== "Escape"', CHAT_JS)
        self.assertIn('$("#cv-toggle").focus()', CHAT_JS)

    def test_the_rows_can_shrink_to_the_panel_and_are_touch_sized(self):
        self.assertIn(".convs .cv-list { grid-template-columns: minmax(0, 1fr); }", CSS)
        self.assertRegex(CSS, r"\.cv-fav, \.cv-del, \.cv-act \{ min-width: 44px; min-height: 44px; \}")
        self.assertRegex(CSS, r"\.cv-open, \.cv-projname, \.cv-addfolder \{ min-height: 44px; \}")


class MediaRuleTest(unittest.TestCase):
    def test_the_narrow_rules_are_in_one_media_query_and_wide_screens_keep_the_old_header(self):
        self.assertIn("@media (max-width: 860px) {", CSS)
        self.assertIn(".ch-panel { display: contents; }", CSS)
        self.assertIn(".ch-more { display: none; }", CSS)
        self.assertLess(CSS.index(".ch-more { display: none; }"), CSS.index("@media"))

    def test_the_open_panel_scrolls_inside_itself_and_shows_the_labels_of_the_selects(self):
        self.assertRegex(CSS, r"\.chathead\.more-open \.ch-panel \{[^}]*max-height: min\(60dvh, 440px\); overflow-y: auto;")
        self.assertIn(".chathead.more-open .ch-panel .effort > span { position: static; display: inline;", CSS)
        self.assertIn("min-height: 44px; font-size: 16px; }", CSS)

    def test_the_composer_and_header_buttons_are_44px_and_the_stylesheet_loads_last(self):
        self.assertRegex(CSS, r"\.composer \.btn, \.composer \.btn\.attach[^{]*\{ min-height: 44px; min-width: 44px; \}")
        links = re.findall(r'<link rel="stylesheet" href="web/([^"]+)">', HTML)
        self.assertGreater(links.index("narrow.css"), links.index("sideuses.css"))

    def test_no_emoji_or_horizontal_scroll_rule_in_the_stylesheet(self):
        self.assertNotRegex(CSS, r"[\U0001F300-\U0001FAFF☀-➿]")
        self.assertNotIn("overflow-x: scroll", CSS)


if __name__ == "__main__":
    unittest.main()
