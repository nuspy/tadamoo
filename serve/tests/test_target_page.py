"""The Target Loop's page: the button, the dialog, the list tools of the text boxes, their texts in both languages and their styles.

No browser is needed: the list logic is run by node (listtools_check.js) and the rest is read from the files."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve.tests.test_web_static import NODE, contrast, strings

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WEB = ROOT / "serve" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
TARGET_JS = (WEB / "target.js").read_text(encoding="utf-8")
LIST_JS = (WEB / "listtools.js").read_text(encoding="utf-8")
CSS = (WEB / "target.css").read_text(encoding="utf-8")
HARNESS = (ROOT / "harness" / "target.py").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the list tools")
class ListToolsLogicTest(unittest.TestCase):
    def test_every_case_of_the_list_logic_passes(self):
        out = subprocess.run([NODE, str(HERE / "listtools_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 20)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class PageWiringTest(unittest.TestCase):
    def test_the_button_sits_next_to_send_and_the_dialog_has_what_the_script_uses(self):
        self.assertLess(HTML.index('id="target-open"'), HTML.index('id="send"'))
        self.assertGreater(HTML.index('id="target-open"'), HTML.index('id="stop"'))
        dialog = HTML[HTML.index('<dialog class="tgd"'):HTML.index('</dialog>', HTML.index('<dialog class="tgd"'))]
        for needle in ('id="tg-text"', 'id="tg-start"', 'id="tg-list"', 'id="tg-new"', 'id="tg-close"', 'id="tg-bar"', 'id="tg-view"', 'id="tg-verify"',
                       'id="tg-files"', 'id="tg-stall"', 'id="tg-max"', 'id="tg-first"', 'id="tg-status"', 'role="status"', 'aria-labelledby="tg-title"'):
            self.assertIn(needle, dialog, needle)
        for ident in re.findall(r'\$t\("([\w-]+)"\)', TARGET_JS):
            self.assertIn(f'id="{ident}"', dialog, ident)

    def test_the_first_message_option_is_a_labelled_choice_the_script_reads_and_writes(self):
        dialog = HTML[HTML.index('<dialog class="tgd"'):HTML.index('</dialog>', HTML.index('<dialog class="tgd"'))]
        self.assertRegex(dialog, r'<label for="tg-first"')
        self.assertRegex(dialog, r'<select id="tg-first"[^>]*aria-describedby="tg-first-h"')
        self.assertIn('value="joined"', dialog)
        self.assertIn('value="separate"', dialog)
        self.assertIn("first_message: $t(\"tg-first\").value", TARGET_JS)
        self.assertIn("opts.first_message", TARGET_JS)

    def test_the_scripts_load_in_the_order_they_need(self):
        self.assertLess(HTML.index("web/app.js"), HTML.index("web/listtools.js"))
        self.assertLess(HTML.index("web/listtools.js"), HTML.index("web/chat.js"))       # its key handler on the prompt box comes first
        self.assertLess(HTML.index("web/listtools.js"), HTML.index("web/slash.js"))
        self.assertLess(HTML.index("web/agentchat.js"), HTML.index("web/target.js"))     # the dialog uses window.agentChat
        self.assertIn('href="web/target.css"', HTML)

    def test_the_prompt_box_sends_on_ctrl_enter_and_the_list_tools_take_plain_enter_first(self):
        chat = (WEB / "chat.js").read_text(encoding="utf-8")
        self.assertIn('e.key === "Enter" && !e.shiftKey', chat)                          # Ctrl+Enter is not excluded: it still sends
        self.assertIn('{ bar:', LIST_JS)
        self.assertIn("}, true);", LIST_JS)                                                # a capture listener: it runs before the chat's own
        self.assertIn("stopImmediatePropagation", LIST_JS)
        for needle in ("!e.ctrlKey", "!e.shiftKey", "wasEscaped"):
            self.assertIn(needle, LIST_JS)                                                 # Ctrl+Enter, Shift+Enter and Esc then Tab are the browser's

    def test_the_list_tools_reach_the_prompt_box_and_the_dialog(self):
        self.assertIn('getElementById("prompt")', LIST_JS)
        self.assertIn("window.ListTools.attach(text", TARGET_JS)
        self.assertIn("Digit7", LIST_JS)
        self.assertIn("Digit8", LIST_JS)
        self.assertIn("execCommand", LIST_JS)                                              # one edit the browser can undo

    def test_the_chat_shows_the_loops_notes_and_tells_the_dialog(self):
        chat = (WEB / "agentchat.js").read_text(encoding="utf-8")
        self.assertIn('case "target":', chat)
        self.assertIn('new CustomEvent("basalt:target"', chat)
        self.assertIn('"basalt:target"', TARGET_JS)

    def test_the_dialog_talks_to_the_conversations_target_routes_with_the_api_key(self):
        self.assertIn("api/agent/sessions/${sid}/targets", TARGET_JS)
        self.assertIn("authHeaders()", TARGET_JS)
        for action in ("notes", "stop", "rerun", "forget"):
            self.assertIn(f"{action}", TARGET_JS)
        self.assertIn('{ mode: "resume" }', TARGET_JS)
        self.assertIn('{ mode: "copy" }', TARGET_JS)

    def test_the_text_of_the_target_is_never_inserted_as_markup(self):
        for text in (TARGET_JS, LIST_JS):
            for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
                self.assertNotIn(banned, text)

    def test_closing_stops_the_polling_and_escape_closes_the_dialog(self):
        self.assertIn('dlg.addEventListener("close"', TARGET_JS)
        self.assertIn("clearTimeout(timer)", TARGET_JS)
        self.assertIn("showModal()", TARGET_JS)                                            # a native modal dialog: Escape and focus return are the browser's

    def test_the_button_is_disabled_with_the_reason_when_there_is_no_conversation_with_the_agent(self):
        self.assertIn("openBtn.disabled = !ok", TARGET_JS)
        self.assertIn('T("tg.open.needs")', TARGET_JS)


@unittest.skipUnless(NODE, "node is needed to read the translations")
class TextsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = strings()

    def keys_used(self):
        keys = set()
        for text in (TARGET_JS, LIST_JS):
            keys |= {k for k in re.findall(r'\bT\(\s*"([\w.]+)"', text) if not k.endswith(".")}
        keys |= {k for k in re.findall(r'data-t(?:-title|-ph|-aria)?="(tg\.[\w.]+|lt\.[\w.]+)"', HTML)}
        return keys

    def test_every_text_the_page_uses_exists_in_both_languages(self):
        keys = self.keys_used()
        self.assertGreater(len(keys), 40)
        for lang in ("en", "it"):
            self.assertEqual(sorted(k for k in keys if k not in self.s[lang]), [], lang)

    def test_every_state_phase_flag_reason_and_note_the_server_can_send_has_a_text(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from harness import target as tg
        wanted = {f"tg.state.{x}" for x in tg.TARGET_STATES} | {f"tg.crit.{x}" for x in tg.CRITERION_STATES} | {f"tg.task.{x}" for x in tg.TASK_STATES}
        wanted |= {f"tg.stop.{x}" for x in tg.STOP_REASONS} | {f"tg.report.{x}" for x in tg.STOP_REASONS}
        wanted |= {f"tg.phase.{x}" for x in set(re.findall(r'phase="(\w+)"', HARNESS))}
        wanted |= {f"tg.flag.{x}" for x in re.findall(r'flags=\[?\{?"(\w+)"', HARNESS)}
        wanted |= {f"tg.note.{x}" for x in ("planned", "task", "pass", "fix", "approach", "achieved", "blocked", "stopped")}
        said = set(re.findall(r'_say\("(\w+)"', HARNESS)) | {"fix", "achieved", "blocked", "stopped"}      # `_say(state, ...)` and the fix/approach choice are not literal
        self.assertEqual(said - {"planned", "task", "pass", "approach"}, {"fix", "achieved", "blocked", "stopped"})
        wanted |= {f"tg.check.{x}" for x in ("command", "file")}
        wanted |= {"tg.report.achieved", "tg.report.blocked"}
        for code in ("busy", "text_missing", "text_long", "not_running", "gone", "achieved", "closed", "running"):
            wanted.add(f"tg.e.{code}")
        for lang in ("en", "it"):
            self.assertEqual(sorted(k for k in wanted if k not in self.s[lang]), [], lang)
        self.assertIn("flags", HARNESS)

    def test_the_server_codes_the_page_translates_are_the_ones_the_server_uses(self):
        for code in re.findall(r'TargetError\([^)]*"(\w+)"\)', (ROOT / "serve" / "target_api.py").read_text(encoding="utf-8") + HARNESS):
            if code not in ("bad_note", "bad_status", "bad_mode"):
                self.assertIn(f"tg.e.{code}", self.s["en"], code)

    def test_placeholders_match_and_the_italian_is_not_the_english(self):
        for key, en in self.s["en"].items():
            if key.startswith(("tg.", "lt.")):
                self.assertEqual(sorted(re.findall(r"\{\d+\}", en)), sorted(re.findall(r"\{\d+\}", self.s["it"][key])), key)
        for key in ("tg.help", "tg.text.help", "tg.sec.criteria.help", "tg.stop.tip", "tg.mark.achieved", "lt.help", "tg.report.stalled"):
            self.assertNotEqual(self.s["en"][key], self.s["it"][key], key)

    def test_every_control_of_the_dialog_explains_itself(self):
        dialog = HTML[HTML.index('<dialog class="tgd"'):HTML.index('</dialog>', HTML.index('<dialog class="tgd"'))]
        for tag in re.findall(r"<button[^>]*>", dialog):
            self.assertRegex(tag, r'data-t-title="tg\.[\w.]+"', tag)
        for ident in re.findall(r'<(?:textarea|input)[^>]*id="([\w-]+)"', dialog):
            self.assertRegex(dialog, rf'for="{ident}"', ident)                              # a label
            self.assertRegex(dialog, rf'<(?:textarea|input)[^>]*id="{ident}"[^>]*aria-describedby="[\w-]+"', ident)   # and a help line
        # the buttons the script builds carry a tooltip as their last argument
        for call in re.findall(r'\bbtn\(T\("tg\.[\w.]+"\), "[^"]*", [^\n]*?, (T\("tg\.[\w.]+"\))\)', TARGET_JS):
            self.assertIn(".tip", call)
        self.assertGreaterEqual(len(re.findall(r'\btip\)|T\("tg\.[\w.]+\.tip"\)', TARGET_JS)), 6)
        for key in ("lt.help", "lt.ol", "lt.ul"):
            self.assertIn(f'T("{key}")', LIST_JS)


class StyleTest(unittest.TestCase):
    def test_the_text_box_takes_sixty_percent_of_the_window_and_can_be_resized(self):
        rule = re.search(r"#tg-text \{([^}]*)\}", CSS).group(1)
        self.assertIn("height: calc(60vh / var(--z, 1))", rule)            # the zoomed body of a wide window keeps it at 60 percent
        self.assertIn("resize: vertical", rule)
        self.assertNotIn("monospace", rule)
        self.assertNotIn("mono", rule)

    def test_buttons_reach_thirty_pixels_and_nothing_scrolls_sideways(self):
        self.assertRegex(CSS, r"\.lt-btn \{[^}]*min-height: var\(--btn-h, 30px\)")
        self.assertNotRegex(CSS, r"overflow-x:\s*(auto|scroll)")
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertIn(":focus-visible", CSS)
        self.assertRegex(CSS, r"font: 400 16px/1\.55")                                       # 16 px: no zoom on focus

    def test_achieved_is_struck_through_in_red_and_is_also_written(self):
        self.assertRegex(CSS, r"\.tg-mark-achieved \{[^}]*line-through[^}]*var\(--tg-red\)")
        self.assertRegex(CSS, r"\.tgd-crit\[data-s=\"achieved\"\] \.tgd-crit-t \{[^}]*line-through[^}]*var\(--tg-red\)")
        self.assertIn("tgd-crit-st", TARGET_JS)                                              # the status in words beside it
        self.assertIn('"sr"', TARGET_JS)                                                     # and for a screen reader inside the highlighted text

    def test_the_colours_keep_the_contrast_of_the_rest_of_the_page(self):
        red = re.search(r"--tg-red:\s*(#[0-9A-Fa-f]{6})", CSS).group(1)
        for ground in ("#151A1F", "#1A2128", "#0E1114"):                                      # panel, surface, background
            self.assertGreaterEqual(contrast(red, ground), 4.5, ground)
        for fg in ("#9AA4AD", "#8A95A0", "#E9ECEE"):                                          # --fg-2, --fg-3, --fg
            self.assertGreaterEqual(contrast(fg, "#1A2128"), 4.5, fg)


if __name__ == "__main__":
    unittest.main()
