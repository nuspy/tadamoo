"""The Chat page's description of old code: the control, the texts in both languages, the route, and where the rule is applied."""
import re
import unittest
from pathlib import Path

SERVE = Path(__file__).resolve().parent.parent
WEB = SERVE / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (SERVE / "handler.py").read_text(encoding="utf-8")


class CodePage(unittest.TestCase):
    def test_the_control_exists_is_limited_to_three_to_five_and_both_languages_have_its_texts(self):
        self.assertIn('id="cs-code-keep"', HTML)
        self.assertRegex(HTML, r'id="cs-code-keep"[^>]*min="3"[^>]*max="5"')
        for key in ("abs.codekeep", "abs.codehelp", "su.u.code_abstract", "su.u.code_abstract.d", "su.p.min_lines"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        for key in re.findall(r'data-t="(abs\.code\w+)"', HTML):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_setting_is_kept_with_the_other_context_settings_between_3_and_5_and_four_by_default(self):
        self.assertIn("const CODE_KEEP = 4;", CHAT)
        self.assertIn("Math.max(3, Math.min(5, Math.round(Number(v.codeKeep)) || CODE_KEEP))", CHAT)
        self.assertIn("codeKeep: Number($(\"#cs-code-keep\").value) || CODE_KEEP", CHAT)             # in the same object as the old answers' setting

    def test_the_rule_changes_only_what_the_model_is_sent_and_never_in_the_agent(self):
        self.assertIn('"api/chat/code-abstract"', CHAT)
        self.assertIn('path == "/api/chat/code-abstract"', HANDLER)
        ask = CHAT[CHAT.index("async function codeAbstracts"):CHAT.index("function apiMessages")]
        self.assertIn("if (agent()) return;", ask)
        self.assertIn("m.codeOf === len", ask)                                                       # asked once for a message that did not change
        self.assertNotIn("m.text =", ask)                                                            # what the person sees is not touched
        wire = CHAT[CHAT.index("function apiMessages"):CHAT.index("/* ---- the context")]
        self.assertIn("!agent() && m.codeAbstract && m.codeOf === (m.text || \"\").length && sentCount() - 1 - i >= prefs.codeKeep", wire)
        self.assertEqual(wire.count("codeSaid(m, i,"), 3)                                            # the user's text, the same with images, the reply
        self.assertIn("await codeAbstracts();", CHAT)


if __name__ == "__main__":
    unittest.main()


def test_the_chat_counts_the_messages_sent_without_the_reply_being_written_and_indexes_the_words_of_each_message():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "web" / "chat.js").read_text(encoding="utf-8")
    assert "const sentCount = " in js and "sentCount() - 1 - i >= prefs.codeKeep" in js and "i < sentCount() - keep" in js
    assert "wordsOf(m.text != null ? m.text : m.content)" in js                          # the words of a chat message are kept in `text`
