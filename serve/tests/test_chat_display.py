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
}
const modes = JSON.parse(process.argv[3]);
const ctx = { document: { createElement: t => new El(t), querySelectorAll: () => [] }, chatSettings: { shows: k => modes[k] }, console };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1] + '/chatdisplay.js', 'utf8'), ctx);
const D = ctx.window.chatDisplay, T = (k, ...a) => k + '[' + a.join(',') + ']';
const it = { body: new El('div'), blocks: JSON.parse(process.argv[2]) };
for (const b of it.blocks) { b.row = new El('div'); b.el = b.row; D.place(it, b); }
D.paint(it, T);
const shape = it.body.children.map(c => c.tag === 'details' ? { group: c.dataset.kind, rows: c.children[1].children.length, open: c.open, hidden: c.hidden, title: c.children[0].children[0].textContent } : { row: true, hidden: c.hidden });
console.log(JSON.stringify(shape));
"""


def shape(blocks, reasoning="full", tooling="full"):
    out = subprocess.run([NODE, "-e", DRIVER, str(WEB), json.dumps(blocks), json.dumps({"reasoning": reasoning, "tooling": tooling})], capture_output=True, text=True, timeout=60)
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
        self.assertIn("window.chatDisplay.paint(it, T)", AGENT)
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
    def test_a_run_of_one_block_has_no_container(self):
        self.assertEqual(shape([R, TEXT, TOOL("read_file")]), [{"row": True, "hidden": False}] * 3)

    def test_consecutive_reasoning_and_consecutive_actions_each_have_one_container(self):
        got = shape([R, R, R, TOOL("read_file"), TOOL("read_file"), TOOL("edit_file", "failed"), TEXT, R])
        self.assertEqual([g.get("group") or "row" for g in got], ["reasoning", "tooling", "row", "row"])
        self.assertEqual([g.get("rows") for g in got[:2]], [3, 3])
        self.assertEqual(got[0]["title"], "ch.group.reasoning[3]")
        self.assertEqual(got[1]["title"], "ch.group.tooling[3,read_file ×2, edit_file] · ch.group.failed[1]")

    def test_full_is_open_compact_is_closed_and_absent_hides_everything(self):
        blocks = [R, R, TOOL("a"), TOOL("b"), TEXT]
        full = shape(blocks, "full", "full")
        self.assertEqual([(g["open"], g["hidden"]) for g in full[:2]], [(True, False), (True, False)])
        compact = shape(blocks, "compact", "compact")
        self.assertEqual([(g["open"], g["hidden"]) for g in compact[:2]], [(False, False), (False, False)])
        absent = shape(blocks, "absent", "absent")
        self.assertEqual([g["hidden"] for g in absent], [True, True, False])
        mixed = shape(blocks, "absent", "compact")
        self.assertEqual([(g["hidden"], g["open"]) for g in mixed[:2]], [(True, False), (False, False)])

    def test_a_single_block_is_hidden_when_absent(self):
        self.assertEqual([g["hidden"] for g in shape([R, TEXT, TOOL("a")], "absent", "absent")], [True, False, True])


if __name__ == "__main__":
    unittest.main()
