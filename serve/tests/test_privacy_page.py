"""The chat header's Private and Forget buttons, and the export button of Settings > Memories: what the pages call and that the texts exist in both languages."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "privacy.js").read_text(encoding="utf-8")
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
AGENT = (WEB / "agentchat.js").read_text(encoding="utf-8")
MEMORIES = (WEB / "memories.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class PrivacyPageTest(unittest.TestCase):
    def test_the_buttons_are_in_the_chat_header_and_the_script_is_loaded_after_the_chat(self):
        self.assertRegex(HTML, r'<button type="button" class="btn" id="priv-toggle" aria-pressed="false">')
        self.assertIn('id="forget-open"', HTML)
        self.assertLess(HTML.index("web/agentchat.js"), HTML.index("web/privacy.js"))

    def test_a_conversation_is_marked_and_forgotten_through_its_routes(self):
        self.assertIn("/private", JS)
        self.assertIn("{ private: want }", JS)
        self.assertIn("/forget", JS)
        self.assertIn("/api/past/forget", CHAT)                                          # the model-only chat has its own route
        self.assertIn("...(window.privacy && window.privacy.pending() ? { private: true } : {})", AGENT)    # a mark made before the first message goes with the creation

    def test_forgetting_asks_twice_and_marking_shows_its_state_without_colour_alone(self):
        self.assertIn("if (!armed) {", JS)
        self.assertIn('priv.setAttribute("aria-pressed"', JS)
        self.assertIn('T(on ? "priv.on" : "priv.off")', JS)

    def test_the_model_only_chat_sends_nothing_to_the_index_while_private(self):
        body = CHAT[CHAT.index("function indexSoon()"):CHAT.index("function indexSoon()") + 200]
        self.assertIn("if (isPrivate()) return;", body)
        self.assertIn('keep(PRIV_KEY, "0")', CHAT)                                       # a new conversation starts not private

    def test_the_memories_page_has_an_export_button_that_names_the_folder(self):
        self.assertIn("api/memories/export", MEMORIES)
        self.assertIn("exportBlock()", MEMORIES)
        self.assertIn('T("me.export.done", exported.notes, exported.folder)', MEMORIES)

    def test_no_markup_is_written_and_the_texts_exist_in_both_languages(self):
        for source in (JS, MEMORIES):
            for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
                self.assertNotIn(banned, source)
        keys = set(re.findall(r'"(priv\.[a-z.]+)"', JS)) | {"me.export.title", "me.export.help", "me.export.go", "me.export.done"}
        self.assertGreaterEqual(len(keys), 9)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)


if __name__ == "__main__":
    unittest.main()
