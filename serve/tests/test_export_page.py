"""The Export button and window of the chat: where the button is, the texts in both languages, the safety rules of the page's scripts, the
accessors the window reads, and what the page sends (only the ticked kinds). They need no browser; node runs the page's script against stubs."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "serve" / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "export.js").read_text(encoding="utf-8")
CSS = (WEB / "export.css").read_text(encoding="utf-8")
FORMATS = ("docx", "md", "pdf", "html", "odt")


def strings():
    script = ("const vm=require('vm'),fs=require('fs');"
              "const ctx={document:{documentElement:{dataset:{}}},location:{search:''},URLSearchParams,localStorage:{getItem(){return null},setItem(){}}};"
              "vm.createContext(ctx);"
              f"vm.runInContext(fs.readFileSync({json.dumps(str(WEB / 'i18n.js'))},'utf8')+';this.S=STRINGS;',ctx);"
              "console.log(JSON.stringify(ctx.S))")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, encoding="utf-8")
    if out.returncode:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)


class MarkupTest(unittest.TestCase):
    def test_the_button_is_left_of_skills_in_the_chat_header(self):
        head = HTML[HTML.index('class="sh chathead"'):]
        self.assertLess(head.index('id="export-open"'), head.index('id="pskills-open"'))
        self.assertLess(head.index('id="branches-open"'), head.index('id="export-open"'))
        button = re.search(r'<button[^>]*id="export-open"[^>]*>', head).group(0)
        for needle in ('type="button"', 'aria-haspopup="dialog"', 'data-t="exp.btn"', 'data-t-title="exp.btn.title"'):
            self.assertIn(needle, button)
        self.assertNotIn("hidden", button)                                           # in every mode of the chat, not only with tools

    def test_the_script_and_style_are_loaded_after_the_chats(self):
        self.assertIn('<link rel="stylesheet" href="web/export.css">', HTML)
        order = [HTML.index(f'src="web/{n}"') for n in ("chat.js", "agentchat.js", "i18n.js", "export.js")]
        self.assertLess(order[0], order[3])
        self.assertLess(order[1], order[3])
        self.assertLess(order[2], order[0])

    @unittest.skipUnless(NODE, "node is needed to check the scripts")
    def test_the_script_parses(self):
        out = subprocess.run([NODE, "--check", str(WEB / "export.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_nothing_is_built_as_markup_and_the_key_travels(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)
        self.assertIn("authHeaders()", JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')
        for call in re.findall(r"fetch\(\s*[\"`]([^\"`]+)", JS):
            self.assertTrue(call.startswith("api/"), call)

    def test_the_last_choices_are_kept_inside_try_catch(self):
        for match in re.finditer(r"localStorage\.(get|set)Item", JS):
            before = JS[:match.start()]
            self.assertGreater(before.rfind("try {"), before.rfind("}"), "localStorage used outside try/catch")
        self.assertIn("basalt.export", JS)

    def test_the_window_is_a_labelled_dialog_that_returns_the_focus(self):
        for needle in ('mk("dialog"', "aria-labelledby", "aria-describedby", "showModal()", 'addEventListener("close"', "trigger.focus()", 'setAttribute("role", "status")',
                       'type = "checkbox"', "htmlFor", "aria-busy"):
            self.assertIn(needle, JS, needle)
        self.assertNotIn('addEventListener("cancel"', JS)                           # Escape is left to the dialog: it closes, then focus returns

    def test_the_style_is_mobile_first_and_accessible(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertRegex(CSS, r"font: 400 16px")                                     # inputs at 16 px: no zoom on focus in iOS
        self.assertIn("prefers-reduced-motion: reduce", CSS)
        self.assertIn(":focus-visible", CSS)
        self.assertIn("env(safe-area-inset-bottom)", CSS)
        self.assertIn("calc(100vw - 24px)", CSS)                                     # fits a 360 px screen
        self.assertNotRegex(CSS, r"overflow-x:\s*(auto|scroll)")
        self.assertNotRegex(CSS, r":hover")                                          # nothing that exists only with a mouse


@unittest.skipUnless(NODE, "node is needed to read the translations")
class TranslationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = strings()

    def used(self):
        keys = set(re.findall(r'"(exp\.[\w.]+)"', JS))
        keys |= set(re.findall(r'data-t(?:-title)?="(exp\.[\w.]+)"', HTML))
        return keys

    def test_every_key_used_exists_in_both_languages(self):
        for lang in ("en", "it"):
            self.assertEqual(sorted(k for k in self.used() if k not in self.s[lang]), [], lang)

    def test_every_key_defined_is_used(self):
        for lang in ("en", "it"):
            defined = {k for k in self.s[lang] if k.startswith("exp.")}
            self.assertEqual(sorted(defined - self.used()), [], lang)
        self.assertEqual({k for k in self.s["en"] if k.startswith("exp.")}, {k for k in self.s["it"] if k.startswith("exp.")})

    def test_the_placeholders_match_and_the_italian_is_translated(self):
        for key in self.used():
            self.assertEqual(sorted(re.findall(r"\{\d+\}", self.s["en"][key])), sorted(re.findall(r"\{\d+\}", self.s["it"][key])), key)
        for key in ("exp.btn", "exp.title", "exp.lead", "exp.what", "exp.k.answers.d", "exp.f.pdf.d", "exp.empty", "exp.fail"):
            self.assertNotEqual(self.s["en"][key], self.s["it"][key], key)

    def test_every_control_has_a_help_line(self):
        for kind in ("prompts", "reasoning", "actions", "answers"):
            self.assertIn(f"exp.k.{kind}.d", self.s["en"])
        for fmt in FORMATS:
            self.assertIn(f"exp.f.{fmt}.d", self.s["en"])
        self.assertIn("exp.name.d", self.s["en"])


HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const calls = [];
const SNAP = JSON.parse(process.argv[2]);
const win = {};
win.agentChat = { active: () => true, snapshot: () => SNAP };
win.chatUi = { diagram: code => (code === 'graph TD; A-->B' ? '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>' : '') };
const ctx = { window: win, document: { getElementById: () => null }, LANG: 'en', T: k => k, authHeaders: () => ({ 'X-Test': '1' }), URL,
  location: { href: 'http://127.0.0.1:8080/?x=1#chat' }, Date, DOMParser: class { parseFromString() { throw new Error('no dom'); } }, XMLSerializer: class {},
  Image: class {}, FileReader: class {}, console, localStorage: { getItem() { return null; }, setItem() {} },
  fetch: async (url, opts) => { calls.push([url, opts.headers]); return { ok: false }; } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const sel = JSON.parse(process.argv[3]);
win.TadamooChat.prepare(sel, 'My title').then(out => console.log(JSON.stringify({ out, calls })));
"""

