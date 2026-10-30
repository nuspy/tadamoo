"""The Long answer control and the cut-reply Continue button: the markup the scripts rely on, the strings, the settings key."""
import re
import unittest
from pathlib import Path

from serve import profiles

WEB = Path(__file__).resolve().parents[1] / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
LONG_JS = (WEB / "longform.js").read_text(encoding="utf-8")
CHAT_JS = (WEB / "chat.js").read_text(encoding="utf-8")
AGENT_JS = (WEB / "agentchat.js").read_text(encoding="utf-8")
CSS = (WEB / "longform.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class LongAnswerPage(unittest.TestCase):
    def test_every_element_the_script_reads_is_in_the_page(self):
        for ident in sorted(set(re.findall(r'\$\("#([\w-]+)"\)', LONG_JS))):
            self.assertIn(f'id="{ident}"', HTML, ident)

    def test_the_button_and_dialog_are_explained_and_labelled(self):
        button = re.search(r'<button[^>]*id="long-open"[^>]*>', HTML).group(0)
        self.assertIn('data-t-title="la.open.tip"', button)
        self.assertIn('aria-haspopup="dialog"', button)
        dialog = re.search(r'<dialog[^>]*id="long-dlg"[^>]*>', HTML).group(0)
        self.assertIn('aria-labelledby="la-title"', dialog)
        for field in ("la-mode", "la-words", "la-lang", "la-instr", "la-style", "la-gloss", "la-protect"):
            self.assertRegex(HTML, rf'<label[^>]*for="{field}"')
            self.assertRegex(HTML, rf'id="{field}"[^>]*aria-describedby="{field}-h"')

    def test_every_string_exists_in_both_languages(self):
        used = set(re.findall(r'T\("(la\.[\w.]+)"', LONG_JS + CHAT_JS + AGENT_JS)) | set(re.findall(r'data-t(?:-title|-ph)?="(la\.[\w.]+)"', HTML))
        used |= {k for k in re.findall(r'T\("(chat\.(?:continue|length|repeated)|ch\.stop\.length(?:\.h)?|ch\.continue\.cut\.text|cs\.max_answer_tokens(?:\.help)?)"', CHAT_JS + AGENT_JS + LONG_JS)}
        used |= {"cs.max_answer_tokens", "cs.max_answer_tokens.help", "chat.continue", "chat.length", "chat.repeated", "ch.stop.length", "ch.stop.length.h", "ch.continue.cut.text"}
        english, italian = I18N.split("\n  it: {", 1) if "\n  it: {" in I18N else (I18N, "")
        for key in sorted(used):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_buttons_are_at_least_30_px_and_nothing_scrolls_sideways(self):
        self.assertNotRegex(CSS, r"min-width:\s*\d{4,}px")
        self.assertIn("min(640px, 96vw)", CSS)
        self.assertIn("min-height: 36px", CSS)

    def test_a_cut_reply_offers_continue_and_a_loop_does_not(self):
        self.assertIn('class="btn ghost goon"', CHAT_JS)
        self.assertRegex(CHAT_JS, r"lengthCut = m => .*m\.finish === \"length\".*!m\.repeated.*!m\.long")
        self.assertIn("if (cont) body.messages.push", CHAT_JS)
        self.assertIn('"length"].includes(kind)', AGENT_JS)

    def test_the_answer_cap_is_a_setting_a_preset_keeps(self):
        self.assertIn("max_answer_tokens", profiles.INFERENCE_KEYS)
        self.assertIn('key: "max_answer_tokens"', (WEB / "chatset.js").read_text(encoding="utf-8"))
        self.assertIn('id="cs-max_answer_tokens"', HTML)

    def test_a_long_answer_is_not_compressed_by_billion_context(self):
        self.assertIn('...(long ? {} : { "X-Basalt-Compress": "1" })', CHAT_JS)


if __name__ == "__main__":
    unittest.main()
