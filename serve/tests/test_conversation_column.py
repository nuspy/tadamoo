"""The Chat's column of conversations and its one-row header."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "agentchat.js").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class ConversationColumnTest(unittest.TestCase):
    def test_the_drop_down_is_gone_and_the_column_is_at_the_left_of_the_chat(self):
        self.assertNotIn("asessions", HTML + JS + CSS)
        self.assertLess(HTML.index('id="convs"'), HTML.index('class="sec chatbox"'))
        self.assertRegex(HTML, r'<div class="chatrow">\s*<aside class="convs" id="convs"')
        for ident in ("cv-fav", "cv-recent", "cv-toggle", "cv-fav-group"):
            self.assertIn(f'id="{ident}"', HTML)

    def test_each_conversation_opens_goes_to_the_favourites_and_is_deleted_after_a_second_press(self):
        self.assertIn("openSession(s.id)", JS)
        self.assertIn("`/${s.id}/favorite`", JS)
        self.assertIn("`/${s.id}/delete`", JS)
        self.assertIn("if (delArmed !== s.id) {", JS)                                                # the first press only asks again
        self.assertIn("fav = shown.filter(s => s.favorite)", JS)                                     # favourites are drawn apart, above the others
        self.assertLess(HTML.index('id="cv-fav-group"'), HTML.index('id="cv-recent"'))

    def test_the_column_opens_and_closes_with_an_arrow_and_remembers_it(self):
        self.assertIn(r't.textContent = open ? "\u2039" : "\u203A";', JS)
        self.assertIn('keep(CV_KEY, open ? "1" : "0")', JS)
        self.assertRegex(CSS, r"\.convs\.shut \{ flex-basis: 40px; \}")
        self.assertIn('aria-expanded="true" aria-controls="cv-body"', HTML)

    def test_the_header_is_one_row_with_the_buttons_at_the_left_and_the_choices_at_the_right(self):
        head = HTML[HTML.index('<div class="sh chathead">'):HTML.index('<div class="acopy"')]
        order = [head.index(f'id="{i}"') for i in ("new-chat", "branches-open", "mode-chip", "chatmode", "effort", "cs-open")]
        self.assertEqual(order, sorted(order))
        self.assertLess(head.index('id="mode-chip"'), head.index('class="grow"'))
        self.assertGreater(head.index('id="chatmode"'), head.index('class="grow"'))
        self.assertNotIn("<b>Chat</b>", head)

    def test_no_help_text_sits_at_the_top_of_the_chat_and_the_kind_of_conversation_is_a_tooltip(self):
        self.assertNotIn("chathelp", HTML + JS + CSS)
        self.assertNotIn("mode-help", HTML + JS)
        self.assertNotIn("conv-help", HTML + JS)
        self.assertIn('modeSel.title = T("ch.mode." + mode + ".d");', JS)

    def test_the_model_is_not_shown_in_the_chat_but_the_model_command_still_reads_it(self):
        self.assertIn('<span id="chat-model" hidden></span>', HTML)
        self.assertNotIn("chatmodel", CSS)

    def test_the_mode_label_is_two_lines_without_a_frame_and_the_mode_is_bold_and_coloured(self):
        self.assertRegex(CSS, r"\.modechip \{[^}]*flex-direction: column[^}]*border: 0")
        self.assertRegex(CSS, r"\.modechip \.mc-mode \{ font: 700 12px")
        self.assertRegex(CSS, r"\.modechip \.mc-hint \{ font: 400 10\.5px[^}]*color: var\(--fg-3\)")
        colours = {m: re.search(r'\.modechip\[data-mode="%s"\] \.mc-mode \{ color: (#[0-9A-Fa-f]{6}|var\(--fg-2\))' % m, CSS).group(1) for m in ("read_only", "default", "plan", "auto")}
        self.assertEqual(len(set(colours.values())), 4)                                              # each mode has its own colour

    def test_the_chat_has_no_choice_of_a_copy_of_the_folder_because_it_has_no_project_folder(self):
        self.assertNotIn("acopy-ask", HTML + JS + CSS)
        self.assertNotIn("acopy-always", HTML + JS)
        self.assertNotIn('"wc.always', I18N)
        self.assertIn('const copyMode = () => "auto";', JS)

    def test_every_style_sheet_closes_what_it_opens(self):
        for sheet in WEB.glob("*.css"):
            text = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
            self.assertEqual(text.count("{"), text.count("}"), sheet.name)

    def test_the_texts_exist_in_both_languages(self):
        for key in ("cv.title", "cv.fav", "cv.recent", "cv.empty", "cv.shut", "cv.open", "cv.star", "cv.unstar", "cv.del", "cv.del.sure", "cv.deleted", "ch.chip.hint"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)


if __name__ == "__main__":
    unittest.main()
