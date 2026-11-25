"""What the chat shows of the reasoning and the actions: two settings a preset keeps (full, compact, absent), the Chat's reasoning box that follows the first, and in the Agent chat one container for each run
of consecutive reasoning blocks and each run of consecutive action blocks. node runs the page's own chatdisplay.js against a small stand-in for the page."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve import profiles

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
CHATSET = (WEB / "chatset.js").read_text(encoding="utf-8")
CHAT = (WEB / "chat.js").read_text(encoding="utf-8")
AGENT = (WEB / "agentchat.js").read_text(encoding="utf-8")
DISPLAY = (WEB / "chatdisplay.js").read_text(encoding="utf-8")

DRIVER = r"""
const vm = require('vm'), fs = require('fs');
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.parent = null; this.dataset = {}; this.hidden = false; this.open = false; this.className = ''; this.textContent = ''; this.listeners = {}; }
  append(...kids) { for (const k of kids) { if (k.parent) k.parent.children.splice(k.parent.children.indexOf(k), 1); k.parent = this; this.children.push(k); } }
  before(node) { const p = this.parent; if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = p; p.children.splice(p.children.indexOf(this), 0, node); }
  addEventListener(n, f) { this.listeners[n] = f; }
  get parentNode() { return this.parent; }
}
const modes = JSON.parse(process.argv[3]);
const ctx = { document: { createElement: t => new El(t), querySelectorAll: () => [] }, chatSettings: { shows: k => modes[k] }, console };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1] + '/chatdisplay.js', 'utf8'), ctx);
const D = ctx.window.chatDisplay, T = (k, ...a) => k + '[' + a.join(',') + ']';
const it = { body: new El('div'), blocks: JSON.parse(process.argv[2]) };
for (const b of it.blocks) { b.row = new El('div'); b.el = b.row; D.place(it, b); }
const paintModes = process.argv[5] ? JSON.parse(process.argv[5]) : undefined;
D.paint(it, T, paintModes);
const shape = it.body.children.map(c => c.tag === 'details' ? { group: c.dataset.kind, rows: c.children[1].children.length, open: c.open, hidden: c.hidden, title: c.children[0].children[0].textContent } : { row: true, hidden: c.hidden });
if (process.argv[4] === 'flat') console.log(JSON.stringify(shape.filter(c => !c.group || !c.hidden)));
else
console.log(JSON.stringify(shape));
"""


def shape(blocks, reasoning="full", tooling="full", flat=False, paint_modes=None):
    args = [str(WEB), json.dumps(blocks), json.dumps({"reasoning": reasoning, "tooling": tooling}), "flat" if flat else "-"] + ([json.dumps(paint_modes)] if paint_modes else [])
    out = subprocess.run([NODE, "-e", DRIVER, *args], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


R = {"t": "reasoning", "s": "thinking"}
TOOL = lambda name, status="ok": {"t": "tool", "name": name, "status": status}
TEXT = {"t": "text", "s": "answer"}


class Settings(unittest.TestCase):
    def test_a_preset_keeps_both_and_only_the_three_values(self):
        self.assertIn("show_reasoning", profiles.INFERENCE_KEYS)
        self.assertIn("show_tooling", profiles.INFERENCE_KEYS)
        for value in ("full", "compact", "absent", ""):
            self.assertEqual(profiles.check_inference({"show_reasoning": value, "show_tooling": value})["show_tooling"], value)
        for bad in ("none", "hidden", 3, True):
            with self.assertRaises(ValueError):
                profiles.check_inference({"show_tooling": bad})
            with self.assertRaises(ValueError):
                profiles.check_inference({"show_reasoning": bad})

    def test_the_panel_reads_and_writes_them_and_the_old_checkbox_is_gone(self):
        for key in ("show_reasoning", "show_tooling"):
            self.assertIn(f'id="cs-{key}"', HTML)
            self.assertRegex(HTML, rf'<label[^>]*for="cs-{key}"')
            self.assertRegex(HTML, rf'id="cs-{key}"[^>]*aria-describedby="cs-{key}-h"')
            self.assertIn(f'{key}: $("#cs-{key}").value', CHATSET)
            self.assertIn(f'$("#cs-{key}").value = s.{key} || "full"', CHATSET)
        self.assertNotIn("cs-show-reasoning", HTML + CHATSET)
        self.assertNotIn("showReasoning", CHATSET + CHAT)
        self.assertIn("shows,", CHATSET)

    def test_a_profile_keeps_every_setting_of_the_panel(self):
        read = CHATSET[CHATSET.index("function read() {"):CHATSET.index("for (const f of fieldsOf()) s[f.key]")]
        keys = set(re.findall(r"\b([a-z_]+):", read)) - {"checked", "value", "read", "write"}
        keys |= set(re.findall(r'\{ key: "([a-z_]+)"', CHATSET))
        keys |= set(re.findall(r"\b([a-z_]+): (?:box|field|level|radio)\(", CHATSET))
        keys -= {"kind"}
        self.assertTrue({"system", "temperature", "max_answer_tokens", "abstract_on", "images", "billion_port", "show_tooling", "autonomy", "effort"} <= keys, keys)
        self.assertEqual(sorted(keys - set(profiles.INFERENCE_KEYS)), [])

    def test_the_old_answers_billion_context_and_the_pictures_are_kept_and_checked(self):
        good = {"abstract_on": False, "abstract_keep": "8", "code_keep": 4, "cold_tools": False, "billion_on": True, "billion_compress": False, "billion_port": "7300", "images": "high", "charts": "", "autonomy": "plan"}
        self.assertEqual(profiles.check_inference(good), good)
        for bad in ({"abstract_on": "yes"}, {"cold_tools": "no"}, {"billion_on": 1}, {"abstract_keep": 0}, {"abstract_keep": 21}, {"code_keep": 6}, {"billion_port": 80}, {"billion_port": "x"}, {"images": "huge"}, {"charts": 3}, {"autonomy": "root"}, {"autonomy": 2}):
            with self.assertRaises(ValueError, msg=str(bad)):
                profiles.check_inference(bad)

    def test_the_dark_web_switches_are_kept_by_a_saved_profile_checked_and_written_back_only_when_one_is_loaded_by_hand(self):
        good = {"darkweb": True, "darkweb_search": False, "darkweb_read": True}
        self.assertEqual(profiles.check_inference(good), good)
        for bad in ({"darkweb": "yes"}, {"darkweb_search": 1}, {"darkweb_read": None}):
            with self.assertRaises(ValueError, msg=str(bad)):
                profiles.check_inference(bad)
        self.assertIn('darkweb: dw("darkweb")', CHATSET)
        self.assertIn("window.dwSet.write(s)", CHATSET)
        self.assertIn("if (apply && window.dwSet", CHATSET)

    def test_the_page_reader_and_browser_override_are_checked_and_applied_only_when_a_preset_is_loaded_by_hand(self):
        good = {"reader_override": "firecrawl", "browser_override": "tadamoo"}
        self.assertEqual(profiles.check_inference(good), good)
        self.assertEqual(profiles.check_inference({"reader_override": "", "browser_override": ""}), {"reader_override": "", "browser_override": ""})
        for bad in ({"reader_override": "chrome"}, {"browser_override": "edge"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                profiles.check_inference(bad)
        self.assertIn('reader_override: $("#cs-browsing-reader").value', CHATSET)
        self.assertIn('browser_override: $("#cs-browsing-browser").value', CHATSET)
        self.assertIn("if (apply) applyBrowsingDefaults(s.reader_override, s.browser_override)", CHATSET)
        self.assertIn('id="cs-browsing"', HTML)

    def test_only_a_preset_loaded_by_hand_reaches_the_conversation_and_the_billion_context_process(self):
        self.assertIn("write(p.settings, true)", CHATSET)
        self.assertIn("write(s, true)", CHATSET)
        self.assertIn('if (apply && s.billion_on !== undefined) fire("cs-bc-on")', CHATSET)
        self.assertIn("if (quiet) return;", CHATSET)
        self.assertIn('level.dispatchEvent(new Event("change"', CHATSET)

    def test_every_string_exists_in_both_languages(self):
        used = set(re.findall(r'data-t="((?:cs\.show[\w.]*|cs\.display))"', HTML)) | set(re.findall(r'T\("(ch\.group\.[\w]+)"', DISPLAY))
        self.assertTrue({"cs.display", "cs.show_reasoning", "cs.show_tooling", "cs.show.full", "cs.show.compact", "cs.show.absent", "ch.group.reasoning", "ch.group.tooling"} <= used)
        for key in sorted(used | {"cs.show_reasoning.help", "cs.show_tooling.help", "ch.group.failed", "ch.group.running"}):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        self.assertNotIn("cs.showreasoning", I18N)

    def test_the_chat_follows_the_setting_and_the_agent_chat_uses_the_containers(self):
        self.assertIn('chatSettings.shows("reasoning")', CHAT)
        self.assertIn('mode === "absent"', CHAT)
        self.assertIn('document.addEventListener("tadamoo:display"', CHAT)
        self.assertIn("window.chatDisplay.place(it, b)", AGENT)
        self.assertIn("window.chatDisplay.paint(it, T, rec && rec.inference ?", AGENT)        # the open conversation's own choice, not whatever the settings panel last held
        self.assertIn("web/chatdisplay.js", HTML)
        self.assertIn("web/chatdisplay.css", HTML)
        self.assertLess(HTML.index("web/chatdisplay.js"), HTML.index("web/agentchat.js"))

    def test_the_container_is_a_44_px_target_and_nothing_scrolls_sideways(self):
        css = (WEB / "chatdisplay.css").read_text(encoding="utf-8")
        self.assertIn("min-height: 44px", css)
        self.assertNotRegex(css, r"min-width:\s*\d{3,}px")
        self.assertIn("prefers-reduced-motion", css)


@unittest.skipUnless(NODE, "node is not installed")
class Containers(unittest.TestCase):
    def test_full_is_every_block_on_a_line_of_its_own_with_no_container(self):
        got = shape([R, R, R, TOOL("read_file"), TOOL("read_file"), TEXT, R], flat=True)
        self.assertEqual(got, [{"row": True, "hidden": False}] * 7)

    def test_compact_puts_each_run_in_one_closed_container_that_says_what_is_in_it(self):
        got = shape([R, R, R, TOOL("read_file"), TOOL("read_file"), TOOL("edit_file", "failed"), TEXT, R], "compact", "compact")
        self.assertEqual([g.get("group") or "row" for g in got], ["reasoning", "tooling", "row", "row"])
        self.assertEqual([(g.get("rows"), g.get("open"), g["hidden"]) for g in got[:2]], [(3, False, False), (3, False, False)])
        self.assertEqual(got[0]["title"], "ch.group.reasoning[3]")
        self.assertEqual(got[1]["title"], "ch.group.tooling[3,read_file ×2, edit_file] · ch.group.failed[1]")

    def test_a_run_of_one_block_has_no_container_in_any_mode(self):
        for mode in ("full", "compact"):
            self.assertEqual(shape([R, TEXT, TOOL("read_file")], mode, mode), [{"row": True, "hidden": False}] * 3)

    def test_each_kind_has_its_own_setting(self):
        got = shape([R, R, TOOL("a"), TOOL("b"), TEXT], "full", "compact")
        self.assertEqual([g.get("group") or "row" for g in got if not g.get("group") or not g["hidden"]], ["row", "row", "tooling", "row"])

    def test_an_explicit_choice_wins_over_whatever_the_settings_panel_currently_holds(self):
        # the settings panel (chatSettings.shows, mocked here) says "full" for both, but the open conversation's own saved choice is "compact":
        # paint() must follow the conversation, not the panel - this is what a conversation opened while the panel is bound to something else needs.
        got = shape([R, R, R, TOOL("read_file"), TOOL("read_file")], reasoning="full", tooling="full", paint_modes={"reasoning": "compact", "tooling": "compact"})
        self.assertEqual([g.get("group") or "row" for g in got], ["reasoning", "tooling"])

    def test_absent_shows_nothing_of_it_not_even_an_empty_container(self):
        got = shape([R, R, TOOL("a"), TOOL("b"), TEXT, R, TOOL("c")], "absent", "absent")
        self.assertEqual([g["hidden"] for g in got if not g.get("group")], [True, True, True, True, False, True, True])
        self.assertTrue(all(g["hidden"] for g in got if g.get("group")))
        compact = shape([R, R, TEXT], "absent", "compact")
        self.assertTrue(all(g["hidden"] for g in compact if g.get("group")))


if __name__ == "__main__":
    unittest.main()
