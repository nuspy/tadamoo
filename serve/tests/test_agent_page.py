"""The Agent tab: the conversation page working in a folder the person added, with the folders in its column and the changes beside it."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
PROJ = (WEB / "projects.js").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class AgentPageTest(unittest.TestCase):
    def test_the_old_one_shot_page_is_gone_and_the_tab_shows_the_conversation_page(self):
        for gone in ("agent.js", "agent.css"):
            self.assertFalse((WEB / gone).exists(), gone)
            self.assertNotIn(gone, HTML)
        self.assertNotIn('id="view-agent"', HTML)
        self.assertNotIn('id="ag-form"', HTML)
        self.assertIn('const SHOWN_AS = { agent: "chat" };', APP)
        for script in ("chat.js", "chatset.js", "slash.js", "agentchat.js"):
            self.assertIn('"agent"', (WEB / script).read_text(encoding="utf-8"), script)       # each one that waits for the page waits for it in both tabs

    def test_the_column_has_a_button_that_adds_a_folder_and_the_window_asks_for_its_path(self):
        self.assertIn('id="cv-addfolder"', HTML)
        for needle in ('id="afolder"', 'id="af-path"', 'id="af-browse"', 'id="af-form"'):
            self.assertIn(needle, HTML)
        self.assertIn('app.postMessage({ type: "pickFolder", id: "af-path"', PROJ)             # the Windows dialog inside the app, a typed path in a browser
        self.assertIn('api/agent/projects', PROJ)
        self.assertLess(HTML.index("web/projects.js"), HTML.index("web/agentchat.js"))

    def test_a_conversation_in_the_agent_tab_is_made_in_the_folder_and_asks_for_one_first(self):
        self.assertIn('...(inAgent() ? { workspace: curPath() } : {})', CHAT)
        self.assertIn("if (inAgent() && !curPath() && !(await addFolder()))", CHAT)
        self.assertIn("if (inAgent() && !curPath()) { addFolder(); return; }", CHAT)
        self.assertIn('if (agent) mode = "agent";', CHAT)                                      # no model-only chat or question-only kind in a folder
        self.assertIn('$("#chatmode-label").hidden = inAgent();', CHAT)
        self.assertIn("const inContext = x => (inAgent() ? !isChatSession(x) : isChatSession(x));", CHAT)

    def test_each_tab_remembers_its_own_last_conversation_and_does_not_clear_the_other(self):
        self.assertIn('LAST_KEYS = { chat: "basalt.chat.session", agent: "basalt.agent.session" }', CHAT)
        self.assertIn("keep(LAST_KEYS.chat, kept.chat); keep(LAST_KEYS.agent, kept.agent);", CHAT)
        self.assertNotRegex(CHAT, r"\bLAST_KEY\b")

    def test_the_changes_panel_is_beside_the_conversation_and_opens_from_the_header(self):
        for needle in ('id="chg-side"', 'id="chg-open"', 'id="changes-root"', 'id="folder-chip"'):
            self.assertIn(needle, HTML)
        self.assertNotIn("task-root", HTML)
        self.assertIn("showChanges(chgSide.hidden)", CHAT)

    def test_the_class_of_the_current_folder_does_not_borrow_the_charts_cursor(self):
        self.assertNotIn('" cur"', CHAT)
        self.assertNotIn(".cv-projhead.cur", (WEB / "app.css").read_text(encoding="utf-8"))

    def test_the_new_texts_exist_in_both_languages_and_the_page_writes_no_markup(self):
        for text in (CHAT, PROJ):
            for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
                self.assertNotIn(banned, text)
        keys = {k for k in re.findall(r'T\("(ag\.[a-z.]+)"', CHAT + PROJ + HTML) if not k.endswith(".")}
        keys |= set(re.findall(r'data-t(?:-title|-ph)?="(ag\.[a-z.]+)"', HTML))
        self.assertGreater(len(keys), 12)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)


if __name__ == "__main__":
    unittest.main()