SNAP = {
    "title": "Trip", "session": "abcd1234",
    "items": [
        {"kind": "prompt", "text": "PROMPT-MARK"},
        {"kind": "reasoning", "turn": 1, "text": "REASON-MARK"},
        {"kind": "action", "turn": 1, "name": "browse_url", "description": "DESC-MARK", "arguments": {"url": "ARG-MARK"},
         "result": "[file:0123456789ab:guide.pdf] RESULT-MARK", "sources": [{"url": "https://example.com/", "host": "example.com"}]},
        {"kind": "answer", "turn": 1, "text": "ANSWER-MARK\n\n![map](basalt-file:0123456789ab)\n\n```mermaid\ngraph TD; A-->B\n```\n"},
    ],
}


@unittest.skipUnless(NODE, "node is needed to run the page's script")
class SelectionTest(unittest.TestCase):
    def run_page(self, sel):
        out = subprocess.run([NODE, "-e", HARNESS, str(WEB / "export.js"), json.dumps(SNAP), json.dumps(sel)], capture_output=True, text=True, timeout=30, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_only_the_ticked_kinds_leave_the_page(self):
        for sel in ({"prompts": True}, {"reasoning": True}, {"actions": True}, {"answers": True}, {"prompts": True, "answers": True}, {"actions": True, "reasoning": True}):
            sent = json.dumps(self.run_page(sel)["out"])
            for kind, mark in (("prompts", "PROMPT-MARK"), ("reasoning", "REASON-MARK"), ("answers", "ANSWER-MARK")):
                self.assertEqual(mark in sent, bool(sel.get(kind)), (sel, mark))
            for mark in ("DESC-MARK", "ARG-MARK", "RESULT-MARK"):
                self.assertEqual(mark in sent, bool(sel.get("actions")), (sel, mark))

    def test_the_sources_of_unticked_actions_go_only_with_the_answers(self):
        sent = self.run_page({"answers": True})["out"]
        action = [i for i in sent["items"] if i["kind"] == "action"]
        self.assertEqual(len(action), 1)
        self.assertEqual(sorted(action[0]), ["kind", "result", "sources", "turn"])        # no name, description or arguments
        self.assertEqual(action[0]["result"], "[file:0123456789ab:guide.pdf]")             # the head that names the file, not its output
        self.assertEqual([i["kind"] for i in self.run_page({"prompts": True})["out"]["items"]], ["prompt"])

    def test_pictures_and_diagrams_are_read_only_for_ticked_answers(self):
        got = self.run_page({"answers": True})
        self.assertEqual(got["calls"], [["api/outputs/abcd1234/0123456789ab/file", {"X-Test": "1"}]])    # relative, with the key
        self.assertEqual(list(got["out"]["diagrams"]), ["graph TD; A-->B"])
        self.assertTrue(got["out"]["diagrams"]["graph TD; A-->B"]["svg"].startswith("<svg"))
        none = self.run_page({"prompts": True})
        self.assertEqual((none["calls"], none["out"]["diagrams"], none["out"]["assets"]), ([], {}, {}))

    def test_the_request_carries_what_the_server_needs_to_write_links_and_times(self):
        out = self.run_page({"answers": True})["out"]
        self.assertEqual((out["title"], out["session"], out["lang"]), ("My title", "abcd1234", "en"))
        self.assertEqual(out["base"], "http://127.0.0.1:8080/")
        self.assertIsInstance(out["tz"], int)
        self.assertGreater(out["exported_at"], 1_700_000_000)


if __name__ == "__main__":
    unittest.main()
