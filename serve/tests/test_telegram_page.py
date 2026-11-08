"""Settings > Voice > Telegram and the Move to Telegram button: static checks of the page, its script, its style and its words."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "telegram.js").read_text(encoding="utf-8")
CSS = (WEB / "telegram.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
CHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2705\u274C]")


class TelegramPageTest(unittest.TestCase):
    def test_the_section_is_a_part_of_settings_voice_with_its_script_and_style(self):
        self.assertRegex(HTML, r'<section class="sec stvoice" data-sub="voice" id="tgm-section" aria-labelledby="tgm-h"')
        self.assertIn('id="tgm-root"', HTML)
        self.assertIn('<script defer src="web/telegram.js"></script>', HTML)
        self.assertIn('href="web/telegram.css"', HTML)
        self.assertIn('settingsAt("tgm-h", "voice")', (WEB / "shell.js").read_text(encoding="utf-8"))

    def test_the_button_is_in_the_header_of_chat_and_code_hidden_until_it_can_work(self):
        self.assertRegex(HTML, r'<button type="button" class="btn" id="tgm-move"[^>]*data-t="tgm.move" hidden>')
        self.assertLess(HTML.index('id="export-open"'), HTML.index('id="tgm-move"'))
        self.assertLess(HTML.index('id="tgm-move"'), HTML.index('id="tech-toggle"'))
        self.assertIn("moveBtn.hidden = !((view === \"chat\" || view === \"agent\") && sid && rec && (moved || can))", JS)    # offered only with a bot that runs
        self.assertIn("rec.telegram_chat", JS)

    def test_every_route_it_calls_exists_on_the_server(self):
        for route in ('"api/telegram"', '"api/telegram/token"', '"api/telegram/test"', '"api/telegram/settings"', '"api/telegram/allow"', '"api/telegram/pairing"',
                      '"api/telegram/send-test"', '"api/telegram/import"', "/telegram`"):
            self.assertIn(route, JS)
        for route in ('"/api/telegram"', '"/api/telegram/"', '"settings"', '"token"', '"allow"', '"pairing"', '"import"', '"test"', '"send-test"', 'parts[2] == "telegram"'):
            self.assertIn(route, HANDLER)
        self.assertIn("authHeaders()", JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)

    def test_the_token_field_is_write_only(self):
        self.assertIn('input.type = "password"', JS)
        self.assertIn('input.autocomplete = "off"', JS)
        self.assertIn('input.value = "";', JS)                                              # cleared as soon as it is sent
        self.assertNotRegex(JS, r"S\.token\b|\.token\s*[,)]\s*;?\s*$")                      # the page is told only whether one is set
        self.assertIn("S.configured", JS)

    def test_a_pairing_row_offers_authorise_and_dismiss_with_the_id_in_the_label(self):
        self.assertIn('{ authorise: p.id }', JS)
        self.assertIn('{ dismiss: p.id }', JS)
        self.assertIn('setAttribute("aria-label", `${T("tgm.authorise")} ${p.id}`)', JS)

    def test_the_permission_level_is_the_chats_own_four_levels(self):
        self.assertIn('T("ag.mode." + m)', JS)
        for mode in ("plan", "read_only", "default", "auto"):
            self.assertIn(f'"ag.mode.{mode}"', I18N)

    def test_every_word_is_in_both_languages_and_every_key_used_exists(self):
        en_start, it_start = I18N.index("  en: {"), I18N.index("  it: {")
        en, it = I18N[en_start:it_start], I18N[it_start:]
        used = set(re.findall(r'T\("(tgm\.[a-z_.]+?)"', JS)) | set(re.findall(r'T\("(tgm\.[a-z_.]+?)"', CHAT)) | set(re.findall(r'T\(e\.chat \? "(tgm\.[a-z.]+)" : "(tgm\.[a-z.]+)"', CHAT)[0])
        used |= {f"tgm.state.{s}" for s in ("off", "no_token", "starting", "running", "conflict", "bad_token", "error")}
        used |= set(re.findall(r'data-t="(tgm\.[a-z.]+)"', HTML))
        used.discard("tgm.state.")                                                         # built at run time, listed above
        for key in used | {"pal.set.telegram"}:
            self.assertEqual(en.count(f'"{key}":'), 1, f"{key} in English")
            self.assertEqual(it.count(f'"{key}":'), 1, f"{key} in Italian")
        keys_en = set(re.findall(r'"(tgm\.[a-z_.]+)":', en))
        keys_it = set(re.findall(r'"(tgm\.[a-z_.]+)":', it))
        self.assertEqual(keys_en, keys_it)
        self.assertIn("Hermes", en[en.index('"tgm.one"'):en.index('"tgm.one"') + 400])

    def test_touch_targets_are_44_pixels_and_nothing_scrolls_sideways(self):
        self.assertRegex(CSS, r"\.tgm-root \.btn, \.tgm-pop \.btn \{[^}]*min-height: 44px; min-width: 44px")
        self.assertRegex(CSS, r"\.tgm-field input, \.tgm-field select \{[^}]*min-height: 44px")
        self.assertRegex(CSS, r"\.tgm-switch \{[^}]*min-height: 44px")
        self.assertIn("max-width: calc(100vw - 32px)", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertNotRegex(CSS, r"overflow-x:\s*scroll")

    def test_the_input_font_is_16_pixels_so_a_phone_does_not_zoom(self):
        self.assertRegex(CSS, r"\.tgm-field input, \.tgm-field select \{[^}]*font: 16px")

    def test_no_emoji_stands_for_an_icon(self):
        for text in (JS, CSS):
            self.assertIsNone(EMOJI.search(text))
        added = "".join(re.findall(r'"tgm\.[a-z_.]+": "[^"]*"', I18N))
        self.assertIsNone(EMOJI.search(added))

    def test_the_status_is_not_told_by_colour_alone(self):
        self.assertIn("T(\"tgm.state.\" + S.state)", JS)                                       # the word comes with the dot
        self.assertIn('dot.setAttribute("aria-hidden", "true")', JS)

    def test_a_conversation_moved_to_telegram_shows_a_badge_in_the_list_and_a_note_when_it_moves(self):
        self.assertIn('if (s.telegram_chat) open.append(mk("span", "cv-tgm", T("tgm.badge")))', CHAT)
        self.assertIn('case "telegram":', CHAT)
        self.assertIn(".cv-tgm", CSS)

    def test_focus_and_escape_work_without_a_pointer(self):
        self.assertIn("focus-visible", CSS)
        self.assertIn('e.key === "Escape"', JS)
        self.assertIn('pop.setAttribute("role", "dialog")', JS)


if __name__ == "__main__":
    unittest.main()
