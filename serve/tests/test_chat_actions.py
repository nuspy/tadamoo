"""The ends of a prompt, a reasoning window and an answer: a copy icon, and Branch from here after an answer."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class ChatActionsTest(unittest.TestCase):
    def test_a_prompt_a_reasoning_window_and_an_answer_each_end_with_a_copy_icon(self):
        self.assertIn('acts.append(copyButton(() => it.text || ""), backButton(it), skillButton(it));', CHAT)
        self.assertIn('d.querySelector(".think-end").append(copyButton(() => b.s || ""));', CHAT)
        self.assertIn("it.actsEl.append(copyButton(() => answerText(it)), save, branchBtn);", CHAT)
        self.assertIn('const answerText = it => it.blocks.filter(x => x.t === "text")', CHAT)

    def test_an_answer_that_is_done_can_be_branched_from_where_it_stands(self):
        self.assertIn("if (it.done && !it.actsEl)", CHAT)
        self.assertIn('items.slice(0, items.indexOf(it)).filter(x => x.kind === "user").length', CHAT)
        self.assertIn("async function branch(at)", CHAT)
        self.assertIn("Number.isInteger(at) ? at :", CHAT)

    def test_the_icons_are_drawn_without_markup_and_the_texts_exist_in_both_languages(self):
        for banned in ("innerHTML", "insertAdjacentHTML"):
            self.assertNotIn(banned, CHAT)
        for key in ("ch.copy", "ch.copied", "ch.copy.fail", "ch.branch.from", "ch.branch.from.tip"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        self.assertRegex(CHAT, r'createElementNS\(SVGNS, "svg"\)')


class FileLinksTest(unittest.TestCase):
    chat = (WEB / "chat.js").read_text(encoding="utf-8")

    def test_a_link_to_a_file_the_agent_made_or_to_a_file_in_the_folder_becomes_a_button_that_saves_it(self):
        self.assertIn("tadamoo-file:([a-f0-9]{12})", self.chat)
        self.assertIn("tadamoo-path:", self.chat)
        self.assertEqual(self.chat.count('class="afile"'), 2)
        self.assertIn('e.target.closest("button.afile")', CHAT)
        self.assertIn("saveOutput(b.dataset.id, b.dataset.name, b)", CHAT)
        self.assertIn("saveWorkspace(b.dataset.path, b.dataset.name, b)", CHAT)
        self.assertIn("api/agent/sessions/${encodeURIComponent(sid)}/file?path=", CHAT)

    def test_a_file_the_agent_wrote_or_edited_has_its_own_save_button_in_its_step(self):
        self.assertIn('["write_file", "edit_file"].includes(b.name) && b.status === "ok"', CHAT)

    def test_saving_goes_through_the_one_function_the_desktop_app_and_the_browser_share(self):
        saves = CHAT[CHAT.index("async function saveWorkspace"):CHAT.index("async function saveBlob")]
        self.assertIn("await saveBlob(", saves)
        self.assertIn("headers: authHeaders()", saves)
        body = CHAT[CHAT.index("async function saveBlob"):CHAT.index("async function saveBlob") + 2500]
        for way in ("window.chrome.webview", "showSaveFilePicker", "a.download = name"):
            self.assertIn(way, body)


if __name__ == "__main__":
    unittest.main()
