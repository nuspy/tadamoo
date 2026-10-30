"""The card of questions, the chips and the settings page of the questions: the logic is run by node (askcard_check.js), the wiring and the
texts are read from the files. No browser is needed."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve.tests.test_web_static import strings

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
CARD = (WEB / "askcard.js").read_text(encoding="utf-8")
CHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
INTAKE = (WEB / "intake.js").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the card logic")
class CardLogicTest(unittest.TestCase):
    def test_every_case_of_the_card_logic_passes(self):
        out = subprocess.run([NODE, str(HERE / "askcard_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 8)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class WiringTest(unittest.TestCase):
    def test_the_card_script_loads_before_the_chat_and_the_settings_have_their_place(self):
        self.assertLess(HTML.index("web/askcard.js"), HTML.index("web/agentchat.js"))
        self.assertIn('id="iq-root"', HTML)
        self.assertIn('src="web/intake.js"', HTML)
        section = HTML[HTML.index('id="iq-section"') - 80:HTML.index('id="iq-root"')]
        self.assertIn('data-sub="general"', section)

    def test_what_the_agent_wrote_enters_the_page_as_text_only(self):
        for name, js in (("askcard.js", CARD), ("intake.js", INTAKE)):
            for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval(", "outerHTML"):
                self.assertNotIn(banned, js, f"{name}: {banned}")

    def test_the_chat_draws_the_card_sends_the_answer_to_the_server_and_chips_never_send(self):
        for needle in ("AskCard.draw", "answerCard", 'case "suggestions"', "showChips", "showQuestion", 'case "intake"', "window.chatUi.setPrompt(c.prompt)"):
            self.assertIn(needle, CHAT)
        chips = CHAT[CHAT.index("function showChips"):CHAT.index("document.addEventListener(\"input\"")]
        self.assertNotIn("send(", chips)                                                    # a chip only fills the box
        self.assertIn('setAttribute("role", "group")', chips)
        self.assertIn("aria-label", chips)

    def test_the_card_is_keyboard_operable_and_names_what_is_missing(self):
        for needle in ('"radiogroup"', '"fieldset"', '"legend"', "ctrlKey", 'aria-live', "ev.detail > 0", "T(\"ch.ask.missing\""):
            self.assertIn(needle.strip('"') if needle.startswith('"') else needle, CARD)
        self.assertIn("ch.ask.recommended", CARD)                                           # the recommended tag is words, not colour only

    def test_the_buttons_are_big_enough_and_nothing_scrolls_sideways(self):
        for rule in (".aq-row", ".aq-other-text", ".asuggest .btn", ".aq-card"):
            self.assertIn(rule, CSS)
        card_css = CSS[CSS.index("/* the card of questions"):CSS.index(".acap-list")]
        self.assertIn("min-height: var(--btn-h)", card_css)
        self.assertIn("overflow-wrap: anywhere", card_css)
        self.assertIn("max-width: 720px", card_css)
        self.assertIn("(pointer: coarse)", card_css)
        self.assertNotIn("white-space: nowrap; overflow", card_css)

    def test_every_text_is_in_english_and_italian_and_the_two_differ(self):
        keys = set(re.findall(r'T\("((?:ch\.ask|ch\.suggest|ch\.intake|iq)\.[a-z_.]+)"', CARD + CHAT + INTAKE))
        keys = {k for k in keys if not k.endswith(".")}                                      # a key built from a name is listed below
        keys |= {"iq.mode." + m for m in ("off", "rules", "full")} | {"iq.mode." + m + ".d" for m in ("off", "rules", "full")} | {"iq.title"}
        self.assertGreater(len(keys), 30)
        strs = strings()
        en, it = strs["en"], strs["it"]
        for key in sorted(keys):
            self.assertIn(key, en, key)
            self.assertIn(key, it, key)
            if key != "ch.ask.n_of_m":
                self.assertNotEqual(en[key], it[key], key)
            self.assertEqual(sorted(re.findall(r"\{\d\}", en[key])), sorted(re.findall(r"\{\d\}", it[key])), key)


if __name__ == "__main__":
    unittest.main()
