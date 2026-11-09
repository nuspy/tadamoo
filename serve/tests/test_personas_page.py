"""Settings > Personalities and the menu in the header of Chat and Code: the markup, the scripts that use the routes, and the texts."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "personas.js").read_text(encoding="utf-8")
CSS = (WEB / "personas.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
SUBTABS = (WEB / "subtabs.js").read_text(encoding="utf-8")
SHELL = (WEB / "shell.js").read_text(encoding="utf-8")
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
AGENT = (WEB / "agentchat.js").read_text(encoding="utf-8")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿]")


class PersonasPageTest(unittest.TestCase):
    def test_the_page_is_a_part_of_settings_with_its_script_style_and_tab(self):
        self.assertRegex(HTML, r'<section class="sec stpersonas" data-sub="personas" id="pe-section" aria-labelledby="pe-h"')
        self.assertIn('id="pe-root"', HTML)
        self.assertIn('<script defer src="web/personas.js"></script>', HTML)
        self.assertIn('href="web/personas.css"', HTML)
        self.assertIn('{ id: "personas", view: "settings", label: () => T("sub.personas") }', SUBTABS)
        self.assertIn('settingsAt("pe-h", "personas")', SHELL)

    def test_the_header_of_chat_and_code_has_one_menu_for_both(self):
        self.assertRegex(HTML, r'<label class="effort" for="persona" id="persona-label" hidden>')
        self.assertRegex(HTML, r'<select id="persona" data-t-title="pe\.tip"></select>')
        head = HTML[HTML.index('class="sh chathead"'):HTML.index('id="cs-open"')]
        self.assertIn('id="persona-label"', head)                                                   # the header both tabs share
        self.assertLess(HTML.index('id="coderole-label"'), HTML.index('id="persona-label"'))

    def test_every_route_of_the_server_is_used(self):
        for route in ('"api/personas"', "`api/personas/${encodeURIComponent(chosen)}?${q}`", "`api/personas/${encodeURIComponent(chosen)}/update`", "`api/personas/${encodeURIComponent(chosen)}/delete`",
                      "`api/personas/${encodeURIComponent(chosen)}/memory/${action}`", "`api/personas/${encodeURIComponent(slug)}/compose`", "`api/personas/${encodeURIComponent(slug)}/learn`", "`api/agent/sessions/${encodeURIComponent(sid)}/persona`"):
            self.assertIn(route, JS)
        for action in ('mpost("item"', 'mpost("confirm"', 'mpost("delete"', 'mpost("clear"', 'mpost("forget-learned"'):
            self.assertIn(action, JS)
        self.assertIn("authHeaders()", JS)
        self.assertIn('e.detail === "personas"', JS)

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)
        self.assertIn("textContent = text", JS)

    def test_deleting_needs_two_presses_and_sends_the_confirmation(self):
        self.assertEqual(JS.count("{ confirm: true }"), 3)                                          # the personality, all its notes, and what was learned
        self.assertIn('if (!sure) { arm("forget"); return; }', JS)
        self.assertIn('if (!sure) { arm("delete"); return; }', JS)
        self.assertIn('if (!sure) { arm("clear"); return; }', JS)
        self.assertIn('if (!sure) { arm("d" + i.id); return; }', JS)

    def test_the_agent_gets_the_choice_at_creation_and_when_it_changes(self):
        self.assertIn("...(window.personaUi && window.personaUi.pending() ? { persona: window.personaUi.pending() } : {})", AGENT)
        self.assertIn('case "persona":', AGENT)
        self.assertIn("rec.persona = e.slug", AGENT)
        self.assertIn("{ persona: slug }", JS)

    def test_the_model_only_chat_puts_the_personality_in_its_system_message(self):
        self.assertIn("await window.personaUi.system(text)", CHAT)
        self.assertIn('[chatSettings.system(), personaText].filter(Boolean).join("\\n\\n")', CHAT)
        self.assertIn('document.addEventListener("tadamoo:persona", () => { personaText = ""; })', CHAT)
        self.assertIn("window.personaUi.reset()", CHAT)                                              # a new conversation starts with none
        self.assertIn('const KEY = "tadamoo.chat.persona"', JS)
        self.assertIn("if (!slug || withSessions()) return \"\"", JS)                               # a conversation with a harness has it composed by the harness

    def test_learning_from_conversations_has_a_switch_a_line_and_a_forget_button(self):
        self.assertIn('cb.type = "checkbox"; cb.id = "pe-learn-on"; cb.checked = on;', JS)
        self.assertIn("{ learn: cb.checked }", JS)                                                   # kept in persona.json by the update route
        self.assertIn('T("pe.learn.line", L.conversations, when(L.last))', JS)                      # learned from N conversations, last at
        self.assertIn('i.source === "conversation"', JS)                                            # the learned notes carry a mark
        self.assertIn(".pe-switch { display: flex; align-items: center; gap: 12px; min-height: 44px;", CSS)
        self.assertIn("window.personaUi.learn(", CHAT)                                               # "New conversation" hands the model-only chat over
        self.assertIn("!isPrivate()", CHAT[CHAT.index("window.personaUi.learn(") - 40:CHAT.index("window.personaUi.learn(") + 20])        # never a private one
        self.assertLess(CHAT.index("window.personaUi.learn("), CHAT.index("window.personaUi.reset()"))      # before the choice is cleared
        self.assertIn("if (!slug || withSessions() || !lines.length) return;", JS)                  # a conversation with a harness is learned from by the server

    def test_the_texts_exist_in_both_languages_and_have_no_emoji(self):
        keys = set(re.findall(r'\bT\(\s*"(pe\.[\w.]+)"', JS)) | set(re.findall(r'data-t(?:-title)?="(pe\.[\w.]+)"', HTML)) | {"sub.personas", "pal.set.personas"}
        self.assertGreater(len(keys), 40)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        for text in (JS, CSS, "\n".join(l for l in I18N.splitlines() if '"pe.' in l)):
            self.assertIsNone(EMOJI.search(text))

    def test_the_style_is_made_for_a_phone_first(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("prefers-reduced-motion: reduce", CSS)
        self.assertIn("@media (min-width: 900px)", CSS)                                              # one column first, two on a wide window
        self.assertIn("@media (pointer: coarse), (max-width: 520px) { .chathead #persona { min-height: 44px; } }", CSS)
        self.assertNotRegex(re.sub(r"rgba?\([^)]*\)", "", CSS), r"(?<![\w-])color:\s*#")            # the colours are the page's own
        self.assertNotIn("hover", CSS)                                                               # nothing that exists only under a mouse


if __name__ == "__main__":
    unittest.main()
