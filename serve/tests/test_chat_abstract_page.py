"""The Chat page's short record of old long answers: the controls, the texts in both languages and the places the rule is applied."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (Path(__file__).resolve().parent.parent / "handler.py").read_text(encoding="utf-8")


class AbstractPage(unittest.TestCase):
    def test_the_controls_exist_and_every_text_is_in_both_languages(self):
        for ident in re.findall(r'\$\("#(cs-abs[\w-]*)"\)', CHAT):
            self.assertIn(f'id="{ident}"', HTML)
        for key in re.findall(r'data-t="(abs\.[\w.]+)"', HTML):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_the_rule_is_applied_to_the_model_wire_only_and_the_server_writes_the_abstract(self):
        self.assertIn('"api/chat/abstract"', CHAT)
        self.assertIn('path == "/api/chat/abstract"', HANDLER)
        self.assertIn("usersAfter[i] >= prefs.keep", CHAT)                      # the newest turns go whole
        self.assertIn("m.abstractOf === (m.text || \"\").length", CHAT)         # an edited or continued reply is sent as it is now
        self.assertIn("agent()", CHAT[CHAT.index("async function abstractLater"):CHAT.index("function apiMessages")])   # not in the Agent
        self.assertNotIn("m.abstract", CHAT[CHAT.index("async function send"):CHAT.index("async function send") + 200])


if __name__ == "__main__":
    unittest.main()
