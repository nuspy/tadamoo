"""The Chat's form for a request for input from an MCP server: drawn once, typed values checked before they go, in both languages."""
import re
import unittest
from pathlib import Path

from harness.mcp_elicit import ELICIT_PREFIX

WEB = Path(__file__).resolve().parent.parent / "web"
JS = (WEB / "agentchat.js").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class ElicitPageTest(unittest.TestCase):
    def setUp(self):
        self.block = JS[JS.index('b.kind === "mcp_elicitation") {'):JS.index('b.kind === "capability_choice") {')]

    def test_the_prefix_is_the_one_the_harness_reads_and_the_answer_is_cut_where_the_session_cuts_it(self):
        self.assertIn(f'prefix = "{ELICIT_PREFIX}"', self.block)
        self.assertIn("prefix.length + 500", self.block)

    def test_the_form_is_not_redrawn_while_the_person_types_and_nothing_is_sent_without_a_required_value(self):
        self.assertIn('if (el.dataset.built && !done) return;', self.block)
        self.assertIn("ch.mcp.need", self.block)

    def test_the_servers_words_enter_as_text_and_the_warning_is_always_shown(self):
        for banned in ("innerHTML", "insertAdjacentHTML"):
            self.assertNotIn(banned, self.block)
        self.assertIn('mk("blockquote", "aask-quote", f.message || "")', self.block)
        self.assertIn('T("ch.mcp.warn")', self.block)

    def test_every_text_exists_in_both_languages_and_the_inputs_are_big_enough_to_touch(self):
        for key in set(re.findall(r'T\("(ch\.mcp\.[a-z]+)"', self.block)) | {"ch.q.mcp_elicitation", "ch.mcp.cancel", "ch.mcp.decline"}:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        self.assertRegex(CSS, r"\.aask-field input:not\(\[type=checkbox\]\)[^}]*min-height: 44px")


if __name__ == "__main__":
    unittest.main()
