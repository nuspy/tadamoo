"""The Chat's slash commands and the mode label: slash.js run against a small stand-in for the page (slash_check.js), and the page's wiring."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is needed to run the page's script")
class SlashCommandsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run([NODE, str(HERE / "slash_check.js")], capture_output=True, text=True, timeout=60)
        if out.returncode:
            raise RuntimeError(out.stderr)
        cls.r = json.loads(out.stdout)

    def test_only_a_slash_at_the_start_with_a_word_is_a_command(self):
        p = self.r["parse"]
        self.assertEqual(p[0], {"word": "effort", "rest": "low"})
        self.assertIsNone(p[2])
        self.assertEqual(p[4], {"word": "x", "rest": "a  b"})

    def test_the_commands_that_act_on_the_page(self):
        r = self.r
        self.assertEqual(r["clear"], [True, True, 2])                                  # /clear and its alias press the new-conversation button
        self.assertEqual(r["effort"][:5], [True, "low", True, "", True])               # high is the model's own default, the list's first entry
        self.assertIn("Thinking is High (default)", r["effort"][5][0])                 # no or an unknown level: it says what it is
        self.assertEqual(r["mode"][3], ["default", "auto"])                            # ask -> default; automatic asks for the confirmations in perm.set
        self.assertEqual(r["plan"][1], ["plan"])
        self.assertEqual(r["planSent"], ["fix the build"])
        self.assertEqual(r["stopped"], 1)
        self.assertEqual(r["hash"], "plans")
        self.assertEqual(r["settingsOpened"], 1)

    def test_what_needs_the_process_is_handed_to_it(self):
        self.assertEqual(self.r["server"][-1], [["status", ""], ["context", ""], ["compact", ""], ["tools", ""], ["hooks", ""], ["mcp", ""], ["memory", "zebra"], ["permissions", ""]])

    def test_a_command_that_does_not_exist_stays_in_the_box_with_a_note(self):
        self.assertEqual(self.r["unknown"][0], "keep")
        self.assertIn("There is no command /nothing", self.r["unknown"][1][-1])

    def test_the_requests_are_sent_to_the_model_in_words_and_research_needs_a_question(self):
        sent = self.r["sent"]
        self.assertEqual(len(sent), 4)
        self.assertTrue(sent[0].startswith("Review the changes in this folder (git diff") and sent[0].endswith("only src"))
        self.assertTrue(sent[1].startswith("Look through this folder and write AGENTS.md"))
        self.assertTrue(sent[2].endswith("Question: why is the sky blue") and sent[3].endswith("Question: tides"))      # /search is /research
        self.assertEqual(self.r["ask"][2], "keep")
        self.assertEqual(self.r["notesAfterAsk"], ["Write the question: /research question."])

    def test_copy_takes_the_nth_answer_from_the_end(self):
        self.assertEqual(self.r["copied"], "first answer")

    def test_skills_found_on_the_pc_are_commands_and_run_without_any_approval(self):
        r = self.r
        self.assertEqual(r["skillMenu"], [["deploy", False], ["draft", False]])         # a skill that is off is not offered
        self.assertEqual(r["skillRun"][2], ["Load the skill skill:deploy with load_capability and follow it. What I need: to staging",
                                            "Load the skill skill:draft with load_capability and follow it. Ask me what I need if the skill requires input."])
        self.assertIn("deploy", r["candidates"])

    def test_the_menu_puts_the_names_that_start_with_the_letters_first(self):
        rev, sec, none = self.r["rank"]
        self.assertEqual(rev[:2], ["review", "security-review"])
        self.assertEqual(sec, ["security-review"])
        self.assertEqual(none, 0)

    def test_what_the_process_answers_is_drawn_as_text_in_the_language_of_the_page(self):
        r = self.r
        self.assertEqual(r["status"], ["StatusMode: Plan onlyThinking: LowTurns: 3Steps per answer: No limitEnd-of-turn bias: 0Context: 1200 of 32768 tokens (4%)Messages: 7Tools: 30Memories: 2MCP servers: 1Tokens used by the model: 9000"])
        self.assertEqual(r["compact"], [["The conversation was shortened: 9000 → 3000 tokens."], ["Wait until the work in hand is done."], ["There is nothing to shorten yet (55 tokens)."]])
        self.assertEqual(r["mcp"][1], ["MCP serversa · 3 toolsb · did not start: no start"])
        self.assertEqual(r["memory"][1], ["Memory (1)project · uses python"])
        self.assertIn("allow shell_command:pytest *", r["permissions"][0])
        self.assertEqual(r["failed"], ["The command did not work: boom"])
        self.assertIn("/clear /new /reset", r["help"])
        self.assertIn("/research /search <question>", r["help"])


class ModeLabelWiringTest(unittest.TestCase):
    html = (WEB / "index.html").read_text(encoding="utf-8")
    chat = (WEB / "agentchat.js").read_text(encoding="utf-8")
    slash = (WEB / "slash.js").read_text(encoding="utf-8")

    def test_the_label_sits_by_the_new_conversation_button_and_says_what_it_does(self):
        self.assertRegex(re.sub(r'\n\s*(?:<button[^>]*id="(?:wud-toggle|tech-toggle|priv-toggle|forget-open|tgm-move|ch-more)"|<div class="ch-panel")[^\n]*', '', self.html), r'id="new-chat"[^\n]*\n\s*<button[^>]*id="branches-open"[^\n]*\n\s*<button[^>]*id="export-open"[^\n]*\n\s*<button[^>]*id="pskills-open"[^\n]*\n\s*<button[^>]*id="chg-open"[^\n]*\n\s*<span[^>]*id="folder-chip"[^\n]*\n\s*<button[^>]*id="branch-chat"[^\n]*\n\s*<button[^>]*id="mode-chip"[^>]*aria-keyshortcuts="Shift\+Tab"')
        self.assertIn('"ch.chip"', (WEB / "i18n.js").read_text(encoding="utf-8"))
        i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
        self.assertIn('"ch.chip": "Shift + TAB to change mode: {0}", "ch.chip.hint": "Shift + TAB to change mode:"', i18n)
        self.assertNotIn("actual", re.search(r'"ch.chip": "[^"]*"', i18n).group(0))
        self.assertIn("mc-hint", self.chat)
        self.assertIn("mc-mode", self.chat)

    def test_shift_tab_is_taken_only_in_the_message_box_and_only_where_there_is_a_mode(self):
        self.assertIn('e.key === "Tab" && e.shiftKey', self.slash)
        self.assertIn("A().perm.available()", self.slash)
        self.assertIn("prompt.addEventListener(\"keydown\"", self.slash)
        self.assertNotIn("document.addEventListener(\"keydown\"", self.slash)          # the rest of the page keeps its keyboard

    def test_going_to_automatic_asks_nothing_and_sends_no_confirmation(self):
        for gone in ("confirmAuto", "confirm_auto", "AUTO_OK", "autook", "riskConfirm"):
            self.assertNotIn(gone, self.chat)

    def test_the_other_risk_questions_are_answered_yes_by_themselves_while_the_mode_is_automatic(self):
        risk = (WEB / "risk.js").read_text(encoding="utf-8")
        self.assertIn('window.tadamooAutoMode = () => permNow() === "auto";', self.chat)
        body = risk[risk.index("window.riskConfirm"):]
        before_dialog = body[:body.index("showModal")]
        self.assertIn("window.tadamooAutoMode()", before_dialog)
        self.assertIn("resolve(true); return;", before_dialog)

    def test_the_menu_is_a_listbox_the_message_box_controls_and_the_script_is_loaded_after_the_chat(self):
        self.assertIn('id="slash-menu" role="listbox"', self.html)
        self.assertLess(self.html.index("web/agentchat.js"), self.html.index("web/slash.js"))
        for attr in ('"aria-controls", "slash-menu"', '"aria-activedescendant"', '"role", "option"'):
            self.assertIn(attr, self.slash)

    def test_the_chat_hands_a_typed_command_over_before_it_sends(self):
        js = (WEB / "chat.js").read_text(encoding="utf-8")
        self.assertRegex(js, r'async function send\(cont, auto\) \{[^}]*\}\s*else \{\s*if \(prompt\.value\.startsWith\("/"\) && window\.slash\)')

    def test_the_thinking_list_follows_the_conversation(self):
        self.assertIn("effort: effortSel.value", self.chat)
        self.assertIn("`/${sid}/effort`", self.chat)
        self.assertIn('typeof rec.effort === "string"', self.chat)


if __name__ == "__main__":
    unittest.main()
